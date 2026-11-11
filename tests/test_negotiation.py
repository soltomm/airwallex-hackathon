"""The negotiation planner and the hotel manager's "approve with changes".

Run: PYTHONPATH=src python -m pytest tests/test_negotiation.py -q
"""
from __future__ import annotations

import pytest

from agentbank.groupsales import negotiate
from agentbank.groupsales.engine import Refused
from agentbank.groupsales.policy import hotel_check
from agentbank.groupsales.seller import RulesSeller

from test_group_sales import _quoted, buyer_turn, deal_by, eng  # noqa: F401  (eng is a fixture)


def _countered(e, payload, text="Counter"):
    """Northstar's v1 is out (£220, 30% deposit, 10% release); the buyer counters with `payload`."""
    s, n = _quoted(e)
    e.buyer_action(n["id"], n["buyer_token"], "counter", text, payload)
    s.run()
    return s, n


def _pending(e, n):
    return next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")


# --------------------------------------------------------------------------------------------- the planner
def test_planner_trades_release_for_deposit_and_keeps_the_rate(eng):
    """The demo counter (20% release, 25% deposit): the extra release costs the hotel less than it is worth to the
    buyer, so the planner gives it against the 40% exception deposit. The spec's £3,840 deposit is unchanged."""
    e = eng
    s, n = _quoted(e)
    t = n["versions"][0]["terms"]
    pl = negotiate.plan(e, n["id"], t, {"attrition_pct": 20, "deposit_pct": 25}, 12)
    c = pl["choice"]
    assert (c["rate"], c["attrition_pct"], c["deposit_pct"]) == (22000, 20, 40) and c["deposit"] == 384000
    assert c["rules"] == ["Free release (attrition)"]
    assert c["score"] > pl["current"]["score"]
    assert pl["levers"]["attrition"]["how"] == "met" and pl["levers"]["deposit"]["why"] == "exchange"
    assert pl["ask"]["hard"]                                    # 25% with 20% release breaks the deposit rule
    for o in pl["options"]:                                    # everything offered is inside the hard limits
        assert not o["hard"] and o["buyer_cost"] <= pl["current"]["buyer_cost"]


def test_planner_never_leaves_the_buyer_worse_off(eng):
    """A larger deposit is worth more to the hotel than it costs the buyer, but alone it is no answer to a counter:
    with nothing to give, the planner keeps the current terms rather than raising the deposit."""
    e = eng
    s, n = _quoted(e)
    t = n["versions"][0]["terms"]
    pl = negotiate.plan(e, n["id"], t, {"deposit_pct": 20}, 12)
    assert negotiate.describe(pl["choice"]) == negotiate.describe(pl["current"])


def test_rate_counter_meets_the_buyer_part_way_below_the_floor_with_approval(eng):
    """Northstar asks for £180. The seller moves at most half way (£200), but on these busy March nights £200 breaks
    the margin, so it offers £205: below its own floor, which needs the 40% deposit and the hotel manager."""
    e = eng
    s, n = _countered(e, {"rate": 18000, "deposit_pct": 30}, "Could you do £180?")
    v = n["versions"][-1]
    assert (v["terms"]["rate"], v["terms"]["deposit_pct"], v["terms"]["attrition_pct"]) == (20500, 40, 10)
    assert n["status"] == "pending_approval"
    a = _pending(e, n)
    assert [r["rule"] for r in a["rules"]] == ["Twin rate"]
    assert a["options"][0]["label"] == "Seller's choice"
    assert "demand on your dates is high" in v["message"].lower() and "£205" in v["message"]
    assert "Counter: weighed" in e.s["activity"][-1]["text"] or any(
        "Counter: weighed" in x["text"] for x in e.s["activity"] if x.get("deal_id") == n["id"])


def test_small_rate_gain_is_not_worth_the_managers_time(eng):
    """Asking for £5 off from the floor: half way is £2.50, rounded up to the next £5 step, so the rate holds and
    nothing goes to the manager."""
    e = eng
    s, n = _quoted(e)
    t = dict(n["versions"][0]["terms"], rate=21000)
    pl = negotiate.plan(e, n["id"], t, {"rate": 20500}, 12)
    assert pl["choice"]["rate"] == 21000 and not pl["choice"]["rules"]


def test_planner_config_comes_from_the_scenario(eng):
    cfg = negotiate.config(eng.rms)
    assert cfg["rate_split_pct"] == 50 and cfg["approval_cost"] == 5000 and "_note" not in cfg


# ---------------------------------------------------------------------------------- approve with changes
def test_manager_approves_with_changes_and_the_seller_sends_those_terms_without_asking_again(eng):
    e = eng
    s, n = _countered(e, {"attrition_pct": 20, "deposit_pct": 25})
    a = _pending(e, n)
    r = e.decide_approval(a["id"], "amend", "hotel_manager", "15% is enough for this account",
                          {"attrition_pct": 15, "deposit_pct": 40})
    assert r["status"] == "amended" and n["status"] == "revise"
    assert a["amended_terms"]["attrition_pct"] == 15 and a["amended_terms"]["rate"] == 22000
    s.run()
    v = n["versions"][-1]
    assert n["status"] == "offer_sent" and v["status"] == "sent" and v["n"] == 3
    assert (v["terms"]["rate"], v["terms"]["attrition_pct"], v["terms"]["deposit_pct"]) == (22000, 15, 40)
    assert not [x for x in e.s["approvals"].values() if x["deal_id"] == n["id"] and x["status"] == "pending"]
    bound = next(x for x in e.s["approvals"].values() if x.get("preauthorized_by") == a["id"])
    assert (bound["version"], bound["hash"], bound["status"]) == (3, v["hash"], "approved")
    assert bound["rules"] == hotel_check(v["terms"], v["pricing"], e.scenario)["rules"]
    assert a["used"] is True
    assert "15% attrition" in v["message"]
    assert buyer_turn(e, "Northstar")["accepted"] is True          # the buyer can take the manager's terms


def test_manager_changes_that_break_a_hard_limit_are_refused_and_the_request_stays_open(eng):
    e = eng
    s, n = _countered(e, {"attrition_pct": 20, "deposit_pct": 25})
    a = _pending(e, n)
    with pytest.raises(Refused, match="hard limit"):
        e.decide_approval(a["id"], "amend", "hotel_manager", changes={"attrition_pct": 25})       # above 20% max
    with pytest.raises(Refused, match="40% deposit"):
        e.decide_approval(a["id"], "amend", "hotel_manager", changes={"deposit_pct": 30})         # exception needs 40%
    with pytest.raises(Refused, match="hard limit"):
        e.decide_approval(a["id"], "amend", "hotel_manager", changes={"rate": 19000})             # below the minimum
    with pytest.raises(Refused, match="use Approve"):
        e.decide_approval(a["id"], "amend", "hotel_manager", changes={"attrition_pct": 20})       # no change
    with pytest.raises(Refused, match="only the hotel manager"):
        e.decide_approval(a["id"], "amend", "seller", changes={"attrition_pct": 15})
    assert a["status"] == "pending" and n["status"] == "pending_approval"


def test_manager_can_take_the_offer_back_to_standard_terms(eng):
    """Changes within the seller's own authority need no approval at all: the seller sends them straight away."""
    e = eng
    s, n = _countered(e, {"attrition_pct": 20, "deposit_pct": 25})
    a = _pending(e, n)
    e.decide_approval(a["id"], "amend", "hotel_manager", changes={"attrition_pct": 10, "deposit_pct": 30})
    s.run()
    v = n["versions"][-1]
    assert n["status"] == "offer_sent" and (v["terms"]["attrition_pct"], v["terms"]["deposit_pct"]) == (10, 30)
    assert a["used"] is True and not any(x.get("preauthorized_by") for x in e.s["approvals"].values())


def test_amended_terms_only_cover_those_exact_terms(eng):
    """The manager's terms authorize one package: a different one still goes back to the manager."""
    e = eng
    s, n = _countered(e, {"attrition_pct": 20, "deposit_pct": 25})
    a = _pending(e, n)
    e.decide_approval(a["id"], "amend", "hotel_manager", changes={"attrition_pct": 15, "deposit_pct": 40})
    r = e.propose_offer(n["id"], 22000, 40, 20, "not what the manager set", rooms_per_night=12)
    assert r["status"] == "pending_approval" and not a.get("used")


def test_only_offers_can_be_approved_with_changes(eng):
    e = eng
    s, n = _countered(e, {"attrition_pct": 20, "deposit_pct": 25})
    a = _pending(e, n)
    a["subject"] = "refund"                                     # any non-offer request
    with pytest.raises(Refused, match="only an offer"):
        e.decide_approval(a["id"], "amend", "hotel_manager", changes={"attrition_pct": 15})


def test_after_a_rejection_the_seller_offers_the_best_it_can_agree_alone(eng):
    """A plain rejection: the seller re-plans the buyer's counter within its own authority and asks nobody."""
    e = eng
    s, n = _countered(e, {"rate": 18000, "deposit_pct": 30})
    a = _pending(e, n)
    e.decide_approval(a["id"], "reject", "hotel_manager", "keep the floor")
    s.run()
    v = n["versions"][-1]
    assert n["status"] == "offer_sent" and v["terms"]["rate"] == e.rms["rates"]["twin"]["autonomous_floor"]
    assert v["terms"]["deposit_pct"] == 30
    assert not [x for x in e.s["approvals"].values() if x["deal_id"] == n["id"] and x["status"] == "pending"]
    assert any("best terms the seller can agree alone" in x["text"] for x in e.s["activity"] if x.get("deal_id") == n["id"])


def test_the_buyer_never_sees_the_options_or_the_managers_terms(eng):
    e = eng
    s, n = _countered(e, {"attrition_pct": 20, "deposit_pct": 25})
    a = _pending(e, n)
    e.decide_approval(a["id"], "amend", "hotel_manager", changes={"attrition_pct": 15, "deposit_pct": 40})
    s.run()
    view = repr(e.deal_view(n["id"], for_="buyer"))
    for secret in ("options", "amended", "expected contribution", "Seller's choice", "margin"):
        assert secret not in view, secret


def test_claude_seller_counter_options_tool_attaches_the_comparison(eng):
    """The Claude seller gets the same search as a tool; proposing one of its packages puts the options on the card."""
    from agentbank.groupsales.seller import TOOLS, ClaudeSeller, schema_errors
    e = eng
    s, n = _quoted(e)
    c = ClaudeSeller(e, client=object())
    tool = next(t for t in TOOLS if t["name"] == "counter_options")
    args = {"deal_id": n["ref"], "rate_gbp": None, "deposit_pct": 25, "attrition_pct": 20}
    assert not schema_errors(args, tool["input_schema"])
    r = c._do("counter_options", args)
    best = r["best_first"][0]
    assert (best["rate"], best["attrition_pct"], best["deposit_pct"]) == ("£220", 20, 40)
    assert r["buyer_exact_ask"]["not_possible"] and r["case_for_best"].startswith("Chose")
    out = c._do("propose_offer", {"deal_id": n["ref"], "rate_gbp": 220, "deposit_pct": 40, "attrition_pct": 20,
                                  "message_to_buyer": "20% release for a 40% deposit."})
    a = e.s["approvals"][out["approval_id"]]
    assert a["options"][0]["label"] == "Seller's choice" and a["options"][0]["attrition_pct"] == 20
