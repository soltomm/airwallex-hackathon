"""Normalise Airwallex object states into the few outcomes your decision logic needs.

Verified against the sandbox on 2026-10-19:
- A failed transfer ends CANCELLED (never FAILED on read-back), with details nested under
  `failure: {code, message, details: {type}}`. The simulator's FAILED response can carry a
  different failure type from the one you asked for, so key on code/message, not your input.
- SENT is in flight, not final.
- A *declined* card transaction also has transaction_type CLEARING. Decide on
  status/failure_reason, never on transaction_type alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TransferState(str, Enum):
    PENDING = "pending"      # created, scheduled, processing, overdue
    IN_FLIGHT = "in_flight"  # SENT: may still pay or bounce
    PAID = "paid"
    FAILED = "failed"        # CANCELLED / FAILED: read failure for why


_TRANSFER_MAP = {
    "PAID": TransferState.PAID,
    "SENT": TransferState.IN_FLIGHT,
    "CANCELLED": TransferState.FAILED,
    "FAILED": TransferState.FAILED,
}


@dataclass(frozen=True)
class Failure:
    code: str | None
    type: str | None
    message: str | None


def transfer_state(transfer: dict) -> TransferState:
    return _TRANSFER_MAP.get(str(transfer.get("status", "")).upper(), TransferState.PENDING)


def transfer_is_final(transfer: dict) -> bool:
    return transfer_state(transfer) in (TransferState.PAID, TransferState.FAILED)


def transfer_failure(transfer: dict) -> Failure | None:
    if transfer_state(transfer) is not TransferState.FAILED:
        return None
    f = transfer.get("failure") or {}
    return Failure(
        code=f.get("code"),
        type=(f.get("details") or {}).get("type") or transfer.get("failure_type"),
        message=f.get("message") or transfer.get("failure_reason"),
    )


@dataclass(frozen=True)
class CardOutcome:
    accepted: bool
    decline_reason: str | None
    settled: bool  # False while an accepted transaction is still PENDING


def card_outcome(txn: dict) -> CardOutcome:
    status = str(txn.get("status", "")).upper()
    reason = txn.get("failure_reason")
    if status == "FAILED" or reason:
        return CardOutcome(False, reason or "UNKNOWN", False)
    return CardOutcome(True, None, status in ("APPROVED", "CLEARED", "SETTLED"))
