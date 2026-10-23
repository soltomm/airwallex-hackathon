"""The hotels' side of the marketplace, as plain functions a buyer agent can call.

search -> negotiate (with that hotel's seller agent) -> book an offer the hotel committed to.
Only offers the policy let the seller commit get an offer_id, and booking takes nothing
but that id, so the terms the traveller confirmed are exactly the terms that get booked.
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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
_PRIVATE = ("client_secret", "approval_fingerprint", "history")   # kept out of buyer-facing views


class OfferDesk:
    def __init__(self, hotels: dict[str, Hotel], audit: AuditLog, ledger: OperationLedger,
                 bookings: BookingStore, api: AirwallexAPI | None = None, client=None,
                 pay_base_url: str = "http://127.0.0.1:8009", metal_base_url: str = "http://127.0.0.1:4022",
                 now=lambda: datetime.now(timezone.utc)):
        self.hotels = hotels
        self.audit = audit
        self.ledger = ledger
        self.bookings = bookings
        self.payments = HotelPayments(bookings, ledger, audit, api, now=now)
        self.client = client                     # Anthropic client for seller agents (lazy if None)
        self.pay_base_url = pay_base_url         # card checkout pages (scripts/hotel_mcp.py serves them)
        self.metal_base_url = metal_base_url     # x402 deposit endpoint (metal/hotel-pay.ts)
        self.now = now
        self.approvals = ApprovalStore(ttl_seconds=int(OFFER_TTL.total_seconds()))
        self.sellers: dict[str, SellerAgent] = {}   # negotiation_id -> that hotel's seller agent
        self.offers: dict[str, Offer] = {}          # committed offers, bookable by id
        for b in bookings.all().values():           # rooms already sold survive restarts
            if b["status"] not in ("cancelled", "cancelled_refund_due"):
                self._reserve(b["hotel_id"], b["room_code"], b["check_in"], b["check_out"])

    @classmethod
    def from_files(cls, root: Path, api: AirwallexAPI | None = None) -> "OfferDesk":
        return cls(load_hotels(root / "data/hotels.json"), AuditLog(root / "var/audit.jsonl"),
                   OperationLedger(root / "var/ledger.sqlite3"), BookingStore(root / "var/bookings.json"), api)

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

    def commit(self, offer: Offer) -> str:
        offer_id = "ofr_" + uuid.uuid4().hex[:10]
        self.offers[offer_id] = offer
        return offer_id

    def search(self, check_in: str, check_out: str, guests: int, extras: list[str],
               cancellation: str) -> list[dict]:
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
        self.audit.record("search", check_in=check_in, check_out=check_out, guests=guests, extras=extras,
                          cancellation=cancellation, results=len(results))
        return results

    def negotiate(self, hotel_id: str, message: str, negotiation_id: str | None = None) -> dict:
        """Send the buyer's message to the hotel's seller agent; continue a negotiation by id."""
        if negotiation_id:
            seller = self.sellers.get(negotiation_id)
            if seller is None or seller.hotel.id != hotel_id:
                return {"error": f"unknown negotiation {negotiation_id} for {hotel_id}"}
        elif hotel_id not in self.hotels:
            return {"error": f"unknown hotel {hotel_id}"}
        else:
            negotiation_id = "neg_" + uuid.uuid4().hex[:10]
            seller = self.sellers[negotiation_id] = SellerAgent(self.hotels[hotel_id], self.commit, self.audit,
                                                                client=self.client, now=self.now)
        reply = seller.reply(message)
        return {"negotiation_id": negotiation_id, "round": seller.buyer_turns, "seller_activity": reply["activity"],
                "seller_message": reply["message"], "offers": [self.view(i) for i in reply["offer_ids"]]}

    def book(self, offer_id: str, traveller_confirmed: bool, payment_method: str = "card") -> dict:
        if not traveller_confirmed:
            return {"result": "not_booked", "reason": "show the traveller the exact terms and get a yes first"}
        if payment_method not in PAYMENT_METHODS:
            return {"result": "not_booked", "reason": f"payment_method must be one of {', '.join(PAYMENT_METHODS)}"}
        offer = self.offers.get(offer_id)
        if offer is None:
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
                return {"result": "booked_payment_unavailable", "reason": str(e), **self.booking_view(ref)}
        return {"result": "booked", **self.booking_view(ref)}

    def _booked_before(self, op) -> dict:
        if op.result:
            return {"result": "already_booked", **self.booking_view(op.result["booking_ref"])}
        return {"result": "booking_in_progress", "reason": "this offer is being booked; check again shortly"}

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

    def booking_status(self, ref: str) -> dict:
        try:
            b = self.payments.refresh(ref)
        except BookingError as e:
            return {"error": str(e)}
        if b["method"] == "card" and b["status"] == "awaiting_payment" and not b.get("payment_intent_id"):
            try:
                self.payments.open_card_payment(ref)
            except BookingError as e:
                return {**self.booking_view(ref), "payment_error": str(e)}
        return self.booking_view(ref)

    def cancel_booking(self, ref: str) -> dict:
        try:
            b = self.payments.cancel(ref)
        except BookingError as e:
            return {"error": str(e)}
        if b["status"] in ("cancelled", "cancelled_refund_due"):
            self._reserve(b["hotel_id"], b["room_code"], b["check_in"], b["check_out"], rooms=-1)
        return self.booking_view(ref)
