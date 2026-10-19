"""Every sandbox-only call lives behind this one interface, so the decision logic never
calls a simulation endpoint directly and you can swap this out without touching it."""
from __future__ import annotations

from .airwallex.client import AirwallexClient, utcnow_iso
from .money import Money


class SandboxSimulator:
    def __init__(self, client: AirwallexClient):
        self.c = client

    def deposit(self, global_account_id: str, amount: float, payer_name: str = "Simulated payer", **kw) -> dict:
        """Lands in the Global Account's currency (the body takes no currency field).
        The response says PENDING; the balance updates within a few seconds. Re-read it."""
        return self.c.post("/simulation/deposit/create", {
            "global_account_id": global_account_id, "amount": amount, "payer_name": payer_name}, **kw)

    def transfer_to(self, transfer_id: str, next_status: str, failure_type: str | None = None, **kw) -> dict:
        """Move a transfer along: SENT, then PAID or FAILED. A failure_type applies only after SENT."""
        body = {"next_status": next_status}
        if failure_type:
            body["failure_type"] = failure_type
        return self.c.post(f"/simulation/transfers/{transfer_id}/transition", body, **kw)

    def card_transaction(self, card_id: str, amount: Money, merchant_category_code: str | None = None, **kw) -> dict:
        body = {"card_id": card_id, "transaction_amount": amount.api(),
                "transaction_currency": amount.currency}
        if merchant_category_code:
            body["merchant_category_code"] = merchant_category_code
        return self.c.post("/simulation/issuing/create", body, **kw)

    def dispute(self, payment_intent_id: str, reason_code: str, stage: str = "RFI",
                due_at: str | None = None, amount: float | None = None) -> dict:
        body = {"payment_intent_id": payment_intent_id, "reason_code": reason_code,
                "stage": stage, "due_at": due_at or utcnow_iso()}
        if amount is not None:
            body["amount"] = amount
        return self.c.post("/simulation/pa/payment_disputes/create", body)

    def escalate_dispute(self, dispute_id: str) -> dict:
        return self.c.post(f"/simulation/pa/payment_disputes/{dispute_id}/escalate", {})
