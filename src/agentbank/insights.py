"""What the hotels learn from negotiating: demand, lost business and margin, for their revenue staff.

OfferDesk writes one structured event per search, negotiation, policy check, committed offer and
booking to a hotel-side log (never shown to buyers: it holds floors). `report()` joins it with the
bookings store into the dashboard's data: funnel, margin kept, unmet demand and why it was lost,
demand by night and by extra, and card payouts split between hotel and platform.
"""
from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .offers import OFFER_TTL, Hotel
from .reserves import ReservePolicy

CANCELLED = ("cancelled", "cancelled_refund_due", "cancelled_by_hotel", "refunded_by_hotel")


class DemandLog:
    """Append-only JSONL (or in memory when path is None)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._mem: list[dict] = []
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, at: datetime, **data) -> None:
        entry = {"at": at.isoformat(), "event": event, **data}
        with self._lock:
            if self.path:
                with self.path.open("a") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            else:
                self._mem.append(entry)

    def entries(self) -> list[dict]:
        with self._lock:
            if not self.path:
                return list(self._mem)
            if not self.path.exists():
                return []
            return [json.loads(line) for line in self.path.read_text().splitlines() if line]


def reason_category(reason: str) -> str:
    r = reason.lower()
    if "too low" in r:
        return "price below floor"
    if " left on " in r:
        return "sold out"
    if "late checkout" in r:
        return "late checkout unavailable"
    if "deposit" in r:
        return "deposit too small"
    if "sleeps up to" in r:
        return "room too small"
    if "sign-off" in r:
        return "needed manager sign-off"
    return "other"


def _nights(check_in: str, check_out: str) -> list[str]:
    ci, co = date.fromisoformat(check_in), date.fromisoformat(check_out)
    return [(ci + timedelta(days=i)).isoformat() for i in range((co - ci).days)]


def _d(x) -> Decimal:
    return Decimal(str(x))


def _pct(x: Decimal | None) -> str | None:
    return None if x is None else str((x * 100).quantize(Decimal("0.1")))


def _mean(xs: list[Decimal]) -> Decimal | None:
    return sum(xs) / len(xs) if xs else None


def _settlement(hb: list[dict], incidents: list[dict], policy: ReservePolicy, hotel_id: str,
                bookings: dict[str, dict], now: datetime) -> dict:
    """Reserves and reconciliation for one hotel, in its currency: what it was owed must equal what
    it was paid, what is held for it, and what was taken back for stays it did not deliver."""
    pct, why = policy.pct(hotel_id, bookings)
    frozen = policy.frozen(hotel_id, incidents, now)
    sums = defaultdict(Decimal)
    for b in hb:
        st = b.get("settlement") or {}
        parts = st.get("parts") or {}
        if not parts:
            continue
        owed = _d(st["hotel_payout"])
        reserve_h, now_h = _d(st.get("reserve", 0)), _d(st.get("paid_now", owed))
        for part, amount in (("now", now_h), ("reserve", reserve_h)):
            status = (parts.get(part) or {}).get("status")
            if not (parts.get(part) or {}).get("id"):
                sums["unpaid"] += amount
            elif status == "REVERSED":
                sums["recovered"] += amount
            elif part == "reserve" and status != "RELEASED":
                sums["reserve_held"] += amount
            else:
                sums["paid"] += amount
                if part == "reserve":
                    sums["reserve_released"] += amount
        sums["owed"] += owed
    sums["refunded"] = sum((_d((b.get("settlement") or {}).get("captured", b["deposit"])) for b in hb
                            if b["status"] == "refunded_by_hotel"), Decimal(0))
    balanced = sums["owed"] == sums["paid"] + sums["reserve_held"] + sums["recovered"] + sums["unpaid"]
    return {"reserve_pct": _pct(pct), "reserve_why": why,
            "frozen_by": {"at": frozen["at"], "night": frozen["night"], "reason": frozen["reason"]} if frozen else None,
            **{k: str(sums[k]) for k in ("owed", "paid", "reserve_held", "reserve_released", "recovered",
                                         "unpaid", "refunded")},
            "reconciled": balanced}


def report(entries: list[dict], hotels: dict[str, Hotel], bookings: dict[str, dict], now: datetime,
           reserves: ReservePolicy | None = None) -> dict:
    reserves = reserves or ReservePolicy()
    incidents = [e for e in entries if e["event"] == "incident"]
    searches = [e for e in entries if e["event"] == "search"]
    offers = {e["offer_id"]: e for e in entries if e["event"] == "offer"}
    checks_by_neg: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e["event"] == "check":
            checks_by_neg[e["negotiation_id"]].append(e)
    negotiations = {e["negotiation_id"]: e for e in entries if e["event"] == "negotiation"}
    last_seen: dict[str, str] = {}
    for e in entries:
        if e.get("negotiation_id"):
            last_seen[e["negotiation_id"]] = e["at"]
    booked_negs = {b.get("negotiation_id") for b in bookings.values() if b.get("negotiation_id")}

    extras_requested = Counter(x for s in searches for x in s["extras"])
    cancellation_requested = Counter(s["cancellation"] for s in searches)
    out_hotels = []
    totals = defaultdict(Decimal)

    for h in hotels.values():
        hb = [b for b in bookings.values() if b["hotel_id"] == h.id]
        live = [b for b in hb if b["status"] not in CANCELLED]
        h_offers = [o for o in offers.values() if o["hotel_id"] == h.id]
        h_negs = [n for n in negotiations.values() if n["hotel_id"] == h.id]

        # Turned away at search: no room of this hotel could sell the requested terms.
        turned_away = Counter()
        for s in searches:
            mine = [r for r in s["results"] if r["hotel_id"] == h.id]
            if mine and not any(r["bookable"] for r in mine):
                turned_away[reason_category("; ".join(n for r in mine for n in r["notes"]))] += 1

        # Margin: where in the band between floor and list price each sold offer landed.
        kept, discount = [], []
        for b in hb:
            o = offers.get(b.get("offer_id", ""))
            if not o:
                continue
            total, floor, published = _d(o["total"]), _d(o["floor"]), _d(o["published"])
            if published > floor:
                kept.append((total - floor) / (published - floor))
            if published:
                discount.append(1 - total / published)

        # Lost demand: negotiations that ended without a booking, and why.
        lost, open_now = [], 0
        for nid, n in negotiations.items():
            if n["hotel_id"] != h.id or nid in booked_negs:
                continue
            idle = now - datetime.fromisoformat(last_seen.get(nid, n["at"]))
            if idle < OFFER_TTL:
                open_now += 1
                continue
            made = [o for o in h_offers if o.get("negotiation_id") == nid]
            rejected = [c for c in checks_by_neg.get(nid, []) if c["verdict"] != "EXECUTE"]
            terms = (made or checks_by_neg.get(nid) or [{}])[-1]
            row = {"negotiation_id": nid, "at": n["at"], "room": terms.get("room"), "check_in": terms.get("check_in"),
                   "check_out": terms.get("check_out"), "guests": terms.get("guests"),
                   "extras": terms.get("extras", []), "cancellation": terms.get("cancellation")}
            if made:
                best = min(made, key=lambda o: _d(o["total"]))
                row |= {"reason": "offer not taken", "offered": best["total"]}
            elif rejected:
                below = [c for c in rejected if reason_category(c["reason"]) == "price below floor"]
                if below:
                    c = min(below, key=lambda c: _d(c["total"]))
                    row |= {"reason": "price below floor", "asked": c["total"], "floor": c["floor"],
                            "gap": str(_d(c["floor"]) - _d(c["total"]))}
                else:
                    row["reason"] = reason_category(rejected[-1]["reason"])
            else:
                row["reason"] = "no offer made"
            lost.append(row)

        requested_nights = Counter(night for s in searches for night in _nights(s["check_in"], s["check_out"]))
        booked_nights = Counter(night for b in live for night in _nights(b["check_in"], b["check_out"]))
        lost_nights = Counter(night for r in lost if r["check_in"] for night in _nights(r["check_in"], r["check_out"]))
        nights = sorted(set(requested_nights) | set(booked_nights) | set(lost_nights))

        settled = [b for b in hb if b.get("settlement")]
        money = {
            "booked_total": sum((_d(b["total"]) for b in live), Decimal(0)),
            "deposits_held": sum((_d(b["deposit"]) for b in hb if b["status"] == "deposit_held"), Decimal(0)),
            "captured": sum((_d(b["settlement"]["captured"]) for b in settled), Decimal(0)),
            "commission": sum((_d(b["settlement"]["commission"]) for b in settled
                               if b["settlement"].get("funds_split_id")), Decimal(0)),
            "hotel_payout": sum((_d(b["settlement"]["hotel_payout"]) for b in settled
                                 if b["settlement"].get("funds_split_id")), Decimal(0)),
            "lost_value": sum((_d(r.get("asked") or r.get("offered") or 0) for r in lost), Decimal(0)),
        }
        for k, v in money.items():
            totals[k] += v
        funnel = {"searches": len(searches), "negotiations": len(h_negs), "offers": len(h_offers),
                  "bookings": len(hb), "cancelled": len(hb) - len(live), "lost": len(lost), "open": open_now}
        for k, v in funnel.items():
            totals[k] += v

        out_hotels.append({
            "hotel_id": h.id, "name": h.name, "currency": h.currency,
            "rooms": [{"code": r.code, "name": r.name} for r in h.rooms.values()],
            "commission_pct": _pct(h.commission_pct), "connected_account": h.connected_account or None,
            "funnel": funnel,
            "conversion_pct": _pct(Decimal(len(hb)) / len(h_negs)) if h_negs else None,
            "money": {k: str(v) for k, v in money.items()},
            "margin": {"kept_of_band_pct": _pct(_mean(kept)), "discount_vs_list_pct": _pct(_mean(discount)),
                       "sold": len(kept)},
            "extras_sold": dict(Counter(x for b in live for x in b["extras"])),
            "lost_reasons": dict(Counter(r["reason"] for r in lost)),
            "turned_away": dict(turned_away),
            "lost": sorted(lost, key=lambda r: r["at"], reverse=True),
            "nights": [{"night": n, "requested": requested_nights[n], "booked": booked_nights[n],
                        "lost": lost_nights[n]} for n in nights],
            "payouts": [{"booking_ref": b["booking_ref"], "status": b["status"],
                         **{k: v for k, v in b["settlement"].items() if k != "parts"},
                         "reserve_status": ((b["settlement"].get("parts") or {}).get("reserve") or {}).get("status"),
                         "now_status": ((b["settlement"].get("parts") or {}).get("now") or {}).get("status")}
                        for b in sorted(settled, key=lambda b: b["booked_at"], reverse=True)],
            "settlement": _settlement(hb, incidents, reserves, h.id, bookings, now),
            "incidents": [i for i in incidents if i["hotel_id"] == h.id],
            "bookings": [{k: b.get(k) for k in ("booking_ref", "room", "check_in", "check_out", "guests", "extras",
                                                "cancellation", "total", "deposit", "method", "status", "booked_at")}
                         for b in sorted(hb, key=lambda b: b["booked_at"], reverse=True)],
        })

    headlines = _headlines(out_hotels, extras_requested, len(searches))
    return {
        "generated_at": now.isoformat(),
        "currency": next(iter(hotels.values())).currency if hotels else "EUR",
        "totals": {k: str(v) if isinstance(v, Decimal) else v for k, v in totals.items()},
        "requests": {"searches": len(searches), "extras": dict(extras_requested),
                     "cancellation": dict(cancellation_requested)},
        "headlines": headlines,
        "hotels": out_hotels,
    }


def _headlines(hotels: list[dict], extras: Counter, searches: int) -> list[str]:
    """A few plain-language takeaways for the revenue manager."""
    out = []
    for h in hotels:
        below = [r for r in h["lost"] if r["reason"] == "price below floor"]
        if below:
            gaps = sorted(_d(r["gap"]) for r in below)
            out.append(f"{h['name']}: {len(below)} lost negotiation{'s' if len(below) > 1 else ''} wanted a price "
                       f"below the floor; the median gap was {gaps[len(gaps) // 2]} {h['currency']}.")
        sold_out = h["turned_away"].get("sold out", 0) + h["lost_reasons"].get("sold out", 0)
        if sold_out:
            out.append(f"{h['name']}: {sold_out} request{'s' if sold_out > 1 else ''} hit sold-out nights; "
                       f"consider raising those nights' rates.")
        if h["margin"]["kept_of_band_pct"] is not None:
            out.append(f"{h['name']}: deals closed {Decimal(h['margin']['kept_of_band_pct']):.0f}% of the way from floor to list price.")
    if searches:
        for extra, n in extras.most_common():
            out.append(f"{n * 100 // searches}% of travellers asked for {extra.replace('_', ' ')}.")
    return out
