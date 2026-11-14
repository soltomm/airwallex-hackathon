"""Typed buyer replies: the buyer negotiates in its own words and the backend reads the move.

The buyer's mailbox has no pre-written drafts, so a typed counter, acceptance or change request has to reach the
seller as the same move a draft used to carry. Runs offline (simulated provider, rules seller, pattern reading).
"""
from __future__ import annotations

import pytest

from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.intent import accepts, counter_terms, interpret, rooms_released
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller

TERMS = {"rate": 22000, "deposit_pct": 30, "attrition_pct": 10}


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def northstar(e):
    return next(d for d in e.s["deals"].values() if d["company"].startswith("Northstar"))


def say(e, d, text, action="message"):
    return e.buyer_action(d["id"], d["buyer_token"], action, text)


# ------------------------------------------------------------------------------ reading
@pytest.mark.parametrize("text,ask", [
    ("Attendance is still uncertain, so we need a 20% attrition allowance. We propose a 25% deposit.",
     {"attrition_pct": 20, "deposit_pct": 25}),
    ("Could you do £195 per room-night?", {"rate": 19500}),
    ("Can the rate come down to £200?", {"rate": 20000}),
    ("Thank you, the £220 room rate works for us, and a 30% deposit is fine.", {}),     # restates, asks nothing
    ("We could pay £240 a night.", {}),                                                # a higher rate isn't an ask
    ("We'd like the deposit at 40% and attrition of 20%.", {"deposit_pct": 40, "attrition_pct": 20}),
])
def test_counter_terms(text, ask):
    assert counter_terms(text, TERMS) == ask


@pytest.mark.parametrize("text,ok", [
    ("We accept.", True), ("Thanks, we're happy to accept offer v2.", True), ("Agreed, please go ahead.", True),
    ("We can't accept this.", False), ("We accept if you add breakfast.", False), ("Do you accept cards?", False),
    ("I need to check with our organizer before we accept.", False), ("Thanks for the quote.", False),
])
def test_accepts(text, ok):
    assert accepts(text) is ok


@pytest.mark.parametrize("text,n", [
    ("Please reduce our block by two twin rooms for all three nights.", 2),
    ("We need to release 3 rooms each night.", 3), ("Four fewer rooms, please.", 4),
    ("Can we go down to 9 rooms a night?", 3), ("We need fewer rooms.", None),
])
def test_rooms_released(text, n):
    assert rooms_released(text, 12) == n


# ------------------------------------------------------------------------------ the demo, typed
def test_the_northstar_demo_runs_on_typed_replies(eng):
    e, s = eng, RulesSeller(eng)
    s.run()
    n = northstar(e)
    assert n["status"] == "needs_info"
    say(e, n, "Classroom seating for the meeting room. The legal payer is Northstar Analytics Demo Ltd. "
              "Our total budget is inclusive of taxes.", action="reply")
    s.run()
    v1 = n["versions"][-1]
    assert n["status"] == "offer_sent" and v1["pricing"]["total"] == 960000

    say(e, n, "Thank you, the £220 room rate works for us. Attendance is still uncertain, so we need a 20% "
              "attrition allowance. We propose a 25% deposit.")
    m = n["thread"][-1]
    assert m["action"] == "counter" and m["payload"] == {"attrition_pct": 20, "deposit_pct": 25} and m["read_from_text"]
    s.run()
    v2 = n["versions"][-1]
    assert n["status"] == "pending_approval" and v2["terms"]["rate"] == 22000 and v2["pricing"]["deposit"] == 384000
    appr = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()

    say(e, n, "Thank you. We accept.")
    assert n["status"] == "accepted" and n["accepted_version"] == v2["n"]
    s.run()
    op = e._deposit_op(n)
    e.provider.pay(op["payment_intent_id"], "4035 5010 0000 0008")
    e.check_payment(n["id"], source="return page")
    assert n["booking"]["status"] == "confirmed"

    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    say(e, n, "Four colleagues can no longer attend. Please reduce our block by two twin rooms for all three nights.")
    assert n["thread"][-1]["action"] == "change_request" and n["thread"][-1]["payload"] == {"rooms_per_night": 2}
    s.run()
    v3 = n["versions"][-1]
    assert n["status"] == "amendment_sent" and v3["pricing"]["total"] == 828000
    say(e, n, "Agreed, please go ahead.")
    assert n["finance"]["refund_owed"] == 52800


def test_a_hedged_or_unclear_reply_stays_a_message(eng):
    e, s = eng, RulesSeller(eng)
    s.run()
    summit = next(d for d in e.s["deals"].values() if d["company"].startswith("Summit"))
    assert summit["status"] == "offer_sent"
    say(e, summit, "Thanks for the quote. I'm taking it to our organizer and will come back to you.")
    assert summit["thread"][-1]["action"] == "message" and summit["status"] == "offer_sent"
    say(e, summit, "We'd accept if you could include parking?")
    assert summit["thread"][-1]["action"] == "message" and summit["status"] == "offer_sent"


def test_typed_text_cannot_accept_an_offer_that_is_not_current(eng):
    e, s = eng, RulesSeller(eng)
    s.run()
    n = northstar(e)
    assert interpret(n, "We accept.") is None                          # questions pending: no offer to accept
    with pytest.raises(Refused):
        e.buyer_action(n["id"], n["buyer_token"], "accept", payload={"version": 1})
