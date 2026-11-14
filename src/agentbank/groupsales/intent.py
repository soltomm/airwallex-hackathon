"""Read what a buyer's typed reply asks for, so the buyer can negotiate in their own words.

The buyer's mailbox has no pre-written drafts: the person playing the buyer types every reply. This module turns
a typed reply into the same structured move a draft used to carry, so either seller can act on it:

- an offer on the table: changed terms ("we need 20% attrition", "£200 per room-night", "a 25% deposit") become
  a counter; a plain acceptance ("We accept", "Agreed, please go ahead") accepts the current version;
- a confirmed booking: a reduction ("please release two rooms a night", "4 fewer rooms") becomes a change request.

It only reads. Every move still goes through Engine.buyer_action and the sellers, which check versions, prices,
floors, approvals and inventory exactly as for a move sent any other way. Nothing here can loosen a rule, and
anything it can't read stays a plain message for the seller to read.
"""
from __future__ import annotations

import re

from .extract import NUM, _minor, _n

# An acceptance is only read when the message says nothing that conditions or questions it.
_HEDGE = re.compile(r"\b(not|cannot|can'?t|won'?t|don'?t|unless|if|but|however|except|instead|before|"
                    r"once|after|until|maybe|perhaps|might|would|could|check|confirm with|organi[sz]er)\b|n't\b|\?")
_ACCEPT = re.compile(r"\b(?:we|i)(?:\s+(?:are|am)|'re|'m)\s+(?:happy|pleased|glad)\s+to\s+accept\b|"
                     r"\b(?:we|i)\s+(?:(?:hereby|now|gladly)\s+)?accept\b|\baccepted\b|\bagreed\b|\bwe\s+(?:agree|will take it|'ll take it)\b|"
                     r"\b(?:please\s+)?(?:go ahead|proceed)\b|\bdeal\b|\blet'?s\s+(?:do it|book|proceed)\b")
_PCT = r"(\d{1,3})\s*(?:%|per\s*cent|percent)"
_DEPOSIT = [re.compile(_PCT + r"\s+(?:deposit|up\s?front|on signing|prepayment)"),
            re.compile(r"deposit\s+(?:of|at|to|down to|up to|be)?\s*" + _PCT)]
_ATTRITION = [re.compile(_PCT + r"\s+(?:attrition|free release|release allowance|cancellation allowance|allowance|"
                         r"flexibility)"),
              re.compile(r"(?:attrition|release allowance|free release)\s+(?:allowance\s+)?(?:of|at|to|up to|be)?\s*"
                         + _PCT)]
_AMOUNT = r"£\s?(\d{2,4}(?:,\d{3})*(?:\.\d{2})?)"
_PER = r"\s*(?:per|a|an|each|/)\s*(?:room[\s-]?night|room|night)"
_RATE = [re.compile(_AMOUNT + _PER), re.compile(r"rate\s+(?:(?:come|go|be|drop|move)\s+)?(?:down\s+)?(?:of|at|to)\s*" + _AMOUNT)]

_REDUCE_CUE = re.compile(r"\b(reduce|reduction|release|fewer|less|cancel|drop|remove|lose|no longer|"
                         r"can'?t attend|cannot attend|down to|smaller)\b")
_ROOMS = r"(?:twin|king|double|single)?\s*rooms?"
_RELEASE_N = [re.compile(r"\bby\s+" + NUM + r"\s+" + _ROOMS),
              re.compile(r"\b(?:release|cancel|drop|remove|lose|give back|reduce)\s+" + NUM + r"\s+" + _ROOMS),
              re.compile(r"\b" + NUM + r"\s+fewer\s+" + _ROOMS),
              re.compile(r"\b" + NUM + r"\s+" + _ROOMS + r"\s+(?:fewer|less)\b")]
_DOWN_TO = re.compile(r"\b(?:down\s+)?to\s+" + NUM + r"\s+" + _ROOMS)


def _first(patterns: list[re.Pattern], text: str) -> str | None:
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1)
    return None


def counter_terms(text: str, terms: dict) -> dict:
    """The terms a reply asks to change from the current offer: only ones it names and that differ."""
    low = text.lower()
    ask: dict = {}
    dep = _first(_DEPOSIT, low)
    if dep and 0 < int(dep) <= 100 and int(dep) != terms["deposit_pct"]:
        ask["deposit_pct"] = int(dep)
    att = _first(_ATTRITION, low)
    if att and 0 < int(att) < 100 and int(att) != terms["attrition_pct"]:
        ask["attrition_pct"] = int(att)
    rate = _first(_RATE, low)
    if rate and _minor(rate) < terms["rate"]:        # a lower rate is an ask; restating or raising it is not
        ask["rate"] = _minor(rate)
    return ask


def accepts(text: str) -> bool:
    low = " ".join(text.lower().split())
    return bool(_ACCEPT.search(low)) and not _HEDGE.search(low)


def rooms_released(text: str, booked_per_night: int | None) -> int | None:
    """Rooms a night the buyer asks to release, or None when the reply doesn't say how many."""
    low = text.lower()
    n = _first(_RELEASE_N, low)
    if n is not None:
        return _n(n) or None
    m = _DOWN_TO.search(low)
    if m and booked_per_night and 0 < _n(m.group(1)) < booked_per_night:
        return booked_per_night - _n(m.group(1))
    return None


def interpret(d: dict, text: str) -> tuple[str, dict] | None:
    """The structured move a typed reply makes on deal `d`, or None to leave it a plain message."""
    if not text or not text.strip():
        return None
    st, cur = d["status"], (d["versions"][-1] if d["versions"] else None)
    if cur and cur["status"] == "sent" and (st, cur["kind"]) in (("offer_sent", "offer"), ("amendment_sent", "amendment")):
        if cur["kind"] == "offer":
            ask = counter_terms(text, cur["terms"])
            if ask:
                return "counter", ask
        if accepts(text):
            return "accept", {"version": cur["n"]}
        return None
    bk = d.get("booking")
    if st == "confirmed" and bk and bk.get("status") == "confirmed" and _REDUCE_CUE.search(text.lower()) \
            and re.search(r"\brooms?\b", text.lower()):
        per_night = sorted(set((bk.get("rooms") or {}).values()))
        n = rooms_released(text, per_night[0] if len(per_night) == 1 else None)
        return "change_request", ({"rooms_per_night": n} if n else {})
    return None
