"""The hotels' side of the marketplace, as plain functions a buyer agent can call.

search -> negotiate (with that hotel's seller agent) -> book an offer the hotel committed to.
Only offers the policy let the seller commit get an offer_id, and booking takes nothing
but that id, so the terms the traveller confirmed are exactly the terms that get booked.

Every call carries a tenant: one buyer connection (one MCP session when hosted, "local" over
stdio). Hotels' inventory is shared, like a real marketplace; negotiations, offers, bookings
and the live feed belong to the tenant that made them.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from .airwallex import AirwallexAPI, AirwallexError
from .approvals import ApprovalStore
from .audit import AuditLog
from .bookings import BookingError, BookingStore, HotelPayments, usdz
from .guest_currency import GUEST_CURRENCIES, describe
from .insights import DemandLog, report
from .reserves import DELIVERED, ReservePolicy
from .market_simulation import MarketSimulator
from .rule_tuning import RuleBook
from .ledger import DuplicateOperation, OperationLedger
from .money import Money
from .offers import OFFER_TTL, Cancellation, Hotel, Offer, SellerDecision, SellerPolicy, load_hotels
from .policy import Verdict
from .seller_agent import SellerAgent, usage_cost

PAYMENT_METHODS = ("card", "metal")
LOCAL = "local"
_PRIVATE = ("client_secret", "approval_fingerprint", "history", "tenant", "payout", "settlement",
            "negotiation_id")   # kept out of buyer-facing views


@dataclass
class Limits:
    """Caps for a shared deployment; None means unlimited (the local default)."""
    negotiations_per_tenant: int | None = None
    turns_per_tenant: int | None = None
    daily_usd: Decimal | None = None          # seller agents' estimated model spend, all tenants


class OfferDesk:
    def __init__(self, hotels: dict[str, Hotel], audit: AuditLog, ledger: OperationLedger,
                 bookings: BookingStore, api: AirwallexAPI | None = None, client=None,
                 pay_base_url: str = "http://127.0.0.1:8009", metal_base_url: str = "http://127.0.0.1:4022",
                 payment_methods: tuple[str, ...] = PAYMENT_METHODS, limits: Limits | None = None,
                 demand: DemandLog | None = None, rulebook_path: Path | None = None,
                 now=lambda: datetime.now(timezone.utc)):
        self.hotels = hotels
        self.audit = audit
        self.ledger = ledger
        self.bookings = bookings
        self.reserves = ReservePolicy()
        self.payments = HotelPayments(bookings, ledger, audit, api, now=now,
                                      reserve_for=lambda b: self.reserves.pct(b["hotel_id"], self.bookings.all()))
        self.client = client                     # Anthropic client for seller agents (lazy if None)
        self.pay_base_url = pay_base_url         # card checkout pages (scripts/hotel_mcp.py serves them)
        self.metal_base_url = metal_base_url     # x402 deposit endpoint (metal/hotel-pay.ts)
        self.payment_methods = payment_methods
        self.limits = limits or Limits()
        self.demand = demand or DemandLog()       # hotel-side insights; holds floors, never shown to buyers
        self.rulebook = RuleBook(hotels, rulebook_path)   # rule changes applied from the dashboard
        self.sim = MarketSimulator(self)                  # sandbox-only market events
        self.now = now
        self.approvals = ApprovalStore(ttl_seconds=int(OFFER_TTL.total_seconds()))
        self.sellers: dict[str, SellerAgent] = {}   # negotiation_id -> that hotel's seller agent
        self.seller_tenant: dict[str, str] = {}     # negotiation_id -> tenant
        self.offers: dict[str, Offer] = {}          # committed offers, bookable by id
        self.offer_tenant: dict[str, str] = {}      # offer_id -> tenant
        self.offer_negotiation: dict[str, str] = {}  # offer_id -> negotiation_id
        self.turns: dict[str, int] = {}             # tenant -> buyer turns sent to sellers
        self.spend: dict[str, Decimal] = {}         # ISO day -> estimated seller spend in $
        self.feed: list[dict] = []                  # what live views show; never the hotel's floors
        self._feed_lock = threading.Lock()
        self._last_refresh = 0.0
        for b in bookings.all().values():           # rooms already sold survive restarts
            if b["status"] not in ("cancelled", "cancelled_refund_due", "cancelled_by_hotel", "refunded_by_hotel"):
                self._reserve(b["hotel_id"], b["room_code"], b["check_in"], b["check_out"])

    @classmethod
    def from_files(cls, root: Path, api: AirwallexAPI | None = None, **kw) -> "OfferDesk":
        return cls(load_hotels(root / "data/hotels.json"), AuditLog(root / "var/audit.jsonl"),
                   OperationLedger(root / "var/ledger.sqlite3"), BookingStore(root / "var/bookings.json"), api,
                   demand=DemandLog(root / "var/demand.jsonl"), rulebook_path=root / "var/rule_changes.json", **kw)

    def _reserve(self, hotel_id: str, room_code: str, check_in: str, check_out: str, rooms: int = 1) -> None:
        h = self.hotels[hotel_id]
        ci, co = date.fromisoformat(check_in), date.fromisoformat(check_out)
        for i in range((co - ci).days):
            night = ci + timedelta(days=i)
            h.booked[(room_code, night)] = h.booked.get((room_code, night), 0) + rooms

    def _policy(self, hotel_id: str) -> SellerPolicy:
        return SellerPolicy(self.hotels[hotel_id], now=self.now)

    def view(self, offer_id: str) -> dict:
        o = self.offers[offer_id]
        h = self.hotels[o.hotel_id]
        return {"offer_id": offer_id, **o.terms(), "hotel": h.name, "room": h.rooms[o.room_code].name,
                "deposit_pct": str(o.deposit_pct), "total": str(o.total)}

    def commit(self, offer: Offer, tenant: str = LOCAL, negotiation_id: str | None = None) -> str:
        offer_id = "ofr_" + uuid.uuid4().hex[:10]
        self.offers[offer_id] = offer
        self.offer_tenant[offer_id] = tenant
        if negotiation_id:
            self.offer_negotiation[offer_id] = negotiation_id
        self.demand.record("offer", self.now(), offer_id=offer_id, negotiation_id=negotiation_id,
                           **self._economics(offer))
        return offer_id

    def _economics(self, offer: Offer) -> dict:
        h = self.hotels[offer.hotel_id]
        return {"hotel_id": offer.hotel_id, **offer.terms(), "total": str(offer.total.amount),
                "floor": str(h.floor_total(offer).amount), "published": str(h.published_total(offer).amount)}

    def _checked(self, negotiation_id: str, tool: str, offer: Offer, d: SellerDecision) -> None:
        self.demand.record("check", self.now(), negotiation_id=negotiation_id, tool=tool, verdict=d.verdict.name,
                           reason="; ".join(d.public), **self._economics(offer))

    def search(self, check_in: str, check_out: str, guests: int, extras: list[str],
               cancellation: str, tenant: str = LOCAL) -> list[dict]:
        """Published prices for the requested terms. Everything is negotiable via negotiate()."""
        cancel = Cancellation(cancellation)
        results = []
        for h in self.hotels.values():
            for room in h.rooms.values():
                o = Offer(h.id, room.code, date.fromisoformat(check_in), date.fromisoformat(check_out), guests,
                          tuple(extras), cancel, h.min_deposit[cancel], Money.of(0, h.currency),
                          self.now() + OFFER_TTL)
                o = replace(o, total=h.published_total(o))
                d = self._policy(h.id).evaluate(o)
                results.append({"hotel_id": h.id, "hotel": h.name, "room_code": room.code, "room": room.name,
                                "max_guests": room.max_guests, "published_total": str(o.total),
                                "bookable_as_requested": d.allowed, "notes": [] if d.allowed else list(d.public)})
        self.audit.record("search", tenant=tenant, check_in=check_in, check_out=check_out, guests=guests,
                          extras=extras, cancellation=cancellation, results=len(results))
        self.demand.record("search", self.now(), tenant=tenant, check_in=check_in, check_out=check_out,
                           guests=guests, extras=extras, cancellation=cancellation,
                           results=[{"hotel_id": r["hotel_id"], "room_code": r["room_code"],
                                     "bookable": r["bookable_as_requested"], "notes": r["notes"]} for r in results])
        self._emit("search", tenant, request={"check_in": check_in, "check_out": check_out, "guests": guests,
                                              "extras": extras, "cancellation": cancellation}, results=results)
        return results

    def _over_limit(self, tenant: str, new: bool) -> str | None:
        lim = self.limits
        if lim.daily_usd is not None and self.spend.get(self.now().date().isoformat(), Decimal(0)) >= lim.daily_usd:
            return "the demo's daily budget for hotel agents is used up; please try again tomorrow"
        if lim.turns_per_tenant is not None and self.turns.get(tenant, 0) >= lim.turns_per_tenant:
            return "this session has reached its negotiation limit; book one of the offers you have"
        if new and lim.negotiations_per_tenant is not None and \
                sum(t == tenant for t in self.seller_tenant.values()) >= lim.negotiations_per_tenant:
            return "this session has opened its maximum number of negotiations; continue an existing one"
        return None

    def negotiate(self, hotel_id: str, message: str, negotiation_id: str | None = None,
                  tenant: str = LOCAL) -> dict:
        """Send the buyer's message to the hotel's seller agent; continue a negotiation by id."""
        if negotiation_id:
            seller = self.sellers.get(negotiation_id)
            if seller is None or seller.hotel.id != hotel_id or self.seller_tenant[negotiation_id] != tenant:
                return {"error": f"unknown negotiation {negotiation_id} for {hotel_id}"}
        elif hotel_id not in self.hotels:
            return {"error": f"unknown hotel {hotel_id}"}
        if refusal := self._over_limit(tenant, new=not negotiation_id):
            return {"error": refusal}
        if not negotiation_id:
            negotiation_id = "neg_" + uuid.uuid4().hex[:10]
            seller = self.sellers[negotiation_id] = SellerAgent(
                self.hotels[hotel_id], lambda offer, nid=negotiation_id: self.commit(offer, tenant, nid), self.audit,
                client=self.client, now=self.now,
                on_activity=lambda text, nid=negotiation_id: self._emit("activity", tenant, negotiation_id=nid,
                                                                         text=text),
                on_check=lambda tool, offer, d, nid=negotiation_id: self._checked(nid, tool, offer, d))
            self.seller_tenant[negotiation_id] = tenant
            self.demand.record("negotiation", self.now(), negotiation_id=negotiation_id, hotel_id=hotel_id,
                               tenant=tenant)
        hotel = self.hotels[hotel_id].name
        self._emit("buyer", tenant, negotiation_id=negotiation_id, hotel_id=hotel_id, hotel=hotel,
                   round=seller.buyer_turns + 1, message=message)
        before = dict(seller.usage)
        reply = seller.reply(message)
        self.turns[tenant] = self.turns.get(tenant, 0) + 1
        cost = usage_cost(seller.usage, before, seller.model)
        day = self.now().date().isoformat()
        self.spend[day] = self.spend.get(day, Decimal(0)) + cost
        offers = [self.view(i) for i in reply["offer_ids"]]
        self._emit("seller", tenant, negotiation_id=negotiation_id, hotel_id=hotel_id, hotel=hotel,
                   round=seller.buyer_turns, message=reply["message"], offers=offers)
        return {"negotiation_id": negotiation_id, "round": seller.buyer_turns, "seller_activity": reply["activity"],
                "seller_message": reply["message"], "offers": offers}

    def book(self, offer_id: str, traveller_confirmed: bool, payment_method: str = "card",
             tenant: str = LOCAL, currency: str | None = None) -> dict:
        if not traveller_confirmed:
            return {"result": "not_booked", "reason": "show the traveller the exact terms and get a yes first"}
        if payment_method not in self.payment_methods:
            return {"result": "not_booked",
                    "reason": f"payment_method must be one of {', '.join(self.payment_methods)} on this server"}
        currency = (currency or "").upper() or None
        if currency and currency not in GUEST_CURRENCIES:
            return {"result": "not_booked", "reason": f"currency must be one of {', '.join(GUEST_CURRENCIES)}"}
        if currency and payment_method != "card":
            return {"result": "not_booked", "reason": "a guest currency applies to card deposits only"}
        offer = self.offers.get(offer_id)
        if offer is None or self.offer_tenant.get(offer_id) != tenant:
            return {"result": "not_booked", "reason": f"unknown offer {offer_id}"}
        done = self.ledger.get(f"booking:{offer_id}")
        if done is not None:
            return self._booked_before(done)
        h = self.hotels[offer.hotel_id]
        d = self._policy(offer.hotel_id).evaluate(offer)   # inventory or the clock may have moved
        if d.verdict is not Verdict.EXECUTE:
            return self._terms_changed(offer_id, d.public, tenant)
        if payment_method == "metal" and not h.metal_payee:
            return {"result": "not_booked", "reason": f"{h.name} does not accept Metal payments"}

        approval = offer.to_approval(h.name)
        aid = self.approvals.request(approval)
        self.approvals.approve(aid, "traveller")
        self.approvals.consume(aid, approval)

        def reserve(request_id: str) -> dict:
            ref = "BKG-" + request_id[:8].upper()
            self._reserve(offer.hotel_id, offer.room_code, offer.check_in.isoformat(), offer.check_out.isoformat())
            self.bookings.put({
                "booking_ref": ref, **self.view(offer_id), "room_code": offer.room_code,
                "currency": offer.total.currency, "total": str(offer.total.amount),
                "deposit": str(offer.deposit.amount), "method": payment_method, "status": "awaiting_payment",
                "approval_fingerprint": approval.fingerprint(), "booked_at": self.now().isoformat(),
                "tenant": tenant,
                "negotiation_id": self.offer_negotiation.get(offer_id),
                **({"guest_currency": currency} if currency and currency != offer.total.currency else {}),
                **({"metal": {"usdz": usdz(offer.deposit, h.usdz_per_eur), "pay_to": h.metal_payee}}
                   if payment_method == "metal" else {}),
                **({"payout": {"connected_account": h.connected_account, "commission_pct": str(h.commission_pct)}}
                   if payment_method == "card" and h.connected_account else {})})
            return {"booking_ref": ref}

        try:
            ref = self.ledger.run(f"booking:{offer_id}", reserve)["booking_ref"]
        except DuplicateOperation as e:   # a concurrent call for the same offer got there first
            return self._booked_before(e.op)
        self.audit.record("booking_confirmed", booking_ref=ref, offer_id=offer_id, method=payment_method,
                          approval_fingerprint=approval.fingerprint())
        self.demand.record("booking", self.now(), booking_ref=ref, offer_id=offer_id,
                           negotiation_id=self.offer_negotiation.get(offer_id), hotel_id=offer.hotel_id,
                           method=payment_method)
        if payment_method == "card":
            try:
                self.payments.open_card_payment(ref)
            except BookingError as e:
                self.note_booking(ref)
                return {"result": "booked_payment_unavailable", "reason": str(e), **self.booking_view(ref)}
        self.note_booking(ref)
        return {"result": "booked", **self.booking_view(ref)}

    def _terms_changed(self, offer_id: str, reasons: tuple[str, ...], tenant: str) -> dict:
        """The confirmed terms can no longer be sold (rooms sold elsewhere, the offer expired): the hotel
        commits the closest terms it can, and the traveller must approve those before anything is booked."""
        old = self.offers[offer_id]
        policy = self._policy(old.hotel_id)
        h = self.hotels[old.hotel_id]
        fresh = replace(old, expires_at=self.now() + OFFER_TTL)
        candidates = [fresh] + [replace(fresh, room_code=r.code) for r in h.rooms.values()
                                if r.code != old.room_code and r.max_guests >= old.guests]
        counter = next((c for c in map(policy.counter, candidates) if c is not None), None)
        if counter is not None:   # whole euros, like every price the hotel quotes (cents would hint at the floor)
            counter = replace(counter, total=Money.of(counter.total.amount.to_integral_value(ROUND_CEILING),
                                                      counter.total.currency))
        expired = old.expires_at <= self.now()
        reason = ("the offer expired" if expired else "demand for these nights changed since the offer was made") + \
            (f" ({'; '.join(reasons)})" if counter is None else "")
        self.audit.record("terms_changed", offer_id=offer_id, reason=reason, counter=bool(counter))
        if counter is None:
            return {"result": "not_booked", "reason": reason, "next_step": "tell the traveller; search again"}
        nid = self.offer_negotiation.get(offer_id)
        new_id = self.commit(counter, tenant, nid)
        before, after = self.view(offer_id), self.view(new_id)
        changed = [k for k in ("room", "extras", "deposit_pct", "total") if before[k] != after[k]]
        self._emit("seller", tenant, negotiation_id=nid, hotel_id=old.hotel_id, hotel=h.name,
                   round=self.sellers[nid].buyer_turns if nid in self.sellers else 0,
                   message=f"Those terms are no longer available ({reason}). The closest we can confirm: "
                           f"{', '.join(f'{k.replace('_', ' ')} {after[k]}' for k in changed) or 'the same terms'}.",
                   offers=[after])
        return {"result": "terms_changed", "reason": reason, "previous_offer": before, "new_offer": after,
                "changed": changed,
                "next_step": "show the traveller exactly what changed and book new_offer.offer_id only after "
                             "they approve these new terms"}

    def _booked_before(self, op) -> dict:
        if op.result:
            return {"result": "already_booked", **self.booking_view(op.result["booking_ref"])}
        return {"result": "booking_in_progress", "reason": "this offer is being booked; check again shortly"}

    def _owned(self, ref: str, tenant: str | None) -> dict:
        """The booking, if `tenant` may see it (None: the hotel's own pages and scripts)."""
        b = self.bookings.get(ref)
        if tenant is not None and b.get("tenant", LOCAL) != tenant:
            raise BookingError(f"unknown booking {ref}")
        return b

    def booking_view(self, ref: str) -> dict:
        b = self.bookings.get(ref)
        view = {k: v for k, v in b.items() if k not in _PRIVATE}
        if b["status"] == "awaiting_payment":
            if b["method"] == "card" and not b.get("payment_intent_id"):
                view["next_step"] = "card payment not open yet: call booking_status to retry"
            elif b["method"] == "card":
                hold = b["cancellation"] != Cancellation.NON_REFUNDABLE.value
                view["next_step"] = (f"traveller opens {self.pay_base_url}/pay/{ref} and "
                                     f"{'authorises a hold of' if hold else 'pays'} {describe(b, 'deposit')} by card")
            else:
                view["next_step"] = (f"pay {b['metal']['usdz']} *USDZ with the Metal wallet's pay tool: "
                                     f"{self.metal_base_url}/bookings/{ref}/deposit")
        deadline = self.payments.free_cancellation_until(b)
        if deadline:
            view["free_cancellation_until"] = deadline.isoformat()
        return view

    def booking_status(self, ref: str, tenant: str | None = LOCAL) -> dict:
        try:
            before = self._owned(ref, tenant)["status"]
            b = self.payments.refresh(ref)
        except BookingError as e:
            return {"error": str(e)}
        if b["status"] != before:
            self.note_booking(ref)
        if b["method"] == "card" and b["status"] == "awaiting_payment" and not b.get("payment_intent_id"):
            try:
                self.payments.open_card_payment(ref)
            except BookingError as e:
                return {**self.booking_view(ref), "payment_error": str(e)}
        return self.booking_view(ref)

    def cancel_booking(self, ref: str, tenant: str | None = LOCAL) -> dict:
        try:
            self._owned(ref, tenant)
            b = self.payments.cancel(ref)
        except BookingError as e:
            return {"error": str(e)}
        if b["status"] in ("cancelled", "cancelled_refund_due"):
            self._reserve(b["hotel_id"], b["room_code"], b["check_in"], b["check_out"], rooms=-1)
        self.note_booking(ref)
        return self.booking_view(ref)

    def insights(self) -> dict:
        """The hotels' revenue dashboard data (hotel staff only: it includes floors and lost demand),
        with rule changes it suggests and those already applied."""
        entries, bookings = self.demand.entries(), self.bookings.all()
        r = report(entries, self.hotels, bookings, self.now(), self.reserves)
        r["suggestions"] = self.rulebook.suggestions(r)
        r["rule_changes"] = self.rulebook.effects(entries, bookings)
        return r

    # ---- hotel staff (dashboard) ----------------------------------------------
    def apply_suggestion(self, suggestion_id: str) -> dict:
        s = next((s for s in self.insights()["suggestions"] if s["id"] == suggestion_id), None)
        if s is None:
            return {"error": "that suggestion is no longer current; reload the dashboard"}
        try:
            c = self.rulebook.apply(s, self.now())
        except ValueError as e:
            return {"error": str(e)}
        self.audit.record("rule_change_applied", change=c)
        return {"applied": c}

    def revert_change(self, change_id: str) -> dict:
        try:
            c = self.rulebook.revert(change_id, self.now())
        except ValueError as e:
            return {"error": str(e)}
        self.audit.record("rule_change_reverted", change=c)
        return {"reverted": c}

    # ---- payout cycle and incidents (the platform's settlement side) ------------
    def incidents(self) -> list[dict]:
        return [e for e in self.demand.entries() if e["event"] == "incident"]

    def run_payout_cycle(self) -> dict:
        """Release held reserves for delivered stays, except at hotels with a recent incident."""
        released, held = [], []
        incidents = self.incidents()
        for b in self.bookings.all().values():
            r = b.get("settlement", {}).get("parts", {}).get("reserve", {})
            if not r.get("id") or r.get("status") in ("RELEASED", "REVERSED"):
                continue
            frozen = self.reserves.frozen(b["hotel_id"], incidents, self.now())
            if b["status"] not in DELIVERED:
                held.append({"booking_ref": b["booking_ref"], "why": "stay not delivered yet"})
            elif frozen:
                held.append({"booking_ref": b["booking_ref"],
                             "why": f"incident at the hotel on {frozen['at'][:10]}: {frozen['reason']}"})
            else:
                try:
                    self.payments.release_reserve(b["booking_ref"])
                    released.append({"booking_ref": b["booking_ref"], "amount": f"{r['amount']} {r['currency']}"})
                except (AirwallexError, BookingError) as e:
                    held.append({"booking_ref": b["booking_ref"], "why": f"release failed: {e}"})
        self.audit.record("payout_cycle", released=released, held=held)
        return {"released": released, "held": held}

    def hotel_incident(self, hotel_id: str, night: str, reason: str) -> dict:
        """A hotel can't honour stays covering `night`: guests get their holds released or their money
        back, the hotel's share is recovered, its reserves freeze and its reserve rate is recomputed."""
        if hotel_id not in self.hotels:
            return {"error": f"unknown hotel {hotel_id}"}
        before = self.reserves.pct(hotel_id, self.bookings.all())
        n = date.fromisoformat(night)
        affected = [b for b in self.bookings.all().values() if b["hotel_id"] == hotel_id
                    and date.fromisoformat(b["check_in"]) <= n < date.fromisoformat(b["check_out"])
                    and b["status"] in ("awaiting_payment", "deposit_held", "paid")]
        outcomes = []
        for b in affected:
            try:
                after = self.payments.hotel_failed(b["booking_ref"], reason)
                self._reserve(b["hotel_id"], b["room_code"], b["check_in"], b["check_out"], rooms=-1)
                self.note_booking(b["booking_ref"])
                outcomes.append({"booking_ref": b["booking_ref"], "status": after["status"],
                                 "outcome": after["history"][-1]["event"]})
            except (AirwallexError, BookingError) as e:
                outcomes.append({"booking_ref": b["booking_ref"], "error": str(e)})
        self.demand.record("incident", self.now(), hotel_id=hotel_id, night=night, reason=reason,
                           bookings=[o["booking_ref"] for o in outcomes])
        after = self.reserves.pct(hotel_id, self.bookings.all())
        self.audit.record("hotel_incident", hotel_id=hotel_id, night=night, reason=reason, outcomes=outcomes)
        return {"hotel_id": hotel_id, "night": night, "reason": reason, "bookings": outcomes,
                "reserve_before": {"pct": str(before[0]), "why": before[1]},
                "reserve_after": {"pct": str(after[0]), "why": after[1]}}

    HOTEL_ACTIONS = ("refresh", "check-out", "no-show", "cancel")

    def hotel_action(self, ref: str, action: str) -> dict:
        """Front-desk actions on a booking: captures here pay the hotel out (bookings.py)."""
        if action not in self.HOTEL_ACTIONS:
            return {"error": f"action must be one of {', '.join(self.HOTEL_ACTIONS)}"}
        if action == "refresh":
            return self.booking_status(ref, tenant=None)
        if action == "cancel":
            return self.cancel_booking(ref, tenant=None)
        try:
            (self.payments.check_out if action == "check-out" else self.payments.no_show)(ref)
        except BookingError as e:
            return {"error": str(e)}
        except AirwallexError as e:
            return {"error": f"Airwallex: {e}"}
        self.note_booking(ref)
        return self.booking_view(ref)

    # ---- live feed (the in-chat negotiation panel polls this) ---------------
    def _emit(self, kind: str, tenant: str, **data) -> None:
        with self._feed_lock:
            self.feed.append({"seq": len(self.feed) + 1, "at": self.now().isoformat(), "kind": kind,
                              "tenant": tenant, **data})

    def note_booking(self, ref: str) -> None:
        self._emit("booking", self.bookings.get(ref).get("tenant", LOCAL), booking=self.booking_view(ref))

    def feed_since(self, since: int = 0, tenant: str = LOCAL) -> dict:
        """This tenant's events after `since`. Every few seconds, also picks up card holds confirmed on the
        checkout page."""
        if time.monotonic() - self._last_refresh > 5:
            self._last_refresh = time.monotonic()
            shown = {e["booking"]["booking_ref"] for e in list(self.feed) if e["kind"] == "booking"}
            for ref in shown:
                b = self.bookings.get(ref)
                if b["method"] == "card" and b["status"] == "awaiting_payment" and b.get("payment_intent_id"):
                    try:
                        self.booking_status(ref, tenant=None)
                    except Exception:   # a slow or failing Airwallex read must not break the live view
                        pass
        with self._feed_lock:
            events = [{k: v for k, v in e.items() if k != "tenant"}
                      for e in self.feed[since:] if e["tenant"] == tenant]
            return {"events": events, "last": len(self.feed)}
