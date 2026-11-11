"""A competing group changes the seller's decision: firm demand for the same nights counts in the margin.

Run: PYTHONPATH=src python -m pytest tests/test_competing_demand.py -q
"""
from __future__ import annotations

import json

import pytest

from agentbank.groupsales import calc, generator
from agentbank.groupsales.engine import Refused

from test_group_sales import H_ALL, MARCH, RMS, buyer_turn, deal_by, eng, northstar_terms  # noqa: F401
from agentbank.groupsales.seller import RulesSeller


def to_first_offer(e):
    """Reset state through Northstar's v1 (its 12 twins held) and Summit's quote."""
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")                 # answers
    s.run()
    n = deal_by(e, "Northstar")
    assert n["status"] == "offer_sent" and n["versions"][-1]["terms"]["attrition_pct"] == 10
    return s, n


def competitor(e, **traits):
    r = generator.send(e, generator.generate(e, preset="competing_group", seed=7, traits=traits))
    return e.deal(r["deal_id"])


def pending(e, deal_id):
    return [a for a in e.s["approvals"].values() if a["deal_id"] == deal_id and a["status"] == "pending"]


# ------------------------------------------------------------------------------ calculation
def test_firm_demand_moves_the_margin_not_the_price():
    plain = calc.price(northstar_terms(), H_ALL, RMS)
    firm = {"rooms": {n: 5 for n in MARCH}, "groups": [{"ref": "GS-9", "company": "X", "rooms": 7, "nights": MARCH}]}
    busy = calc.price(northstar_terms(), H_ALL, RMS, firm)
    assert (busy["total"], busy["deposit"], busy["lines"]) == (plain["total"], plain["deposit"], plain["lines"])
    # The five rooms a night another group wants carry the full £80, not 80% / 80% / 64% of it.
    extra = sum(5 * (8000 - 8000 * pct // 100) for pct in (80, 80, 64))
    assert busy["displacement"]["blocked"] == plain["displacement"]["blocked"] + extra
    assert [x["competing_rooms"] for x in busy["displacement"]["nights"]] == [5, 5, 5]
    assert busy["displacement"]["firm"] == firm
    # On the demo dates that is the difference between the 20% trade passing the 40% floor and not.
    assert plain["margin_full_release"]["margin_pct"] >= 40 > busy["margin_full_release"]["margin_pct"]
    assert calc.price(northstar_terms(10, 30), H_ALL, RMS, firm)["margin_full_release"]["margin_pct"] >= 40


def test_firm_rooms_never_exceed_the_block():
    firm = {"rooms": {n: 50 for n in MARCH}, "groups": []}
    p = calc.price(northstar_terms(), H_ALL, RMS, firm)
    assert p["displacement"]["blocked"] == 12 * 8000 * 3


# ------------------------------------------------------------------------------ the decision
def test_without_a_competitor_the_seller_trades_attrition_for_deposit(eng):
    s, n = to_first_offer(eng)
    buyer_turn(eng, "Northstar")               # counter: 20% attrition, 25% deposit
    s.run()
    v = n["versions"][-1]
    assert (v["terms"]["attrition_pct"], v["terms"]["deposit_pct"], v["status"]) == (20, 40, "pending_approval")
    assert not v["pricing"]["displacement"].get("firm")


def test_a_competing_group_makes_the_seller_hold_the_standard_release(eng):
    s, n = to_first_offer(eng)
    c = competitor(eng)
    s.run()
    assert (c["requirements"]["rooms"], c["requirements"]["room_type"]) == (7, "twin")   # 2 free beside Northstar
    assert c["status"] == "dates_proposed"     # it can't fit: the seller offers it other dates
    buyer_turn(eng, "Northstar")               # the same counter as above
    s.run()
    # It doesn't trade: v1 (10% attrition, 30% deposit) stays open, same hold, nothing for the manager.
    v = n["versions"][-1]
    assert len(n["versions"]) == 1 and (v["terms"]["attrition_pct"], v["terms"]["deposit_pct"]) == (10, 30)
    assert v["status"] == "sent" and (v["pricing"]["total"], v["pricing"]["deposit"]) == (960000, 288000)
    assert not pending(eng, n["id"])
    notes = [a["text"] for a in eng.s["activity"] if a["deal_id"] == n["id"] and a["kind"] == "reasoning"]
    assert any(t.startswith("Decision changed by competing demand") and "43.1%" in t and "20% attrition" in t
               for t in notes)
    rec = next(r for r in n["records"] if r["system"] == "Competing demand")
    assert c["ref"] in rec["summary"]
    reply = n["thread"][-1]["text"]
    assert "another group has asked for the same nights" in reply.lower() and "stays open" in reply
    assert c["company"] not in reply and c["ref"] not in reply
    assert buyer_turn(eng, "Northstar")["accepted"] is True      # v1 can be accepted as it stands


def test_when_the_competitor_takes_other_dates_the_trade_comes_back(eng):
    s, n = to_first_offer(eng)
    c = competitor(eng)
    s.run()
    eng.buyer_action(c["id"], c["buyer_token"], "reply", "We'll take 13 to 16 March.",
                     {"dates": {"arrival": "2027-03-13", "departure": "2027-03-16"}})
    s.run()
    assert c["requirements"]["arrival"] == "2027-03-13"
    buyer_turn(eng, "Northstar")
    s.run()
    v = n["versions"][-1]
    assert (v["terms"]["attrition_pct"], v["terms"]["deposit_pct"], v["status"]) == (20, 40, "pending_approval")


def test_the_buyer_views_never_show_the_other_group(eng):
    s, n = to_first_offer(eng)
    c = competitor(eng)
    s.run()
    buyer_turn(eng, "Northstar")
    s.run()
    mine = json.dumps(eng.deal_view(n["id"], for_="buyer"))
    assert c["company"] not in mine and c["ref"] not in mine and "competing" not in mine
    theirs = json.dumps(eng.deal_view(c["id"], for_="buyer"))
    assert "Northstar" not in theirs and n["ref"] not in theirs


# ------------------------------------------------------------------------------ who counts
def test_only_open_unserved_requests_for_the_same_rooms_count(eng):
    s, n = to_first_offer(eng)
    terms = n["versions"][-1]["terms"]
    assert eng.competing_demand(n, terms) is None              # Summit wants kings, and holds them
    small = eng.ingest_email("Kim <kim@small.example>", "Two rooms",
                             "Please quote 2 twin rooms for 4 guests arriving 10 March and departing 13 March 2027, "
                             "with breakfast. Billed to Small Demo Ltd. Budget £3,000 inclusive of taxes.", wake=False)
    s.run()
    assert eng.deal(small["deal_id"])["status"] == "offer_sent"  # it fits in the 2 twins left, and holds them
    assert eng.competing_demand(n, terms) is None
    c = competitor(eng)                                        # 2 + 5: now nothing is left beside Northstar
    assert eng.competing_demand(n, terms) is None               # not read yet: no requirements on file
    s.run()
    assert c["status"] == "dates_proposed"
    assert eng.competing_demand(n, terms)["rooms"] == {night: 5 for night in MARCH}
    eng.decline(c["id"], "test", "No rooms, sorry.")
    assert eng.competing_demand(n, terms) is None


def test_the_preset_needs_an_open_offer_to_compete_with(eng):
    with pytest.raises(Refused, match="no open offer is holding twin rooms"):
        generator.generate(eng, preset="competing_group", seed=1)
