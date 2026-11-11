"""How a seller answers a counteroffer: a search over packages, scored in pounds.

A package is a room rate, a deposit % and an attrition (free release) %. Instead of one hand-written
reply per kind of request, the planner:

1. builds every candidate package between the offer on the table and what the buyer asked for (the
   rate in steps, the attrition and deposit values the policy allows);
2. prices each one with the engine's own dry run (`Engine.price_terms`), so the margin rule,
   displacement on busy nights, inventory and every authority limit come from the backend, never
   from here; packages with a hard violation are dropped;
3. scores what is left in money:

       score = hotel value - 1.25 x cost to the buyer - approval cost x rules crossed

   hotel value  = expected contribution (all rooms used, less the expected share of the free release
                  that gets used) + the part of the deposit the hotel values as cancellation cover;
   buyer cost   = how far the package is from the buyer's ask: rate above it on every room-night,
                  free release short of it (at the rate, times how likely the buyer is to need it),
                  deposit above it (at the buyer's cost of paying cash early);
   approval     = a small cost for each rule that sends the version to the hotel manager, so the
                  seller only asks when the gain is worth the manager's time.

   The buyer's cost is weighted a little above 1 (gap_weight_pct): a pound closer to the buyer's ask is
   worth slightly more than a pound of contribution, because it raises the chance of winning the group.

Two constraints keep it a negotiation rather than a giveaway: the rate never moves more than a set
share of the way to the buyer's ask (the rate is zero-sum, so the hotel splits the difference), and
a counter never leaves the buyer worse off than the offer they answered. The deposit only rises where
the policy asks for it (the exception deposit that comes with a below-floor rate or extra release);
otherwise it stays between the current offer and the buyer's ask. Free release costs the hotel less
than it is worth to the buyer on quiet dates, and more on busy ones (displacement), so the score
trades it for the exception deposit only where that pays.

The planner chooses; the engine still decides. A chosen package that crosses a seller rule goes to
the hotel manager with the options weighed, and nothing here can loosen a limit.
"""
from __future__ import annotations

from .calc import gbp

DEFAULTS = {
    "rate_step": 500,               # pence: candidate rates move in £5 steps
    "rate_split_pct": 50,           # the most of the gap to the buyer's rate the seller concedes
    "expected_release_pct": 50,     # share of a free-release allowance the hotel expects to be used
    "deposit_cover_pct": 15,        # what £1 of deposit is worth to the hotel as cancellation cover
    "buyer_cash_cost_pct": 5,       # what paying £1 earlier costs the buyer
    "release_need_pct": 50,         # how likely the buyer is to use free release they ask for
    "approval_cost": 5000,          # pence per rule crossed: the manager's time
    "gap_weight_pct": 125,          # a pound closer to the buyer's ask, against a pound of contribution
}


def config(rms: dict) -> dict:
    return {**DEFAULTS, **{k: int(v) for k, v in (rms.get("negotiation") or {}).items() if k in DEFAULTS}}


def _ceil_step(x: int, step: int) -> int:
    return -(-x // step) * step


def _pounds(pence: int) -> int:
    return (pence + 50) // 100 * 100


def _allowance(room_nights: int, att: int) -> int:
    return room_nights * att // 100


def _evaluate(e, deal_id: str, rate: int, dep: int, att: int, rooms: int, rt: str, ask: dict, cfg: dict) -> dict:
    r = e.price_terms(deal_id, rate, dep, att, rooms_per_night=rooms, room_type=rt)
    p, chk = r["pricing"], r["hotel_check"]
    rn = p["contracted_room_nights"]
    c_all, c_rel = p["margin_all_rooms"]["contribution"], p["margin_full_release"]["contribution"]
    expected = c_all - (c_all - c_rel) * cfg["expected_release_pct"] // 100
    hotel_value = expected + p["deposit"] * cfg["deposit_cover_pct"] // 100
    rate_gap = max(0, rate - ask["rate"]) * rn
    release_gap = max(0, _allowance(rn, ask["attrition_pct"]) - _allowance(rn, att)) * rate * cfg["release_need_pct"] // 100
    deposit_gap = p["total"] * max(0, dep - ask["deposit_pct"]) // 100 * cfg["buyer_cash_cost_pct"] // 100
    # Estimates, so whole pounds: a manager reads them as judgement, not as accounting.
    expected, hotel_value = _pounds(expected), _pounds(hotel_value)
    buyer_cost = _pounds(rate_gap + release_gap + deposit_gap)
    hard = list(chk["hard"]) + ([] if r["fits_inventory"] else ["the rooms, meeting space or breakfast can't be held"])
    rules = [x["rule"] for x in chk["rules"]]
    return {"rate": rate, "deposit_pct": dep, "attrition_pct": att, "total": p["total"], "deposit": p["deposit"],
            "allowance": p["attrition_allowance"], "margin_all_pct": p["margin_all_rooms"]["margin_pct"],
            "margin_release_pct": p["margin_full_release"]["margin_pct"], "contribution": expected,
            "hotel_value": hotel_value, "buyer_cost": buyer_cost, "rules": rules, "hard": hard,
            "score": hotel_value - buyer_cost * cfg["gap_weight_pct"] // 100 - cfg["approval_cost"] * len(rules)}


def _key(o: dict) -> tuple:
    return o["rate"], o["deposit_pct"], o["attrition_pct"]


def plan(e, deal_id: str, current: dict, ask: dict, rooms: int, authority_only: bool = False) -> dict:
    """Choose the reply to a counteroffer.

    `current` is the terms of the offer the buyer answered; `ask` the buyer's counter (any of rate,
    deposit_pct, attrition_pct); `rooms` the rooms per night to quote. `authority_only` keeps to what
    the seller may agree alone (after the hotel manager declined an exception).

    Returns {"choice", "current", "ask", "options", "levers"}: the chosen package, the current offer
    and the buyer's exact ask as evaluated now, every feasible package best first, and for each
    lever what was asked, what is offered and why ("met", "partly", "capped", "held", with a reason).
    """
    e = getattr(e, "_e", e)     # dry runs bypass a run's trace: the AI Agent tab shows the decision, not the search
    cfg = config(e.rms)
    pol, rt = e.policy, current["room_type"]
    bands = e.rms["rates"][rt]
    dep_pol, att_pol = pol["deposit"], pol["attrition"]
    cur = {"rate": int(current["rate"]), "deposit_pct": int(current["deposit_pct"]),
           "attrition_pct": int(current["attrition_pct"])}
    want = {"rate": int(ask.get("rate") or cur["rate"]), "deposit_pct": int(ask.get("deposit_pct") or cur["deposit_pct"]),
            "attrition_pct": int(ask.get("attrition_pct") or cur["attrition_pct"])}

    # Rates: from the split point (never below the buyer's ask or the absolute minimum) up to the current rate.
    lo = cur["rate"]
    if want["rate"] < cur["rate"]:
        split = cur["rate"] - (cur["rate"] - want["rate"]) * cfg["rate_split_pct"] // 100
        lo = max(want["rate"], _ceil_step(split, cfg["rate_step"]), bands["absolute_min"])
    rates = sorted({cur["rate"], lo, *range(_ceil_step(lo, cfg["rate_step"]), cur["rate"], cfg["rate_step"])})
    # Attrition: from the current allowance up to the ask, capped at what the policy allows at all.
    top = min(max(want["attrition_pct"], cur["attrition_pct"]), att_pol["max_pct_with_approval"])
    atts = sorted({cur["attrition_pct"], top, *range(cur["attrition_pct"], top, 5)})
    # Deposits: between the current offer and the buyer's ask; exception terms take the deposit the policy requires.
    deps = sorted({min(max(x, dep_pol["min_pct_with_approval"]), dep_pol["max_pct"])
                   for x in (cur["deposit_pct"], want["deposit_pct"])})

    def deps_for(rate, att):
        exception = rate < bands["autonomous_floor"] or att > att_pol["standard_pct"]
        return [dep_pol["required_pct_for_exceptions"]] if exception else deps

    def ev(rate, dep, att):
        return _evaluate(e, deal_id, rate, dep, att, rooms, rt, want, cfg)

    base = ev(cur["rate"], cur["deposit_pct"], cur["attrition_pct"])
    exact = ev(max(want["rate"], bands["absolute_min"]), want["deposit_pct"], top)
    all_ = [ev(r, dp, a) for r in rates for a in atts for dp in deps_for(r, a)]
    feasible = [o for o in all_ if not o["hard"] and (not authority_only or not o["rules"])
                and o["buyer_cost"] <= base["buyer_cost"]]
    feasible.sort(key=lambda o: (-o["score"], len(o["rules"]), -o["rate"], o["deposit_pct"]))
    choice = feasible[0] if feasible else None
    return {"choice": choice, "current": base, "ask": exact, "want": want, "options": feasible,
            "levers": _levers(cur, want, choice, all_, bands, dep_pol, att_pol) if choice else {}, "config": cfg}


def _margin_blocks(all_: list[dict], better) -> bool:
    """Whether packages better for the buyer on one lever failed on the margin rule (busy dates)."""
    return any(better(o) and any("contribution margin" in h for h in o["hard"]) for o in all_)


def _levers(cur, want, c, all_, bands, dep_pol, att_pol) -> dict:
    out = {}
    if want["rate"] < cur["rate"]:
        if c["rate"] <= want["rate"]:
            how = "met"
        elif c["rate"] < cur["rate"]:
            how = "partly"
        else:
            how = "held"
        why = ("margin" if _margin_blocks(all_, lambda o: o["rate"] < c["rate"]) else
               "absolute_min" if c["rate"] == bands["absolute_min"] else "split" if how == "partly" else "value")
        out["rate"] = {"asked": want["rate"], "offered": c["rate"], "how": how, "why": why if how != "met" else ""}
    if want["attrition_pct"] > cur["attrition_pct"]:
        cap = att_pol["max_pct_with_approval"]
        if c["attrition_pct"] >= want["attrition_pct"]:
            how, why = "met", ""
        elif _margin_blocks(all_, lambda o: o["attrition_pct"] > c["attrition_pct"]):
            how, why = ("partly" if c["attrition_pct"] > cur["attrition_pct"] else "held"), "margin"
        elif c["attrition_pct"] == cap:
            how, why = "capped", "policy_max"
        else:
            how, why = ("partly" if c["attrition_pct"] > cur["attrition_pct"] else "held"), "value"
        out["attrition"] = {"asked": want["attrition_pct"], "offered": c["attrition_pct"], "how": how, "why": why}
    if c["deposit_pct"] != want["deposit_pct"]:
        exception = c["rate"] < bands["autonomous_floor"] or c["attrition_pct"] > att_pol["standard_pct"]
        why = ("exchange" if exception and c["deposit_pct"] == dep_pol["required_pct_for_exceptions"] else
               "standard" if c["deposit_pct"] >= dep_pol["standard_pct"] > want["deposit_pct"] else "value")
        out["deposit"] = {"asked": want["deposit_pct"], "offered": c["deposit_pct"],
                          "how": "met" if c["deposit_pct"] <= want["deposit_pct"] else "held", "why": why}
    return out


def describe(o: dict) -> str:
    return f"{gbp(o['rate'])}, {o['attrition_pct']}% release, {o['deposit_pct']}% deposit"


def options_for_manager(pl: dict, last_n: int | None = None) -> list[dict]:
    """The packages a hotel manager sees on the approval card: what was chosen and what it was weighed against."""
    c = pl["choice"]
    rows = [("Seller's choice", c)]
    rows.append((f"Our last offer{f' (v{last_n})' if last_n else ''}", pl["current"]))
    alone = next((o for o in pl["options"] if not o["rules"]), None)
    if alone:
        rows.append(("Best the seller can agree alone", alone))
    rows.append(("Buyer's ask", pl["ask"]))
    out, seen = [], set()
    for label, o in rows:
        if _key(o) in seen:
            continue
        seen.add(_key(o))
        out.append({"label": label, "rate": o["rate"], "deposit_pct": o["deposit_pct"], "attrition_pct": o["attrition_pct"],
                    "total": o["total"], "deposit": o["deposit"], "margin_release_pct": o["margin_release_pct"],
                    "contribution": o["contribution"], "buyer_cost": o["buyer_cost"], "score": o["score"],
                    "needs": o["rules"], "not_possible": o["hard"][:2]})
    return out


def justification(pl: dict) -> str:
    """The seller's case in one or two sentences, in money."""
    c, base = pl["choice"], pl["current"]
    gain = base["buyer_cost"] - c["buyer_cost"]
    cost = base["contribution"] - c["contribution"]
    text = (f"Chose {describe(c)}: expected contribution {gbp(c['contribution'])} "
            f"(margin {c['margin_release_pct']}% if the whole free release is used)")
    if gain > 0:
        text += (f"; it closes {gbp(gain)} of the gap to the buyer's ask for "
                 + (f"{gbp(cost)} of expected contribution" if cost > 0 else "no loss of expected contribution"))
    if c["deposit"] > base["deposit"]:
        text += f", and the deposit rises to {gbp(c['deposit'])}"
    alone = next((o for o in pl["options"] if not o["rules"]), None)
    if c["rules"] and alone:
        text += (f". Best the seller could agree alone: {describe(alone)}, which scores "
                 f"{gbp(c['score'] - alone['score'])} lower")
    return text + "."
