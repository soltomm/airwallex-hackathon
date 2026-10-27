"""Rule changes the revenue desk suggests from lost demand, and applies to the hotels' live rules.

Suggestions come from the dashboard's evidence and each carries one concrete change:
  night_floor    lower the floor on nights where travellers walked away below it
  night_rate     raise the published rate on busy nights that keep being asked for
  approval_band  make the seller agent hold out for more when deals close near the floor
Applying a change edits the Hotel in memory, so every seller agent prices with it from its next
tool call, and records it (with the values it replaced) so it can be reverted and its effect on
conversion measured. Changes persist in a JSON file and are re-applied at start-up.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from .offers import Hotel
from .storage import doc

MIN_EVIDENCE = 2                 # lost deals or requests before suggesting anything
MAX_FLOOR_CUT = Decimal("0.15")  # never suggest more than 15% off a night's floor
RATE_RAISE = Decimal("0.15")
BAND_STEP, BAND_MAX = Decimal("0.05"), Decimal("0.20")
LOW_MARGIN_PCT = Decimal("30")


def _round5(x: Decimal) -> Decimal:
    return (x / 5).to_integral_value(ROUND_CEILING) * 5


def _nights(check_in: str, check_out: str) -> list[date]:
    ci, co = date.fromisoformat(check_in), date.fromisoformat(check_out)
    return [ci + timedelta(days=i) for i in range((co - ci).days)]


def _span(nights: list[str]) -> str:
    ds = sorted(date.fromisoformat(n) for n in nights)
    if len(ds) == 1:
        return f"the night of {ds[0].day} {ds[0]:%b}"
    if ds[0].month == ds[-1].month:
        return f"the nights of {ds[0].day}–{ds[-1].day} {ds[-1]:%b}"
    return f"the nights of {ds[0].day} {ds[0]:%b} – {ds[-1].day} {ds[-1]:%b}"


def _id(*parts) -> str:
    return "sug_" + hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:10]


class RuleBook:
    def __init__(self, hotels: dict[str, Hotel], path: str | Path | None = None):
        self.hotels = hotels
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.changes: list[dict] = doc(self.path).read([]) if self.path else []   # file or Postgres
        for c in self.changes:
            if not c.get("reverted_at") and c["hotel_id"] in hotels:
                self._set(c, "after")

    def _save(self) -> None:
        if self.path:
            doc(self.path).write(self.changes)

    # ---- applying -------------------------------------------------------------
    def _set(self, c: dict, which: str) -> None:
        h = self.hotels[c["hotel_id"]]
        if c["kind"] == "approval_band":
            h.approval_band = Decimal(c[which])
            return
        target = h.night_floors if c["kind"] == "night_floor" else h.night_rates
        for night, values in c["nights"].items():
            key = (c["room"], date.fromisoformat(night))
            if values[which] is None:
                target.pop(key, None)
            else:
                target[key] = Decimal(values[which])

    def apply(self, suggestion: dict, now: datetime) -> dict:
        with self._lock:
            if any(c["id"] == suggestion["id"] and not c.get("reverted_at") for c in self.changes):
                raise ValueError("this suggestion is already applied")
            c = {**suggestion["change"], "id": suggestion["id"], "title": suggestion["title"],
                 "why": suggestion["why"], "applied_at": now.isoformat(), "reverted_at": None}
            self._set(c, "after")
            self.changes.append(c)
            self._save()
            return c

    def revert(self, change_id: str, now: datetime) -> dict:
        with self._lock:
            c = next((c for c in self.changes if c["id"] == change_id and not c.get("reverted_at")), None)
            if c is None:
                raise ValueError(f"no active change {change_id}")
            self._set(c, "before")
            c["reverted_at"] = now.isoformat()
            self._save()
            return c

    def active(self, hotel_id: str, kind: str) -> list[dict]:
        return [c for c in self.changes if c["hotel_id"] == hotel_id and c["kind"] == kind and not c.get("reverted_at")]

    # ---- suggesting -----------------------------------------------------------
    def suggestions(self, report: dict) -> list[dict]:
        out = []
        for hr in report["hotels"]:
            h = self.hotels[hr["hotel_id"]]
            out += self._lower_floors(h, hr) + self._raise_busy_rates(h, hr, report) + self._hold_out(h, hr)
        applied = {c["id"] for c in self.changes if not c.get("reverted_at")}
        return [s for s in out if s["id"] not in applied]

    def _since_last(self, h: Hotel, kind: str) -> str:
        return max((c["applied_at"] for c in self.active(h.id, kind)), default="")

    def _lower_floors(self, h: Hotel, hr: dict) -> list[dict]:
        """Travellers who walked away below the floor, per room, on nights at normal demand."""
        since = self._since_last(h, "night_floor")
        by_room: dict[str, list[dict]] = {}
        for r in hr["lost"]:
            if r["reason"] == "price below floor" and r.get("room") and r["at"] > since:
                by_room.setdefault(r["room"], []).append(r)
        out = []
        for room, rows in by_room.items():
            if len(rows) < MIN_EVIDENCE or room not in h.rooms:
                continue
            per_night = sorted(Decimal(r["gap"]) / len(_nights(r["check_in"], r["check_out"])) for r in rows)
            gap = per_night[len(per_night) // 2]
            nights = sorted({n for r in rows for n in _nights(r["check_in"], r["check_out"])
                             if h.occupancy(room, n) < h.high_occupancy})
            if not nights:
                continue
            cut = min(_round5(gap), *(_round5(h.floor_rate(room, n) * MAX_FLOOR_CUT) for n in nights))
            won = [r for r in rows if Decimal(r["gap"]) <= cut * len(_nights(r["check_in"], r["check_out"]))]
            change = {"hotel_id": h.id, "kind": "night_floor", "room": room,
                      "nights": {n.isoformat(): {"before": str(h.night_floors[(room, n)]) if (room, n) in h.night_floors else None,
                                                 "after": str(h.floor_rate(room, n) - cut)} for n in nights}}
            name = h.rooms[room].name
            out.append({
                "id": _id(change), "hotel_id": h.id, "hotel": h.name, "kind": "night_floor",
                "title": f"Lower the {name} floor by {cut} {h.currency} a night on {_span([n.isoformat() for n in nights])}",
                "why": f"{len(rows)} travellers walked away below the floor on these nights; the median gap was "
                       f"{gap.quantize(Decimal('1'))} {h.currency} a night.",
                "impact": f"Would have closed {len(won)} of those {len(rows)} deals "
                          f"({sum(Decimal(r['asked']) for r in won):.0f} {h.currency})." if won else
                          "Narrows the gap; the largest asks would still be refused.",
                "change": change})
        return out

    def _raise_busy_rates(self, h: Hotel, hr: dict, report: dict) -> list[dict]:
        """Nights that keep being asked for while the room is nearly full sell at the published rate:
        raise it."""
        since = self._since_last(h, "night_rate")
        if since:
            return []   # one raise at a time; revert or wait for new demand data
        out = []
        for room in h.rooms:
            nights = [n for n in hr["nights"] if n["requested"] >= MIN_EVIDENCE
                      and h.occupancy(room, date.fromisoformat(n["night"])) >= h.high_occupancy
                      and h.available(room, date.fromisoformat(n["night"]))]
            if not nights:
                continue
            ds = [date.fromisoformat(n["night"]) for n in nights]
            change = {"hotel_id": h.id, "kind": "night_rate", "room": room,
                      "nights": {d.isoformat(): {"before": str(h.night_rates[(room, d)]) if (room, d) in h.night_rates else None,
                                                 "after": str(_round5(h.rate(room, d) * (1 + RATE_RAISE)))} for d in ds}}
            name = h.rooms[room].name
            asks = sum(n["requested"] for n in nights)
            out.append({
                "id": _id(change), "hotel_id": h.id, "hotel": h.name, "kind": "night_rate",
                "title": f"Raise the {name} rate by {RATE_RAISE:.0%} on {_span([d.isoformat() for d in ds])}",
                "why": f"{asks} requests for these nights while the {name} is {h.high_occupancy:.0%}+ full; "
                       f"the last rooms sell at the published rate, so it sets the price.",
                "impact": f"About +{sum(Decimal(v['after']) - h.rate(room, date.fromisoformat(k)) for k, v in change['nights'].items()) / len(ds):.0f} "
                          f"{h.currency} a night on each room still to sell.",
                "change": change})
        return out

    def _hold_out(self, h: Hotel, hr: dict) -> list[dict]:
        """Deals closing near the floor: raise the margin the agent may give away on its own."""
        kept = hr["margin"]["kept_of_band_pct"]
        if kept is None or hr["margin"]["sold"] < MIN_EVIDENCE + 1 or Decimal(kept) >= LOW_MARGIN_PCT:
            return []
        if h.approval_band >= BAND_MAX:
            return []
        after = min(h.approval_band + BAND_STEP, BAND_MAX)
        change = {"hotel_id": h.id, "kind": "approval_band", "before": str(h.approval_band), "after": str(after)}
        return [{
            "id": _id(change, hr["margin"]["sold"]), "hotel_id": h.id, "hotel": h.name, "kind": "approval_band",
            "title": f"Make the agent hold out for more: commit alone only {after:.0%} above the floor "
                     f"(now {h.approval_band:.0%})",
            "why": f"Deals closed only {Decimal(kept):.0f}% of the way from floor to list price across "
                   f"{hr['margin']['sold']} sales.",
            "impact": "Lower offers go to a manager for sign-off instead of being committed by the agent.",
            "change": change}]

    # ---- measuring ------------------------------------------------------------
    def effects(self, entries: list[dict], bookings: dict[str, dict]) -> list[dict]:
        """Each change with conversion before and after it, for that hotel's negotiations."""
        booked = {b.get("negotiation_id") for b in bookings.values() if b.get("negotiation_id")}
        negs = [e for e in entries if e["event"] == "negotiation"]
        out = []
        for c in sorted(self.changes, key=lambda c: c["applied_at"], reverse=True):
            end = c.get("reverted_at") or "9999"
            mine = [n for n in negs if n["hotel_id"] == c["hotel_id"]]
            before = [n for n in mine if n["at"] < c["applied_at"]]
            after = [n for n in mine if c["applied_at"] <= n["at"] < end]
            rate = lambda ns: None if not ns else f"{100 * sum(n['negotiation_id'] in booked for n in ns) / len(ns):.0f}"
            out.append({**c, "effect": {"before": {"negotiations": len(before), "conversion_pct": rate(before)},
                                        "after": {"negotiations": len(after), "conversion_pct": rate(after)}}})
        return out
