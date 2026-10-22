"""Bookings and their deposits, paid by card hold (Airwallex) or by the traveller's agent (Metal).

Card: booking opens a PaymentIntent for the deposit; the traveller confirms it as a pre-auth
hold on the checkout page. Free cancellation releases the hold, a late one or a no-show
captures it, and check-out raises the hold to the full total and captures that.
Non-refundable bookings are charged in full at booking instead of held.

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

from .airwallex import AirwallexAPI
from .audit import AuditLog
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
        """Create the PaymentIntent the traveller confirms on the checkout page."""
        b = self.store.get(ref)
        charge = self._money(b, "deposit")
        pi = self.ledger.run(f"pi:{ref}", lambda rid: self._api().create_payment_intent(rid, charge, ref))
        self._log(ref, "card_payment_opened", payment_intent_id=pi["id"], amount=str(charge),
                  hold=b["cancellation"] != Cancellation.NON_REFUNDABLE.value)
        return self.store.update(ref, "card payment opened", payment_intent_id=pi["id"],
                                 client_secret=pi.get("client_secret"))

    def refresh(self, ref: str) -> dict:
        """Read the card payment's state from Airwallex and record what changed."""
        b = self.store.get(ref)
        if b["method"] != "card" or not b.get("payment_intent_id") or b["status"] != "awaiting_payment":
            return b
        status = self._api().get_payment_intent(b["payment_intent_id"]).get("status")
        if status == "REQUIRES_CAPTURE":
            self._log(ref, "deposit_held", amount=b["deposit"])
            return self.store.update(ref, f"deposit {b['deposit']} {b['currency']} held on card", status="deposit_held")
        if status == "SUCCEEDED":
            self._log(ref, "paid", amount=b["deposit"])
            return self.store.update(ref, f"charged {b['deposit']} {b['currency']}", status="paid")
        return b

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

        if b["status"] == "awaiting_payment":
            if b.get("payment_intent_id"):
                self.ledger.run(f"cancel:{ref}", lambda rid: self._api().cancel_payment_intent(
                    rid, b["payment_intent_id"], "Booking cancelled before payment"))
            outcome, status = "cancelled before any payment", "cancelled"
        elif free and b["method"] == "card":
            self.ledger.run(f"cancel:{ref}", lambda rid: self._api().cancel_payment_intent(
                rid, b["payment_intent_id"], "Free cancellation"))
            outcome, status = f"free cancellation: hold of {b['deposit']} {b['currency']} released", "cancelled"
        elif free:   # Metal deposit already paid
            outcome, status = f"free cancellation: refund of {b['metal']['usdz']} *USDZ due to the traveller", "cancelled_refund_due"
        elif b["status"] == "deposit_held":
            self.ledger.run(f"capture:{ref}", lambda rid: self._api().capture_payment_intent(
                rid, b["payment_intent_id"], self._money(b, "deposit")))
            outcome, status = f"late cancellation: deposit {b['deposit']} {b['currency']} captured", "cancelled_fee_charged"
        else:
            outcome, status = "late or non-refundable cancellation: payment kept", "cancelled_fee_charged"
        self._log(ref, "booking_cancelled", outcome=outcome,
                  free_until=deadline.isoformat() if deadline else None)
        return self.store.update(ref, outcome, status=status)

    def check_out(self, ref: str) -> dict:
        b = self.refresh(ref)
        total = self._money(b, "total")
        if b["status"] == "deposit_held":
            if self._money(b, "deposit") < total:
                self.ledger.run(f"increment:{ref}", lambda rid: self._api().increment_authorization(
                    rid, b["payment_intent_id"], total))
            self.ledger.run(f"capture:{ref}", lambda rid: self._api().capture_payment_intent(
                rid, b["payment_intent_id"], total))
            outcome = f"hold raised to {total} and captured"
        elif b["status"] == "paid" and b["method"] == "card":
            outcome = f"already charged {b['deposit']} {b['currency']} at booking"
        elif b["status"] == "paid":
            balance = total - self._money(b, "deposit")
            outcome = f"Metal deposit kept; balance {balance} due at the desk"
        else:
            raise BookingError(f"booking {ref} is {b['status']}; cannot check out")
        self._log(ref, "checked_out", outcome=outcome)
        return self.store.update(ref, outcome, status="checked_out")

    def no_show(self, ref: str) -> dict:
        b = self.refresh(ref)
        if b["status"] == "deposit_held":
            self.ledger.run(f"capture:{ref}", lambda rid: self._api().capture_payment_intent(
                rid, b["payment_intent_id"], self._money(b, "deposit")))
            outcome = f"no-show: deposit {b['deposit']} {b['currency']} captured"
        elif b["status"] == "paid":
            outcome = "no-show: payment kept"
        else:
            raise BookingError(f"booking {ref} is {b['status']}; nothing to charge")
        self._log(ref, "no_show", outcome=outcome)
        return self.store.update(ref, outcome, status="no_show")
