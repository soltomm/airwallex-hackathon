"""PayeeCheck: is it safe to pay this beneficiary?

Two checks, combined into PASS / REVIEW / FAIL:
- Bank details: Airwallex `POST /beneficiaries/validate` (routing codes, sort codes, IBAN
  checksums), plus a local SWIFT-code check, because validate accepts an unknown SWIFT code.
- Sanctions: the payee's name against the US Treasury OFAC SDN list
  (scripts/fetch_sanctions.py downloads it to data/sanctions/sdn.csv).

REVIEW means a person (or the model, reading the evidence) decides; code never auto-passes it.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path

from .airwallex import AirwallexAPI, AirwallexError

# Airwallex validation codes seen in the sandbox, mapped to plain words.
BANK_ERROR_CODES = {
    "083": "IBAN fails its checksum",
    "010": "routing code not recognised",
    "011": "sort code or routing type not recognised",
    "016": "bank account category must be exactly Checking or Savings",
}
SWIFT_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
_SUFFIXES = {"LTD", "LIMITED", "LLC", "INC", "INCORPORATED", "CO", "COMPANY", "CORP", "CORPORATION",
             "GMBH", "AG", "SA", "SAS", "SRL", "SPA", "BV", "NV", "PLC", "LLP", "PTE", "PTY", "THE"}


class Verdict(str, Enum):
    PASS = "pass"
    REVIEW = "review"
    FAIL = "fail"


@dataclass(frozen=True)
class SanctionsHit:
    uid: str
    name: str
    program: str
    score: float  # 1.0 = same name after normalisation


@dataclass
class PayeeResult:
    verdict: Verdict
    bank_problems: list[str] = field(default_factory=list)
    bank_warnings: list[str] = field(default_factory=list)
    sanctions_hits: list[SanctionsHit] = field(default_factory=list)

    def summary(self) -> str:
        parts = [self.verdict.value.upper()]
        parts += [f"bank: {p}" for p in self.bank_problems]
        parts += [f"note: {w}" for w in self.bank_warnings]
        parts += [f"sanctions: {h.name} ({h.program}, score {h.score:.2f})" for h in self.sanctions_hits]
        return " | ".join(parts)


def normalise(name: str) -> str:
    tokens = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    return " ".join(t for t in tokens if t not in _SUFFIXES)


class SanctionsList:
    def __init__(self, entries: list[tuple[str, str, str]]):
        """entries: (uid, name, program)."""
        self.entries = [(uid, name, program, normalise(name)) for uid, name, program in entries]
        # Index by 4-letter word prefixes so spelling variants ("Caribean", "Airline") still meet.
        self._by_prefix: dict[str, set[int]] = {}
        for i, (*_, norm) in enumerate(self.entries):
            for tok in norm.split():
                if len(tok) >= 3:
                    self._by_prefix.setdefault(tok[:4], set()).add(i)

    @classmethod
    def load_ofac_sdn(cls, path: str | Path) -> "SanctionsList":
        rows = []
        with open(path, newline="", encoding="latin-1") as f:
            for row in csv.reader(f):
                if len(row) >= 4 and row[0].strip().isdigit():
                    rows.append((row[0].strip(), row[1].strip(), row[3].strip()))
        return cls(rows)

    def screen(self, name: str, review_at: float = 0.88, limit: int = 3) -> list[SanctionsHit]:
        query = normalise(name)
        candidates = {i for tok in query.split() if len(tok) >= 3 for i in self._by_prefix.get(tok[:4], ())}
        hits = []
        for i in candidates:
            uid, raw, program, norm = self.entries[i]
            # Compare with and without spaces: "AERO CARIBEAN" vs "AEROCARIBBEAN".
            score = 1.0 if norm == query else max(
                SequenceMatcher(None, query, norm).ratio(),
                SequenceMatcher(None, query.replace(" ", ""), norm.replace(" ", "")).ratio())
            if score >= review_at:
                hits.append(SanctionsHit(uid, raw, program, round(score, 3)))
        return sorted(hits, key=lambda h: -h.score)[:limit]


def check_payee(api: AirwallexAPI, sanctions: SanctionsList, beneficiary: dict,
                transfer_methods: list[str], *, on_behalf_of: str | None = None) -> PayeeResult:
    result = PayeeResult(Verdict.PASS)
    bank = beneficiary.get("bank_details", {})

    try:
        kw = {"on_behalf_of": on_behalf_of} if on_behalf_of else {}
        api.validate_beneficiary(beneficiary, transfer_methods, **kw)
    except AirwallexError as e:
        errors = (e.body.get("details") or {}).get("errors") or [{"code": e.code, "source": e.body.get("source")}]
        for err in errors:
            why = BANK_ERROR_CODES.get(str(err.get("code")), f"code {err.get('code')}")
            result.bank_problems.append(f"{why} ({err.get('source')})")

    swift = bank.get("swift_code")
    if swift and not SWIFT_RE.match(swift):
        result.bank_problems.append(f"SWIFT code {swift} is malformed")
    elif swift:
        country = bank.get("bank_country_code")
        if country and swift[4:6] != country:
            result.bank_problems.append(f"SWIFT code {swift} is for {swift[4:6]}, bank country is {country}")
        result.bank_warnings.append("SWIFT code format checked only; Airwallex does not confirm it exists")

    name = beneficiary.get("company_name") or " ".join(
        filter(None, [beneficiary.get("first_name"), beneficiary.get("last_name")]))
    for candidate in filter(None, {name, bank.get("account_name")}):
        result.sanctions_hits += [h for h in sanctions.screen(candidate) if h not in result.sanctions_hits]

    if result.bank_problems or any(h.score == 1.0 for h in result.sanctions_hits):
        result.verdict = Verdict.FAIL
    elif result.sanctions_hits:
        result.verdict = Verdict.REVIEW
    return result
