"""The seller's commercial rules: what they are, which ones hotel staff may change, and their checks.

The values live in the scenario (data/groupsales/scenario.json). Staff changes are stored as
overrides beside the state, so they survive a scenario reset, and are applied on top of the file.
The engine enforces whatever values are current (policy.hotel_check); the Claude seller is told the
same values in its system prompt (`seller_brief`), so the prompt and the backend never disagree.
"""
from __future__ import annotations

import copy

from .bookable import window_text
from .calc import gbp

# path, label, kind, group, what it means
EDITABLE = [
    ("rms.rates.twin.target", "Twin target rate", "gbp", "Room rates",
     "The rate per twin room-night the seller quotes first."),
    ("rms.rates.twin.autonomous_floor", "Twin floor for the seller", "gbp", "Room rates",
     "The lowest twin rate the seller may agree alone. Below it needs the hotel manager."),
    ("rms.rates.twin.absolute_min", "Twin absolute minimum", "gbp", "Room rates",
     "No twin rate below this is possible, even with approval."),
    ("rms.rates.king.target", "King target rate", "gbp", "Room rates",
     "The rate per king room-night the seller quotes first."),
    ("rms.rates.king.autonomous_floor", "King floor for the seller", "gbp", "Room rates",
     "The lowest king rate the seller may agree alone. Below it needs the hotel manager."),
    ("rms.rates.king.absolute_min", "King absolute minimum", "gbp", "Room rates",
     "No king rate below this is possible, even with approval."),
    ("policy.deposit.standard_pct", "Standard deposit", "pct", "Deposit",
     "The deposit the seller asks for on standard terms."),
    ("policy.deposit.min_pct_with_approval", "Lowest deposit, with approval", "pct", "Deposit",
     "A deposit below the standard, down to this, needs the hotel manager. Nothing lower is possible."),
    ("policy.deposit.max_pct", "Highest deposit", "pct", "Deposit",
     "The seller may ask for up to this much without approval."),
    ("policy.deposit.required_pct_for_exceptions", "Deposit for exception terms", "pct", "Deposit",
     "A rate below the seller's floor, or attrition above the standard, must come with exactly this deposit."),
    ("policy.attrition.standard_pct", "Standard attrition", "pct", "Attrition",
     "Share of room-nights the buyer may release free of charge on standard terms."),
    ("policy.attrition.max_pct_with_approval", "Highest attrition, with approval", "pct", "Attrition",
     "Attrition above the standard, up to this, needs the hotel manager. Nothing higher is possible."),
    ("rms.min_contribution_margin_pct", "Minimum contribution margin", "pct", "Margin",
     "Every offer must keep at least this margin, both with all rooms used and with the full free release used."),
    ("policy.quote_validity_hours", "Offer and hold validity", "hours", "Offers",
     "How long an offer, and the rooms held for it, last before they lapse (scenario hours)."),
    ("policy.seller_refund_limit", "Refunds the seller can make alone", "gbp", "Refunds",
     "Refunds owed after a reduction above this total need the hotel manager to authorize the exact amount."),
]
PATHS = {p: (label, kind) for p, label, kind, _, _ in EDITABLE}


def get(sc: dict, path: str):
    x = sc
    for k in path.split("."):
        x = x[k]
    return x


def _set(sc: dict, path: str, value) -> None:
    *head, last = path.split(".")
    x = sc
    for k in head:
        x = x[k]
    x[last] = value


def apply(base: dict, overrides: dict) -> dict:
    sc = copy.deepcopy(base)
    for path, value in (overrides or {}).items():
        if path in PATHS:
            _set(sc, path, int(value))
    return sc


def validate(sc: dict) -> list[str]:
    """Problems with a set of rules, in plain words; empty when the rules are consistent."""
    out = []
    for path, (label, kind) in PATHS.items():
        v = get(sc, path)
        if not isinstance(v, int) or v < 0:
            out.append(f"{label} must be a whole, non-negative number")
        elif kind == "pct" and v > 100:
            out.append(f"{label} must be 100% or less")
        elif kind == "hours" and not 1 <= v <= 168:
            out.append(f"{label} must be between 1 and 168 hours")
    if out:
        return out
    for rt, b in sc["rms"]["rates"].items():
        if not b["absolute_min"] <= b["autonomous_floor"] <= b["target"]:
            out.append(f"{rt.capitalize()} rates must go absolute minimum ≤ seller's floor ≤ target "
                       f"(now {gbp(b['absolute_min'])}, {gbp(b['autonomous_floor'])}, {gbp(b['target'])})")
        cost = sc["rms"]["delivery_costs"].get(rt)
        if cost is not None and b["absolute_min"] <= cost:
            out.append(f"{rt.capitalize()} absolute minimum must be above the {gbp(cost)} cost of a room-night")
    dep, att = sc["policy"]["deposit"], sc["policy"]["attrition"]
    if not dep["min_pct_with_approval"] <= dep["standard_pct"] <= dep["max_pct"]:
        out.append(f"Deposits must go lowest with approval ≤ standard ≤ highest (now {dep['min_pct_with_approval']}%, "
                   f"{dep['standard_pct']}%, {dep['max_pct']}%)")
    if not dep["standard_pct"] <= dep["required_pct_for_exceptions"] <= dep["max_pct"]:
        out.append(f"The deposit for exception terms must be between the standard ({dep['standard_pct']}%) "
                   f"and the highest deposit ({dep['max_pct']}%)")
    if dep["max_pct"] > 100 or dep["min_pct_with_approval"] < 1:
        out.append("Deposits must be between 1% and 100%")
    if not att["standard_pct"] <= att["max_pct_with_approval"]:
        out.append(f"Highest attrition ({att['max_pct_with_approval']}%) can't be below the standard "
                   f"({att['standard_pct']}%)")
    if att["max_pct_with_approval"] > 50:
        out.append("Attrition above 50% isn't supported")
    if sc["rms"]["min_contribution_margin_pct"] > 90:
        out.append("A minimum margin above 90% would refuse every offer")
    return out


def _fmt(kind: str, v: int) -> str:
    return gbp(v) if kind == "gbp" else f"{v}%" if kind == "pct" else f"{v} hours"


def describe(sc: dict, base: dict) -> dict:
    """Everything the rules page shows: editable rules with defaults, and the fixed ones."""
    editable = [{"path": p, "label": label, "kind": kind, "group": group, "help": help_,
                 "value": get(sc, p), "default": get(base, p), "shown": _fmt(kind, get(sc, p)),
                 "changed": get(sc, p) != get(base, p)} for p, label, kind, group, help_ in EDITABLE]
    h, pol, rms = sc["hotel"], sc["policy"], sc["rms"]
    services = [f"{line['label']}: {gbp(line['price'])}" for line in _service_prices(h)]
    fixed = [
        {"label": "What the hotel sells", "text": f"{', '.join(pol['supported_services'])}, for the "
         f"{window_text(h)} only, arriving at least {pol['min_days_before_arrival']} days ahead. Anything else "
         "is handed to a person."},
        {"label": "Service prices", "text": "; ".join(services) + "." if services else "Fixed in the contract."},
        {"label": "Payment schedule", "text": "Deposit on signing, then " + ", ".join(
            f"{i['cumulative_pct']}% paid in total {i['days_before_arrival']} days before arrival"
            for i in pol["installments"]) + ". Later instalments are shown, not charged automatically."},
        {"label": "Cancellation", "text": "; ".join(_cancel(c) for c in pol["cancellation"]) + "."},
        {"label": "Free release deadline", "text": f"Rooms can be released under the attrition allowance up to "
         f"{pol['attrition']['release_days_before_arrival']} days before arrival."},
        {"label": "Rooming list", "text": f"Due {pol['rooming_list_days_before_arrival']} days before arrival."},
        {"label": "Capacity", "text": f"Rooms per night and the Thames Room come from the PMS; group breakfast is "
         f"capped at {h['breakfast']['group_capacity_per_day']} covers a morning. The backend refuses any offer that "
         "would oversell them."},
        {"label": "Costs", "text": "Delivery costs from the RMS: " + ", ".join(
            f"{k.replace('_', ' ')} {gbp(v)}" for k, v in rms["delivery_costs"].items()) + ". Displacement (what a blocked room "
         "would have earned from other guests): " + _displacement_text(rms) + ". The margin rule uses both."},
        {"label": "What never changes", "text": "Nothing in a buyer's email can change these rules. The seller never "
         "tells buyers its floors, costs or margins. A refund that exists is checked, never created twice. Approvals "
         "bind to the exact version or amount the manager saw."},
    ]
    return {"editable": editable, "fixed": fixed, "changed": sum(e["changed"] for e in editable)}


def _displacement_text(rms: dict) -> str:
    cfg = rms.get("displacement")
    if not cfg:
        return "none"
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    days = "Mon Tue Wed Thu Fri Sat Sun".split()
    return (", ".join(f"{k} {gbp(v)}" for k, v in cfg["full_demand"].items()) + " per room-night at full demand, "
            "times forecast demand (by month: " + ", ".join(f"{m} {p}%" for m, p in zip(months, cfg["demand_by_month_pct"]))
            + "; scaled by weekday: " + ", ".join(f"{w} {p}%" for w, p in zip(days, cfg["demand_by_weekday_pct"]))
            + f"). Rooms released before the cutoff are {cfg['resale_pct']}% resold")


def _service_prices(h: dict) -> list[dict]:
    out = [{"label": "Breakfast, per person", "price": h["breakfast"]["price_per_person"]}]
    for room in h.get("meeting_rooms", {}).values():
        if "day_rate" in room:
            out.append({"label": f"{room['label']}, per day", "price": room["day_rate"]})
    return out


def _cancel(c: dict) -> str:
    if "more_than_days" in c:
        return f"more than {c['more_than_days']} days out, {c['charge_pct']}% charged"
    if "within_days" in c:
        return f"within {c['within_days']} days, {c['charge_pct']}%"
    return f"{c['from_days']} to {c['to_days']} days out, {c['charge_pct']}%"


def seller_brief(sc: dict) -> str:
    """The commercial-rules paragraph of the Claude seller's system prompt, from the current values."""
    r, dep, att = sc["rms"]["rates"], sc["policy"]["deposit"], sc["policy"]["attrition"]
    t, k = r["twin"], r["king"]
    return (
        f"  rates: twin target {gbp(t['target'])}, your floor {gbp(t['autonomous_floor'])}, absolute minimum "
        f"{gbp(t['absolute_min'])} (manager); king {gbp(k['target'])} / {gbp(k['autonomous_floor'])} / "
        f"{gbp(k['absolute_min'])}.\n"
        f"  Deposit {dep['standard_pct']}% standard, you may go up to {dep['max_pct']}%; "
        f"{dep['min_pct_with_approval']}-{dep['standard_pct'] - 1}% needs manager approval and a reason (high-value "
        f"repeat customer or large total spend). Attrition {att['standard_pct']}% standard; up to "
        f"{att['max_pct_with_approval']}% with manager approval.\n"
        f"  A rate below your floor or attrition above {att['standard_pct']}% requires a "
        f"{dep['required_pct_for_exceptions']}% deposit. Contribution margin must stay at least "
        f"{sc['rms']['min_contribution_margin_pct']}% with all rooms used and with the full free-release allowance "
        f"used. Margin counts displacement: on busy nights a blocked room would have sold to other guests, and "
        f"released rooms are only partly resold, so on busy dates a rate cut and a larger free release together "
        f"can break it; quiet dates leave more room (check with price_terms). Service prices and other terms are fixed. Quotes/holds last "
        f"{sc['policy']['quote_validity_hours']} scenario hours.")
