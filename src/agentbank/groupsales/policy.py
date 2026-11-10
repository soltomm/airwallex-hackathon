"""Authority rules. The hotel's commercial rules bind the seller; the buyer's mandate binds the buyer.

Each check returns hard violations (nobody can approve them) and exceptions that need a named
approver. Each exception also comes as a rule ({"rule", "alone", "asked"}): which limit is crossed,
what the party could agree alone, and what this request asks for, so an approver sees exactly why. The engine enforces these in code: nothing an agent writes, and nothing inside an RFP,
can change them.
"""
from __future__ import annotations

from datetime import timedelta

from .bookable import window_text
from .calc import d, gbp

LAYOUTS_ON_FILE = {"classroom"}


def rule(name: str, alone: str, asked: str) -> dict:
    """One crossed limit: the rule, what the party can agree alone, what this request asks for."""
    return {"rule": name, "alone": alone, "asked": asked}


def hotel_check(terms: dict, pricing: dict, scenario: dict) -> dict:
    hotel, rms, policy = scenario["hotel"], scenario["rms"], scenario["policy"]
    hard: list[str] = []
    needs: list[str] = []
    rules: list[dict] = []
    if terms.get('vendor_lines'):
        needs.append('Third-party master package requires hotel manager approval')
        rules.append(rule("Third-party services", "hotel services only", "a master package with outside vendors"))
    rt = terms.get("room_type")
    if rt not in rms["rates"]:
        return {"hard": [f"unknown room type {rt!r}"], "approvals": [], "rules": [], "ok": False}
    bands = rms["rates"][rt]
    rate, dep, att = int(terms["rate"]), int(terms["deposit_pct"]), int(terms["attrition_pct"])
    dep_pol, att_pol = policy["deposit"], policy["attrition"]

    if terms.get("currency", "GBP") != "GBP":
        hard.append("all charges are billed in GBP")
    bookable_nights = set(hotel["nights"])
    for night in terms.get("rooms", {}):
        if night not in bookable_nights:
            hard.append(f"{night} is outside the bookable {window_text(hotel)}")
    # Breakfast is served the morning after a contracted night.
    mornings = {(d(n) + timedelta(days=1)).isoformat() for n in terms.get("rooms", {})}
    for morning in (terms.get("breakfast") or {}):
        if morning not in mornings:
            hard.append(f"breakfast on {morning} is outside the stay")
    if rate < bands["absolute_min"]:
        hard.append(f"{rt} rate {gbp(rate)} is below the absolute minimum {gbp(bands['absolute_min'])}")
    exception_terms = False
    if rate < bands["autonomous_floor"]:
        needs.append(f"{rt} rate {gbp(rate)} below the seller's floor {gbp(bands['autonomous_floor'])}")
        rules.append(rule(f"{rt.capitalize()} rate", f"{gbp(bands['autonomous_floor'])} or more", gbp(rate)))
        exception_terms = True
    if att > att_pol["max_pct_with_approval"]:
        hard.append(f"attrition {att}% exceeds the {att_pol['max_pct_with_approval']}% maximum")
    elif att > att_pol["standard_pct"]:
        needs.append(f"attrition {att}% above the standard {att_pol['standard_pct']}%")
        rules.append(rule("Free release (attrition)", f"up to {att_pol['standard_pct']}%", f"{att}%"))
        exception_terms = True
    if dep > dep_pol["max_pct"]:
        hard.append(f"deposit {dep}% above the {dep_pol['max_pct']}% maximum")
    if dep < dep_pol["min_pct_with_approval"]:
        hard.append(f"deposit {dep}% below the {dep_pol['min_pct_with_approval']}% minimum")
    if exception_terms and dep != dep_pol["required_pct_for_exceptions"]:
        hard.append(f"a below-floor rate or attrition above {att_pol['standard_pct']}% requires a "
                    f"{dep_pol['required_pct_for_exceptions']}% deposit")
    elif dep_pol["min_pct_with_approval"] <= dep < dep_pol["standard_pct"]:
        needs.append(f"deposit {dep}% below the standard {dep_pol['standard_pct']}% "
                     "(only for high-value repeat customers or large total spend)")
        rules.append(rule("Deposit", f"{dep_pol['standard_pct']}% to {dep_pol['max_pct']}%", f"{dep}%"))

    bf_cap = hotel["breakfast"]["group_capacity_per_day"]
    for morning, n in (terms.get("breakfast") or {}).items():
        if int(n) > bf_cap:
            hard.append(f"breakfast for {n} on {morning} exceeds group capacity {bf_cap}")
    m = terms.get("meeting")
    if m:
        room = hotel["meeting_rooms"].get(m.get("room", ""))
        if not room:
            hard.append(f"unknown meeting room {m.get('room')!r}")
        else:
            if m.get("date") not in room["available"]:
                hard.append(f"{room['label']} is not available on {m.get('date')}")
            layout = (m.get("layout") or "").lower()
            if layout and layout not in room["capacity"]:
                hard.append(f"{layout} layout is not on file for {room['label']}; hand to events team")
            elif layout and int(m.get("attendees", 0)) > room["capacity"][layout]:
                hard.append(f"{room['label']} holds {room['capacity'][layout]} in {layout}")

    floor = rms["min_contribution_margin_pct"]
    for case, label in (("margin_all_rooms", "all rooms used"), ("margin_full_release", "full allowance released")):
        if pricing[case]["margin_pct"] < floor:
            hard.append(f"contribution margin {pricing[case]['margin_pct']}% with {label} is below {floor}%")
    return {"hard": hard, "approvals": needs, "rules": rules, "ok": not hard}


def buyer_rules(terms: dict, pricing: dict, mandate: dict) -> list[dict]:
    """buyer_check as rules, for the organizer's card (the buyer's own mandate; never shown to the seller)."""
    out = []
    if pricing["total"] > mandate["budget_total"]:
        out.append(rule("Total budget", f"up to {gbp(mandate['budget_total'])}", gbp(pricing["total"])))
    if pricing["deposit"] > mandate["deposit_limit"]:
        out.append(rule("Deposit amount", f"up to {gbp(mandate['deposit_limit'])}", gbp(pricing["deposit"])))
    if pricing["deposit_pct"] > mandate["deposit_limit_pct"]:
        out.append(rule("Deposit share", f"up to {mandate['deposit_limit_pct']}%", f"{pricing['deposit_pct']}%"))
    return out


def buyer_check(terms: dict, pricing: dict, mandate: dict) -> list[str]:
    """Reasons the buyer agent needs organizer authorization to accept these terms."""
    out = []
    if pricing["total"] > mandate["budget_total"]:
        out.append(f"total {gbp(pricing['total'])} above the {gbp(mandate['budget_total'])} budget")
    if pricing["deposit"] > mandate["deposit_limit"]:
        out.append(f"deposit {gbp(pricing['deposit'])} above the {gbp(mandate['deposit_limit'])} limit")
    if pricing["deposit_pct"] > mandate["deposit_limit_pct"]:
        out.append(f"deposit {pricing['deposit_pct']}% above the {mandate['deposit_limit_pct']}% limit")
    return out
