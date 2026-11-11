"""The money trail: every provider object behind a deal's money, and whether it reconciles.

Run: PYTHONPATH=src python -m pytest tests/test_money_trail.py -q
"""
from __future__ import annotations

import json

import pytest

from agentbank.groupsales.buyer import act
from agentbank.groupsales.engine import Engine
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller
from test_group_sales import buyer_turn, deal_by, run_to_confirmed


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("GS_HOTEL_ACCOUNT", "acct_test_harbour")
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def _story(e):
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")                         # change request
    s.run()
    buyer_turn(e, "Northstar")                         # accept v3: £528 owed
    s.run()
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    return n


def test_the_trail_lists_each_provider_object_in_order_and_reconciles(eng):
    e = eng
    n = _story(e)
    m = e.money_trail(n)
    assert m["provider"] == "simulated"
    assert [(r["kind"], r["amount"]) for r in m["rows"]] == [
        ("deposit", 384000), ("payout", 345600), ("refund", 52800), ("payout_reversal", 47520)]
    dep, split, refund, rev = m["rows"]
    assert dep["id"].startswith("int_") and dep["provider_status"] == "SUCCEEDED" and dep["sign"] == 1
    assert split["object"] == "Funds split" and "acct_test_harbour" in split["note"] and "£384" in split["note"]
    assert refund["id"].startswith("rfd_") and refund["status"] == "completed" and refund["sign"] == -1
    assert rev["object"] == "Funds split reversal" and "£52.80" in rev["note"]
    assert m["totals"] == {"buyer_paid": 384000, "refunded": 52800, "net_received": 331200,
                           "hotel_net": 298080, "platform_fee": 33120, "reconciles": True}


def test_a_second_refund_follows_the_first_in_the_order_things_happened(eng):
    e = eng
    n = _story(e)
    s = RulesSeller(e)
    act(e, n["id"], {"action": "change_request", "text": "Please release 1 more room a night.",
                     "payload": {"rooms_per_night": 1}})
    s.run()
    buyer_turn(e, "Northstar")                         # accept the change: £88 owed, £616 in total
    s.run()
    appr = [a for a in e.s["approvals"].values() if a["subject"] == "refund" and a["status"] == "pending"]
    e.decide_approval(appr[0]["id"], "approve", "hotel_manager")
    s.run()
    m = e.money_trail(n)
    assert [(r["kind"], r["amount"]) for r in m["rows"]] == [
        ("deposit", 384000), ("payout", 345600), ("refund", 52800), ("payout_reversal", 47520),
        ("refund", 8800), ("payout_reversal", 7920)]
    assert m["totals"]["reconciles"] is True and m["totals"]["net_received"] == 322400


def test_notifications_are_linked_to_the_deal_including_a_late_duplicate(eng):
    e = eng
    n = _story(e)
    e.release_notifications()                          # the held refund notice arrives late
    rid = e._refund_ops(n)[0]["provider_id"]
    again = {"id": f"evt_{rid}_done", "name": "refund.accepted",
             "data": {"object": {"id": rid, "metadata": {"run_id": e.s["run_id"]}}}}
    e.handle_event(again, verified=True)               # and again
    ev = e.money_trail(n)["events"]
    assert [x["result"] for x in ev] == ["re-read refund: ACCEPTED (already reconciled; ledger unchanged)",
                                         "duplicate ignored"]
    assert all(x["object_id"] == rid and x["name"] == "refund.accepted" for x in ev)
    assert e.money_trail(deal_by(e, "Summit"))["events"] == []


def test_before_payment_the_trail_shows_the_open_intent_only(eng):
    e = eng
    s, n = run_to_confirmed(e)
    sm = deal_by(e, "Summit")
    assert e.money_trail(sm)["rows"] == []
    act(e, sm["id"], {"action": "accept", "text": "We accept v1.", "payload": {"version": 1}})   # checkout opens
    s.run()
    rows = e.money_trail(sm)["rows"]
    assert [r["kind"] for r in rows] == ["deposit"] and rows[0]["status"] == "created"
    assert rows[0]["note"] == "waiting for the buyer to pay"
    assert e.money_trail(sm)["totals"]["reconciles"] is None      # nothing paid, nothing to split yet


def test_the_buyer_never_sees_the_money_trail(eng):
    e = eng
    n = _story(e)
    buyer = json.dumps(e.deal_view(n["id"], for_="buyer"))
    assert "money_trail" not in buyer and "fs_sim" not in buyer and "platform fee" not in buyer.lower()
    snap = next(d for d in e.snapshot()["deals"] if d["id"] == n["id"])
    assert snap["money_trail"]["totals"]["reconciles"] is True
