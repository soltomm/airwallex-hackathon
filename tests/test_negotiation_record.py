"""The negotiation record: what the seller priced before it answered, for the hotel dashboard only.

Run: PYTHONPATH=src python -m pytest tests/test_negotiation_record.py -q
"""
from __future__ import annotations

import json

import pytest

from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller
from test_group_sales import buyer_turn, deal_by


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def _rounds(d):
    return [r for r in d.get("negotiation", []) if r["options"]]


def test_a_counter_records_the_ask_as_written_and_the_counter_chosen(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")                     # answers -> v1
    s.run()
    buyer_turn(e, "Northstar")                     # counter: 20% attrition, 25% deposit
    s.run()
    n = deal_by(e, "Northstar")
    first, counter = _rounds(n)
    assert first["ask"]["summary"].startswith("Inquiry")
    assert first["chosen"]["version"] == 1 and first["chosen"]["status"] == "sent"

    assert counter["ask"]["summary"] == "Counteroffer: 20% attrition, 25% deposit"
    ask, chosen = counter["options"]
    assert (ask["deposit_pct"], ask["attrition_pct"], ask["verdict"]) == (25, 20, "not allowed")
    assert any("requires a 40% deposit" in r for r in ask["reasons"])
    assert (chosen["rate"], chosen["deposit_pct"], chosen["attrition_pct"]) == (22000, 40, 20)
    assert chosen["verdict"] == "needs the manager" and chosen["deposit"] == 384000
    assert counter["chosen"] == {**counter["chosen"], "key": chosen["key"], "version": 2, "status": "pending_approval"}
    assert "we keep the £220 room rate" in counter["chosen"]["explanation"]


def test_a_refused_proposal_is_recorded_and_closes_no_round(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    n = deal_by(e, "Northstar")
    with pytest.raises(Refused):
        e.propose_offer(n["id"], 18000, 30, 10, "a rate below the absolute minimum")
    last = n["negotiation"][-1]
    assert "chosen" not in last
    opt = last["options"][-1]
    assert opt["rate"] == 18000 and opt["verdict"] == "not allowed"
    assert any("absolute minimum" in r for r in opt["reasons"])


def test_the_record_is_bounded(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    n = deal_by(e, "Northstar")
    for rate in range(21000, 23000, 100):           # 20 dry runs in one round
        e.price_terms(n["id"], rate, 30, 10)
    assert len(n["negotiation"][-1]["options"]) == Engine.MAX_OPTIONS_PER_ROUND
    e.price_terms(n["id"], 22900, 30, 10)            # a repeat replaces, it doesn't duplicate
    assert [o["rate"] for o in n["negotiation"][-1]["options"]].count(22900) == 1


def test_the_buyer_never_sees_the_negotiation_record(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    n = deal_by(e, "Northstar")
    assert _rounds(n)
    buyer = json.dumps(e.deal_view(n["id"], for_="buyer"))
    assert "negotiation" not in buyer and "margin" not in buyer and "needs the manager" not in buyer
    assert any(d.get("negotiation") for d in e.snapshot()["deals"])      # the hotel's dashboard has it
