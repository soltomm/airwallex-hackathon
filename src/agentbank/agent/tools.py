"""Typed tools the model may call. The model never holds credentials or builds raw
payloads: every money-moving tool goes policy -> (approval) -> ledger -> API.

Add your kit's tools here following the same shape: a strict JSON schema plus a
handler that returns a JSON-serialisable dict.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable

from ..airwallex import AirwallexAPI, AirwallexError, AmbiguousResult
from ..airwallex.states import transfer_failure, transfer_state
from ..approvals import ApprovalError, ApprovalRequest, ApprovalStore, Approver
from ..audit import AuditLog
from ..ledger import DuplicateOperation, OperationLedger
from ..money import Money
from ..policy import PaymentPolicy, Verdict, payout_fee


@dataclass
class AgentContext:
    api: AirwallexAPI
    policy: PaymentPolicy
    approvals: ApprovalStore
    approver: Approver
    ledger: OperationLedger
    audit: AuditLog


class ToolInputError(ValueError):
    pass


def _money(amount: str, currency: str) -> Money:
    try:
        return Money.of(Decimal(amount), currency)
    except (InvalidOperation, ValueError):
        raise ToolInputError(f"invalid amount {amount!r}") from None


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


_CUR = {"type": "string", "description": "ISO 4217 code, e.g. USD"}
_AMT = {"type": "string", "description": "Decimal amount in major units, e.g. '1250.00' (not cents)"}

TOOLS: list[dict] = [
    {
        "name": "get_balances",
        "description": "Read available, pending and total wallet balances for every currency.",
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "get_fx_quote",
        "description": "Get a firm, single-use FX quote for buying an amount of one currency with another. Read-only.",
        "strict": True,
        "input_schema": _schema({"sell_currency": _CUR, "buy_currency": _CUR, "buy_amount": _AMT}),
    },
    {
        "name": "pay_beneficiary",
        "description": (
            "Propose paying an existing beneficiary. Policy code decides whether it runs, needs a "
            "person's approval, or is rejected. Paying the same invoice_ref twice is refused."
        ),
        "strict": True,
        "input_schema": _schema({
            "beneficiary_id": {"type": "string"},
            "beneficiary_name": {"type": "string", "description": "Shown to the approver"},
            "amount": _AMT,
            "currency": _CUR,
            "source_currency": _CUR,
            "transfer_method": {"type": "string", "enum": ["LOCAL", "SWIFT"]},
            "invoice_ref": {"type": "string", "description": "Unique business reference; dedupe key"},
            "justification": {"type": "string", "description": "Why this payment, now"},
            "evidence": {"type": "array", "items": {"type": "string"},
                         "description": "Ids of the documents/facts relied on"},
        }),
    },
    {
        "name": "check_transfer",
        "description": "Read a transfer's normalised state: pending, in_flight (SENT), paid or failed (with reason).",
        "strict": True,
        "input_schema": _schema({"transfer_id": {"type": "string"}}),
    },
    {
        "name": "escalate_to_human",
        "description": "Stop and hand a decision to a person when policy, evidence or risk is unclear.",
        "strict": True,
        "input_schema": _schema({
            "summary": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        }),
    },
]


# ---- handlers --------------------------------------------------------------
def get_balances(ctx: AgentContext) -> dict:
    rows = ctx.api.balances()
    return {cur: {k: r.get(k) for k in ("available_amount", "pending_amount", "total_amount")}
            for cur, r in rows.items() if r.get("total_amount")}


def get_fx_quote(ctx: AgentContext, sell_currency: str, buy_currency: str, buy_amount: str) -> dict:
    q = ctx.api.fx_quote(sell_currency, _money(buy_amount, buy_currency))
    return {k: q.get(k) for k in ("quote_id", "client_rate", "buy_amount", "sell_amount", "valid_to_at")}


def pay_beneficiary(ctx: AgentContext, *, beneficiary_id: str, beneficiary_name: str, amount: str,
                    currency: str, source_currency: str, transfer_method: str, invoice_ref: str,
                    justification: str, evidence: list[str]) -> dict:
    money = _money(amount, currency)
    if source_currency.upper() != money.currency:
        return {"status": "rejected", "reasons": ["convert first: source and transfer currency must match"]}

    key = f"transfer:{invoice_ref}"
    if (op := ctx.ledger.get(key)) and op.status.value != "started":
        prior = op.result or {}
        return {"status": "already_handled", "ledger_status": op.status.value,
                "transfer_id": prior.get("id"), "transfer_status": prior.get("status")}

    decision = ctx.policy.evaluate_payout(money, transfer_method, ctx.api.available(money.currency))
    ctx.audit.record("policy_decision", key=key, verdict=decision.verdict.value, reasons=decision.reasons)
    if decision.verdict is Verdict.REJECT:
        return {"status": "rejected", "reasons": list(decision.reasons)}

    if decision.verdict is Verdict.NEEDS_APPROVAL:
        req = ApprovalRequest(
            action="transfer", amount=money, counterparty=f"{beneficiary_name} ({beneficiary_id})",
            summary=justification,
            terms={"method": transfer_method, "fee": str(payout_fee(money, transfer_method)),
                   "invoice_ref": invoice_ref},
            evidence=tuple(evidence),
        )
        approval_id = ctx.approvals.request(req)
        ok, who = ctx.approver.ask(approval_id, req)
        ctx.audit.record("approval", approval_id=approval_id, approved=ok, approver=who, fingerprint=req.fingerprint())
        if not ok:
            return {"status": "declined_by_human", "approval_id": approval_id}
        ctx.approvals.approve(approval_id, who)
        try:
            ctx.approvals.consume(approval_id, req)  # re-checks terms right before execution
        except ApprovalError as e:
            return {"status": "approval_invalid", "reason": str(e)}

    def send(request_id: str) -> dict:
        return ctx.api.create_transfer(request_id, beneficiary_id, money, source_currency=source_currency,
                                       transfer_method=transfer_method, reference=invoice_ref)

    try:
        t = ctx.ledger.run(key, send)
    except DuplicateOperation as e:
        return {"status": "already_handled", "ledger_status": e.op.status.value}
    except AmbiguousResult as e:
        ctx.audit.record("ambiguous", key=key, request_id=e.request_id)
        return {"status": "unknown", "note": "outcome unknown; reconcile by request_id before any retry"}
    ctx.audit.record("transfer_created", key=key, transfer_id=t.get("id"), status=t.get("status"))
    return {"status": "submitted", "transfer_id": t.get("id"), "transfer_status": t.get("status"),
            "fee": t.get("fee_amount"), "policy": list(decision.reasons)}


def check_transfer(ctx: AgentContext, transfer_id: str) -> dict:
    t = ctx.api.get_transfer(transfer_id)
    f = transfer_failure(t)
    return {"state": transfer_state(t).value, "raw_status": t.get("status"),
            "failure": f.__dict__ if f else None}


def escalate_to_human(ctx: AgentContext, summary: str, options: list[str]) -> dict:
    ctx.audit.record("escalation", summary=summary, options=options)
    return {"status": "escalated", "note": "A person will decide. Stop acting on this item."}


HANDLERS: dict[str, Callable[..., dict]] = {
    "get_balances": get_balances,
    "get_fx_quote": get_fx_quote,
    "pay_beneficiary": pay_beneficiary,
    "check_transfer": check_transfer,
    "escalate_to_human": escalate_to_human,
}


def run_tool(ctx: AgentContext, name: str, tool_input: dict) -> tuple[str, bool]:
    """Returns (json_result, is_error)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool {name}"}), True
    try:
        result = handler(ctx, **tool_input)
        ctx.audit.record("tool", name=name, input=tool_input, result=result)
        return json.dumps(result, default=str), False
    except (ToolInputError, TypeError) as e:
        return json.dumps({"error": f"bad input: {e}"}), True
    except AirwallexError as e:
        ctx.audit.record("tool_error", name=name, code=e.code, message=e.message)
        return json.dumps({"error": e.code, "message": e.message,
                           "insufficient_funds": e.insufficient_funds}), True
