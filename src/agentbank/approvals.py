"""Human approvals bound to exactly what the approver saw.

An approval covers one action with a fixed amount, currency, counterparty, terms and
evidence. If the agent later tries to execute anything different (a new price, another
supplier, an extra fee), the fingerprint no longer matches and it must ask again.
Approvals are single-use and expire.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .money import Money


@dataclass(frozen=True)
class ApprovalRequest:
    action: str                 # e.g. "transfer", "fx_conversion", "card_issue"
    amount: Money
    counterparty: str           # beneficiary id/name, merchant, connected account
    summary: str                # what the human reads
    terms: dict = field(default_factory=dict)
    evidence: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        canonical = json.dumps({
            "action": self.action, "amount": str(self.amount.amount), "currency": self.amount.currency,
            "counterparty": self.counterparty, "terms": self.terms, "evidence": sorted(self.evidence),
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalError(Exception):
    pass


@dataclass
class _Record:
    request: ApprovalRequest
    fingerprint: str
    expires_at: float
    approved_by: str | None = None
    used: bool = False


class Approver(Protocol):
    def ask(self, approval_id: str, request: ApprovalRequest) -> tuple[bool, str]:
        """Return (approved, approver_name)."""


class ConsoleApprover:
    def ask(self, approval_id: str, request: ApprovalRequest) -> tuple[bool, str]:
        print("\n=== APPROVAL NEEDED ===")
        print(f"  {request.action}: {request.amount} -> {request.counterparty}")
        print(f"  {request.summary}")
        for k, v in request.terms.items():
            print(f"  {k}: {v}")
        if request.evidence:
            print(f"  evidence: {', '.join(request.evidence)}")
        answer = input("Approve? [y/N] ").strip().lower()
        return answer == "y", "console-user"


class ApprovalStore:
    def __init__(self, ttl_seconds: int = 15 * 60):
        self.ttl = ttl_seconds
        self._records: dict[str, _Record] = {}

    def request(self, req: ApprovalRequest) -> str:
        approval_id = "apr_" + uuid.uuid4().hex[:12]
        self._records[approval_id] = _Record(req, req.fingerprint(), time.time() + self.ttl)
        return approval_id

    def approve(self, approval_id: str, approver: str) -> None:
        self._get(approval_id).approved_by = approver

    def consume(self, approval_id: str, proposed: ApprovalRequest) -> str:
        """Check the approval covers `proposed` exactly, then burn it. Returns the approver."""
        rec = self._get(approval_id)
        if rec.used:
            raise ApprovalError("approval already used")
        if time.time() > rec.expires_at:
            raise ApprovalError("approval expired; ask again")
        if not rec.approved_by:
            raise ApprovalError("not approved")
        if proposed.fingerprint() != rec.fingerprint:
            raise ApprovalError("terms changed since approval; ask again")
        rec.used = True
        return rec.approved_by

    def _get(self, approval_id: str) -> _Record:
        try:
            return self._records[approval_id]
        except KeyError:
            raise ApprovalError(f"unknown approval {approval_id}") from None
