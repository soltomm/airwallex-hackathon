"""Bookings and their deposits, paid by card hold (Airwallex) or by the traveller's agent (Metal).

Card: booking opens a PaymentIntent for the deposit; the traveller confirms it as a pre-auth
hold on the checkout page. Free cancellation releases the hold, a late one or a no-show
captures it, and check-out raises the hold to the full total and captures that.
Non-refundable bookings are charged in full at booking instead of held.

Payouts: each card payment is the platform's, tagged with the hotel's Airwallex connected
account. After every capture a funds split pays the hotel its share; the platform keeps its
commission (data/hotels.json `platform`).

Metal: the traveller's agent pays the deposit in *USDZ over x402, within the mandate the
traveller bound on chain. A free cancellation leaves a refund due; the balance is settled
at the desk.

The store is a JSON file so the MCP server and the hotel's ops script share it.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .airwallex import AirwallexAPI, AirwallexError, AmbiguousResult
from .audit import AuditLog
from .guest_currency import charged, describe, lock_rate
from .ledger import OperationLedger
from .money import Money
from .offers import Cancellation

CHECK_IN_HOUR = dtime(15, 0, tzinfo=timezone.utc)   # free-cancellation windows count back from here
FREE_HOURS = {Cancellation.FREE_48H: 48, Cancellation.FREE_24H: 24}


class BookingError(Exception):
    pass


class BookingStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def all(self) -> dict[str, dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, ref: str) -> dict:
        try:
            return self.all()[ref]
        except KeyError:
            raise BookingError(f"unknown booking {ref}") from None

    def put(self, booking: dict) -> dict:
        rows = self.all()
        rows[booking["booking_ref"]] = booking
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2))
        tmp.replace(self.path)
        return booking

    def update(self, ref: str, event: str, **fields) -> dict:
        b = self.get(ref)
        b.update(fields)
        b.setdefault("history", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event})
        return self.put(b)


def usdz(amount: Money, rate: Decimal) -> str:
    """Demo conversion to *USDZ at the hotel's configured testnet rate, to the cent."""
    return str((amount.amount * rate).quantize(Decimal("0.01")))


class HotelPayments:
    def __init__(self, store: BookingStore, ledger: OperationLedger, audit: AuditLog,
                 api: AirwallexAPI | None = None, now=lambda: datetime.now(timezone.utc)):
        self.store = store
        self.ledger = ledger
        self.audit = audit
        self.api = api
        self.now = now

    def _api(self) -> AirwallexAPI:
        if self.api is None:
            raise BookingError("Airwallex is not configured (AWX_CLIENT_ID / AWX_API_KEY in .env)")
        return self.api

    @staticmethod
    def _money(b: dict, field: str) -> Money:
        return Money.of(b[field], b["currency"])

    def _log(self, ref: str, event: str, **data) -> None:
        self.audit.record(event, booking_ref=ref, **data)

    # ---- card (Airwallex) ---------------------------------------------------
    def open_card_payment(self, ref: str) -> dict:
        """Create the PaymentIntent the traveller confirms on the checkout page. A definite
        Airwallex failure leaves the booking without one; the next call tries again."""
        b = self.store.get(ref)
        if b.get("payment_intent_id"):
            return self._guest_currency(b)
        charge = self._money(b, "deposit")
        # Tag the intent with the hotel's connected account, except in a guest currency: Airwallex
        # refuses conversion quotes on platform payments, so those stay untagged and are split all the same.
        guest = b.get("guest_currency") and b["guest_currency"] != b["currency"]
        tag = None if guest else b.get("payout", {}).get("connected_account")
        attempt = b.get("payment_attempts", 0)
        key = f"pi:{ref}" if attempt == 0 else f"pi:{ref}:retry-{attempt}"   # a retry is a new operation
        self.store.update(ref, "opening card payment", payment_attempts=attempt + 1)
        try:
            pi = self.ledger.run(key, lambda rid: self._api().create_payment_intent(rid, charge, ref, tag))
        except AirwallexError as e:
            self._log(ref, "card_payment_failed", error=str(e))
            raise BookingError(f"Airwallex could not open the card payment ({e}); the room stays reserved") from e
        self._log(ref, "card_payment_opened", payment_intent_id=pi["id"], amount=str(charge),
                  hold=b["cancellation"] != Cancellation.NON_REFUNDABLE.value)
        return self._guest_currency(self.store.update(ref, "card payment opened", payment_intent_id=pi["id"],
                                                      client_secret=pi.get("client_secret")))

    def _guest_currency(self, b: dict) -> dict:
        """Re-denominate a fresh intent in the guest's currency at a locked rate. If Airwallex
        can't quote, the deposit stays in the hotel's currency and the booking says so."""
        want, ref = b.get("guest_currency"), b["booking_ref"]
        if not want or want == b["currency"] or "fx" in b or b["status"] != "awaiting_payment":
            return b
        try:
            fx = lock_rate(self._api(), self.ledger, ref, b["payment_intent_id"], self._money(b, "total"), want)
        except AirwallexError as e:
            self._log(ref, "guest_currency_failed", currency=want, error=str(e))
            return self.store.update(ref, f"{want} unavailable; deposit stays in {b['currency']}",
                                     fx={"currency": None, "requested": want, "error": str(e)})
        self._log(ref, "guest_currency_locked", **fx)
        return self.store.update(ref, f"rate locked: 1 {b['currency']} = {fx['rate']} {fx['currency']}", fx=fx)

    def refresh(self, ref: str) -> dict:
        """Read the card payment's state from Airwallex and record what changed."""
        b = self.store.get(ref)
        if b["method"] != "card" or not b.get("payment_intent_id") or b["status"] != "awaiting_payment":
            return b
        status = self._api().get_payment_intent(b["payment_intent_id"]).get("status")
        if status == "REQUIRES_CAPTURE":
            self._log(ref, "deposit_held", amount=b["deposit"])
            return self.store.update(ref, f"deposit {describe(b, 'deposit')} held on card", status="deposit_held")
        if status == "SUCCEEDED":
            self._log(ref, "paid", amount=b["deposit"])
            self.store.update(ref, f"charged {describe(b, 'deposit')}", status="paid")
            return self.settle(ref, "deposit")
        return b

    # ---- payouts --------------------------------------------------------------
    def settle(self, ref: str, field: str | None = None) -> dict:
        """Pay the hotel its share of a captured card payment (`field`: "deposit" or "total") by funds
        split, in the currency the card was charged; the commission stays with the platform. Called
        after each capture; call again without `field` (hotel_ops.py settle) to retry."""
        b = self.store.get(ref)
        payout = b.get("payout")
        done = b.get("settlement", {})
        if not payout or b["method"] != "card" or done.get("funds_split_id"):
            return b
        field = field or done.get("field")
        if field is None:
            return b   # nothing captured yet
        pct = Decimal(payout["commission_pct"])

        def split(m: Money) -> tuple[Money, Money]:
            commission = Money.of((m.amount * pct).quantize(Decimal("0.01")), m.currency)
            return commission, m - commission

        commission, to_hotel = split(charged(b, field))      # what moves, in the card's currency
        commission_h, to_hotel_h = split(self._money(b, field))   # the same in the hotel's currency
        settlement = {"field": field, "captured": b[field], "commission": str(commission_h.amount),
                      "hotel_payout": str(to_hotel_h.amount), "connected_account": payout["connected_account"],
                      **({"charged": {"currency": to_hotel.currency, "captured": str(charged(b, field).amount),
                                      "commission": str(commission.amount), "hotel_payout": str(to_hotel.amount)}}
                         if to_hotel.currency != b["currency"] else {})}
        attempt = done.get("attempts", 0)
        key = f"split:{ref}" if attempt == 0 else f"split:{ref}:retry-{attempt}"   # a retry is a new operation
        try:
            made = self.ledger.run(key, lambda rid: self._api().create_funds_split(
                rid, b["payment_intent_id"], to_hotel, payout["connected_account"],
                {"booking_ref": ref, "commission": str(commission)}))
        except (AirwallexError, AmbiguousResult) as e:   # the capture stands; the payout can be retried
            self._log(ref, "payout_failed", error=str(e))
            return self.store.update(ref, f"payout to hotel not sent ({e})",
                                     settlement={**settlement, "attempts": attempt + 1, "error": str(e)[:300]})
        self._log(ref, "hotel_paid_out", funds_split_id=made["id"], **settlement)
        return self.store.update(ref, f"{to_hotel} to the hotel, {commission} platform commission",
                                 settlement={**settlement, "attempts": attempt + 1, "funds_split_id": made["id"],
                                             "split_status": made.get("status")})

    # ---- Metal --------------------------------------------------------------
    def record_metal_payment(self, ref: str, transaction: str, payer: str, amount_usdz: str) -> dict:
        b = self.store.get(ref)
        if b["method"] != "metal" or b["status"] != "awaiting_payment":
            raise BookingError(f"booking {ref} is not awaiting a Metal payment")
        if Decimal(amount_usdz) < Decimal(b["metal"]["usdz"]):
            raise BookingError(f"paid {amount_usdz} *USDZ, expected {b['metal']['usdz']}")
        self._log(ref, "metal_deposit_paid", transaction=transaction, payer=payer, usdz=amount_usdz)
        return self.store.update(ref, f"deposit paid on Metal: {amount_usdz} *USDZ", status="paid",
                                 metal={**b["metal"], "transaction": transaction, "payer": payer})

    # ---- lifecycle ----------------------------------------------------------
    def free_cancellation_until(self, b: dict) -> datetime | None:
        hours = FREE_HOURS.get(Cancellation(b["cancellation"]))
        if hours is None:
            return None
        return datetime.combine(date.fromisoformat(b["check_in"]), CHECK_IN_HOUR) - timedelta(hours=hours)

    def cancel(self, ref: str) -> dict:
        b = self.refresh(ref)
        if b["status"] not in ("awaiting_payment", "deposit_held", "paid"):
            raise BookingError(f"booking {ref} is {b['status']}; nothing to cancel")
        deadline = self.free_cancellation_until(b)
        free = deadline is not None and self.now() <= deadline
        captured = None

        if b["status"] == "awaiting_payment":
            if b.get("payment_intent_id"):
                self.ledger.run(f"cancel:{ref}", lambda rid: self._api().cancel_payment_intent(
                    rid, b["payment_intent_id"], "Booking cancelled before payment"))
            outcome, status = "cancelled before any payment", "cancelled"
        elif free and b["method"] == "card":
            self.ledger.run(f"cancel:{ref}", lambda rid: self._api().cancel_payment_intent(
                rid, b["payment_intent_id"], "Free cancellation"))
            outcome, status = f"free cancellation: hold of {describe(b, 'deposit')} released", "cancelled"
        elif free:   # Metal deposit already paid
            outcome, status = f"free cancellation: refund of {b['metal']['usdz']} *USDZ due to the traveller", "cancelled_refund_due"
        elif b["status"] == "deposit_held":
            self.ledger.run(f"capture:{ref}", lambda rid: self._api().capture_payment_intent(
                rid, b["payment_intent_id"], charged(b, "deposit")))
            outcome, status = f"late cancellation: deposit {describe(b, 'deposit')} captured", "cancelled_fee_charged"
            captured = "deposit"
        else:
            outcome, status = "late or non-refundable cancellation: payment kept", "cancelled_fee_charged"
        self._log(ref, "booking_cancelled", outcome=outcome,
                  free_until=deadline.isoformat() if deadline else None)
        self.store.update(ref, outcome, status=status)
        return self.settle(ref, captured) if captured else self.store.get(ref)

    def check_out(self, ref: str) -> dict:
        b = self.refresh(ref)
        total = charged(b, "total")
        captured = None
        if b["status"] == "deposit_held":
            if charged(b, "deposit") < total:
                self.ledger.run(f"increment:{ref}", lambda rid: self._api().increment_authorization(
                    rid, b["payment_intent_id"], total))
            self.ledger.run(f"capture:{ref}", lambda rid: self._api().capture_payment_intent(
                rid, b["payment_intent_id"], total))
            outcome = f"hold raised to {describe(b, 'total')} and captured"
            captured = "total"
        elif b["status"] == "paid" and b["method"] == "card":
            outcome = f"already charged {describe(b, 'deposit')} at booking"
        elif b["status"] == "paid":
            balance = self._money(b, "total") - self._money(b, "deposit")
            outcome = f"Metal deposit kept; balance {balance} due at the desk"
        else:
            raise BookingError(f"booking {ref} is {b['status']}; cannot check out")
        self._log(ref, "checked_out", outcome=outcome)
        self.store.update(ref, outcome, status="checked_out")
        return self.settle(ref, captured) if captured else self.store.get(ref)

    def no_show(self, ref: str) -> dict:
        b = self.refresh(ref)
        captured = None
        if b["status"] == "deposit_held":
            self.ledger.run(f"capture:{ref}", lambda rid: self._api().capture_payment_intent(
                rid, b["payment_intent_id"], charged(b, "deposit")))
            outcome = f"no-show: deposit {describe(b, 'deposit')} captured"
            captured = "deposit"
        elif b["status"] == "paid":
            outcome = "no-show: payment kept"
        else:
            raise BookingError(f"booking {ref} is {b['status']}; nothing to charge")
        self._log(ref, "no_show", outcome=outcome)
        self.store.update(ref, outcome, status="no_show")
        return self.settle(ref, captured) if captured else self.store.get(ref)
