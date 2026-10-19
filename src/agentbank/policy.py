"""Deterministic money rules. The model proposes; this code decides what is allowed.

Keep thresholds here, not in prompts. Replace the example rules with your kit's policy
(reserve floors, forecast confidence, per-tenant isolation, dispute economics...).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .money import Money


class Verdict(str, Enum):
    EXECUTE = "execute"                  # agent may act alone
    NEEDS_APPROVAL = "needs_approval"    # a person must approve these exact terms
    REJECT = "reject"                    # not allowed, say why


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.verdict is not Verdict.REJECT


# Flat SWIFT payout fee from the same wallet (LOCAL payouts are free). Sandbox, Oct 2026.
SWIFT_FEES = {"EUR": Money.of("12.85", "EUR")}


def payout_fee(amount: Money, method: str) -> Money:
    if method == "LOCAL":
        return Money.of(0, amount.currency)
    return SWIFT_FEES.get(amount.currency, Money.of(0, amount.currency))


@dataclass
class PaymentPolicy:
    reserve_floor: dict[str, Money] = field(default_factory=dict)  # never dip below
    autonomous_limit: dict[str, Money] = field(default_factory=dict)  # above this, ask a person
    allowed_methods: tuple[str, ...] = ("LOCAL", "SWIFT")
    confidence_floor: Decimal = Decimal("0.8")  # below this, every payment needs approval

    def evaluate_payout(self, amount: Money, method: str, available: Money,
                        confidence: Decimal = Decimal("1")) -> Decision:
        reasons: list[str] = []
        if method not in self.allowed_methods:
            return Decision(Verdict.REJECT, (f"method {method} not allowed",))
        if amount.amount <= 0:
            return Decision(Verdict.REJECT, ("amount must be positive",))

        total = amount + payout_fee(amount, method)
        floor = self.reserve_floor.get(amount.currency, Money.of(0, amount.currency))
        after = available - total
        if after < floor:
            return Decision(Verdict.REJECT, (
                f"would leave {after} after fees, below reserve floor {floor}",))
        reasons.append(f"leaves {after} (floor {floor})")

        limit = self.autonomous_limit.get(amount.currency)
        if limit is not None and total > limit:
            return Decision(Verdict.NEEDS_APPROVAL, (*reasons, f"{total} exceeds autonomous limit {limit}"))
        if confidence < self.confidence_floor:
            return Decision(Verdict.NEEDS_APPROVAL, (*reasons, f"forecast confidence {confidence} below {self.confidence_floor}"))
        return Decision(Verdict.EXECUTE, tuple(reasons))
