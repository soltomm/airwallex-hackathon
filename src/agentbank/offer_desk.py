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
from decimal import Decimal
from pathlib import Path

from .airwallex import AirwallexAPI
from .approvals import ApprovalStore
from .audit import AuditLog
from .bookings import BookingError, BookingStore, HotelPayments, usdz
from .ledger import DuplicateOperation, OperationLedger
from .money import Money
from .offers import OFFER_TTL, Cancellation, Hotel, Offer, SellerPolicy, load_hotels
from .policy import Verdict
from .seller_agent import SellerAgent

PAYMENT_METHODS = ("card", "metal")
LOCAL = "local"
_PRIVATE = ("client_secret", "approval_fingerprint", "history", "tenant")   # kept out of buyer-facing views
_PRICE_IN, _PRICE_OUT = Decimal("4") / 1_000_000, Decimal("20") / 1_000_000   # Claude Opus 5.5, $ per token


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
                 now=lambda: datetime.now(timezone.utc)):
        self.hotels = hotels
        self.audit = audit
        self.ledger = ledger
        self.bookings = bookings
        self.payments = HotelPayments(bookings, ledger, audit, api, now=now)
        self.client = client                     # Anthropic client for seller agents (lazy if None)
        self.pay_base_url = pay_base_url         # card checkout pages (scripts/hotel_mcp.py serves them)
        self.metal_base_url = metal_base_url     # x402 deposit endpoint (metal/hotel-pay.ts)
        self.payment_methods = payment_methods
        self.limits = limits or Limits()
        self.now = now
        self.approvals = ApprovalStore(ttl_seconds=int(OFFER_TTL.total_seconds()))
        self.sellers: dict[str, SellerAgent] = {}   # negotiation_id -> that hotel's seller agent
        self.seller_tenant: dict[str, str] = {}     # negotiation_id -> tenant
        self.offers: dict[str, Offer] = {}          # committed offers, bookable by id
        self.offer_tenant: dict[str, str] = {}      # offer_id -> tenant
        self.turns: dict[str, int] = {}             # tenant -> buyer turns sent to sellers
        self.spend: dict[str, Decimal] = {}         # ISO day -> estimated seller spend in $
        self.feed: list[dict] = []                  # what live views show; never the hotel's floors
        self._feed_lock = threading.Lock()
        self._last_refresh = 0.0
        for b in bookings.all().values():           # rooms already sold survive restarts
            if b["status"] not in ("cancelled", "cancelled_refund_due"):
                self._reserve(b["hotel_id"], b["room_code"], b["check_in"], b["check_out"])

    @classmethod
    def from_files(cls, root: Path, api: AirwallexAPI | None = None, **kw) -> "OfferDesk":
        return cls(load_hotels(root / "data/hotels.json"), AuditLog(root / "var/audit.jsonl"),
                   OperationLedger(root / "var/ledger.sqlite3"), BookingStore(root / "var/bookings.json"), api, **kw)

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

    def commit(self, offer: Offer, tenant: str = LOCAL) -> str:
        offer_id = "ofr_" + uuid.uuid4().hex[:10]
        self.offers[offer_id] = offer
        self.offer_tenant[offer_id] = tenant
        return offer_id

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
                self.hotels[hotel_id], lambda offer: self.commit(offer, tenant), self.audit, client=self.client,
                now=self.now,
                on_activity=lambda text, nid=negotiation_id: self._emit("activity", tenant, negotiation_id=nid,
                                                                         text=text))
            self.seller_tenant[negotiation_id] = tenant
        hotel = self.hotels[hotel_id].name
        self._emit("buyer", tenant, negotiation_id=negotiation_id, hotel_id=hotel_id, hotel=hotel,
                   round=seller.buyer_turns + 1, message=message)
        before = dict(seller.usage)
        reply = seller.reply(message)
        self.turns[tenant] = self.turns.get(tenant, 0) + 1
        cost = ((seller.usage["input"] - before["input"]) * _PRICE_IN +
                (seller.usage["output"] - before["output"]) * _PRICE_OUT)
        day = self.now().date().isoformat()
        self.spend[day] = self.spend.get(day, Decimal(0)) + cost
        offers = [self.view(i) for i in reply["offer_ids"]]
        self._emit("seller", tenant, negotiation_id=negotiation_id, hotel_id=hotel_id, hotel=hotel,
                   round=seller.buyer_turns, message=reply["message"], offers=offers)
        return {"negotiation_id": negotiation_id, "round": seller.buyer_turns, "seller_activity": reply["activity"],
                "seller_message": reply["message"], "offers": offers}

    def book(self, offer_id: str, traveller_confirmed: bool, payment_method: str = "card",
             tenant: str = LOCAL) -> dict:
        if not traveller_confirmed:
            return {"result": "not_booked", "reason": "show the traveller the exact terms and get a yes first"}
        if payment_method not in self.payment_methods:
            return {"result": "not_booked",
                    "reason": f"payment_method must be one of {', '.join(self.payment_methods)} on this server"}
        offer = self.offers.get(offer_id)
        if offer is None or self.offer_tenant.get(offer_id) != tenant:
            return {"result": "not_booked", "reason": f"unknown offer {offer_id}"}
        done = self.ledger.get(f"booking:{offer_id}")
        if done is not None:
            return self._booked_before(done)
        h = self.hotels[offer.hotel_id]
        d = self._policy(offer.hotel_id).evaluate(offer)   # inventory or the clock may have moved
        if d.verdict is not Verdict.EXECUTE:
            return {"result": "not_booked", "reason": "; ".join(d.public)}
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
                **({"metal": {"usdz": usdz(offer.deposit, h.usdz_per_eur), "pay_to": h.metal_payee}}
                   if payment_method == "metal" else {})})
            return {"booking_ref": ref}

        try:
            ref = self.ledger.run(f"booking:{offer_id}", reserve)["booking_ref"]
        except DuplicateOperation as e:   # a concurrent call for the same offer got there first
            return self._booked_before(e.op)
        self.audit.record("booking_confirmed", booking_ref=ref, offer_id=offer_id, method=payment_method,
                          approval_fingerprint=approval.fingerprint())
        if payment_method == "card":
            try:
                self.payments.open_card_payment(ref)
            except BookingError as e:
                self.note_booking(ref)
                return {"result": "booked_payment_unavailable", "reason": str(e), **self.booking_view(ref)}
        self.note_booking(ref)
        return {"result": "booked", **self.booking_view(ref)}

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
                                     f"{'authorises a hold of' if hold else 'pays'} {b['deposit']} {b['currency']} by card")
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
