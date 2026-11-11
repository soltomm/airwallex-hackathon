"""Prepayment: a lower rate in exchange for more paid on signing, enforced in the engine.

Run: PYTHONPATH=src python -m pytest tests/test_prepay.py -q
"""
from __future__ import annotations

import copy
import json

import pytest

from agentbank.groupsales import bookable, calc, rules
from agentbank.groupsales.buyer import act
from agentbank.groupsales.engine import SCENARIO, Engine
from agentbank.groupsales.policy import hotel_check
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller
from test_group_sales import S, buyer_turn, deal_by, northstar_terms, pay

H, RMS = S["hotel"], S["rms"]


def check(**kw):
    t = northstar_terms(**kw)
    return hotel_check(t, calc.price(t, H, RMS), bookable.expand(S))


def test_prepayment_is_its_own_package_with_its_own_floor():
    # Below the seller's £210 floor, alone, only with 50%+ on signing, standard attrition, and not under £200.
    ok = check(rate=20500, dep=50, att=10)
    assert not ok["approvals"] and not any("deposit" in h or "rate" in h for h in ok["hard"])
    assert not check(rate=20000, dep=100, att=10)["approvals"]
    assert any("can go down to £200" in h for h in check(rate=19900, dep=50, att=10)["hard"])
    assert any("prepayment starts at 50%" in h for h in check(rate=21000, dep=45, att=10)["hard"])
    assert any("standard 10% attrition" in h for h in check(rate=21000, dep=50, att=20)["hard"])
    assert any("more than the total" in h for h in check(rate=21000, dep=101, att=10)["hard"])
    # The exception path is unchanged: below the floor without prepayment needs the manager and exactly 40%.
    exc = check(rate=20500, dep=40, att=10)
    assert any("below the seller's floor" in a for a in exc["approvals"]) and not exc["hard"]
    assert any("requires a 40% deposit" in h for h in check(rate=20500, dep=30, att=10)["hard"])


def _quiet(tmp_path):
    """The demo scenario with little displacement, so margin leaves room for a rate cut."""
    sc = json.loads(SCENARIO.read_text())
    sc["rms"]["displacement"]["full_demand"] = {"twin": 1000, "king": 1000}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(sc))
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0), scenario_path=path)


def _counter_on_price(e, s, rate):
    s.run()
    buyer_turn(e, "Northstar")                                    # answers -> v1 at £220
    s.run()
    n = deal_by(e, "Northstar")
    act(e, n["id"], {"action": "counter", "text": f"Our budget is tight: could you do £{rate // 100} a night?",
                     "payload": {"rate": rate, "attrition_pct": 10, "deposit_pct": 30}})
    s.run()
    return n


def test_a_buyer_pushing_on_price_gets_a_prepayment_trade_without_the_manager(tmp_path):
    e = _quiet(tmp_path)
    s = RulesSeller(e)
    n = _counter_on_price(e, s, 18000)
    v = n["versions"][-1]
    assert v["status"] == "sent" and (v["terms"]["rate"], v["terms"]["deposit_pct"], v["terms"]["attrition_pct"]) == (20000, 50, 10)
    assert "if you pay 50% on signing instead of 30%" in v["message"]
    assert "our best rate is £210" in v["message"]                 # the standard-deposit alternative, named
    assert not [a for a in e.s["approvals"].values() if a["deal_id"] == n["id"]]

    rnd = n["negotiation"][-1]
    by = {(o["rate"], o["deposit_pct"]): o for o in rnd["options"]}
    assert by[(18000, 30)]["verdict"] == "not allowed"             # the ask: below the absolute minimum
    assert by[(21000, 30)]["verdict"] == "seller alone"            # the alternative it named
    assert by[(20000, 50)]["verdict"] == "seller alone" and rnd["chosen"]["key"] == by[(20000, 50)]["key"]
    assert "50% paid on signing" in rnd["chosen"]["explanation"]

    # Paying it: the deposit covers the 50% milestone, the booking is confirmed, the hotel paid its share.
    buyer_turn(e, "Northstar")
    s.run()
    pay(e, n)
    fin = e.finance(n)
    assert n["status"] == "confirmed" and fin["received"] == v["pricing"]["deposit"] == v["pricing"]["total"] // 2
    assert [x["amount"] for x in fin["schedule"]][:2] == [0, 0]
    po = e.payout_view(n)
    assert po["hotel_net"] + po["platform_fee"] == fin["net_received"]


def test_on_busy_dates_the_margin_refuses_the_trade_and_the_buyer_is_told_why(tmp_path):
    e = Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))
    s = RulesSeller(e)
    n = _counter_on_price(e, s, 18000)
    v = n["versions"][-1]
    assert (v["terms"]["rate"], v["terms"]["deposit_pct"]) == (21000, 30)
    assert "can't go lower even with more paid up front" in v["message"]
    assert "in exchange" not in v["message"]
    prepaid = next(o for o in n["negotiation"][-1]["options"] if o["deposit_pct"] == 50)
    assert prepaid["verdict"] == "not allowed" and any("contribution margin" in r for r in prepaid["reasons"])


def test_the_seller_never_offers_less_than_was_asked(tmp_path):
    e = _quiet(tmp_path)
    s = RulesSeller(e)
    n = _counter_on_price(e, s, 20500)
    v = n["versions"][-1]
    assert (v["terms"]["rate"], v["terms"]["deposit_pct"]) == (20500, 50)
    assert v["message"].startswith("Thank you: we can do £205 a room-night if you pay 50% on signing")


def test_the_rules_page_keeps_prepayment_consistent():
    base = json.loads(SCENARIO.read_text())
    assert rules.validate(base) == []
    bad = copy.deepcopy(base)
    bad["rms"]["rates"]["twin"]["prepay_floor"] = 21500            # above the seller's own floor
    assert any("floor with prepayment" in p for p in rules.validate(bad))
    bad = copy.deepcopy(base)
    bad["policy"]["prepay"]["min_deposit_pct"] = 40                # not above the highest ordinary deposit
    assert any("Prepayment (40%)" in p for p in rules.validate(bad))
    paths = {e["path"] for e in rules.describe(base, base)["editable"]}
    assert {"rms.rates.twin.prepay_floor", "rms.rates.king.prepay_floor", "policy.prepay.min_deposit_pct"} <= paths
    assert "Prepayment: if the buyer pays at least 50% on signing" in rules.seller_brief(base)
