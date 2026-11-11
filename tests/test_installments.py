"""Overdue installments: the seller's collection decision after booking, enforced in code.

Run: PYTHONPATH=src python -m pytest tests/test_installments.py -q
"""
from __future__ import annotations

import json

import pytest

from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.policy import extension_check
from agentbank.groupsales.provider import SimulatedProvider
from test_group_sales import buyer_turn, deal_by, run_to_confirmed

CARD = "4035 5010 0000 0008"


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("GS_HOTEL_ACCOUNT", "acct_test_harbour")
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def _booked_and_reduced(e):
    """The demo story to 20 Jan: booked at £9,600, reduced to £8,280, £528 refunded. £3,312 net received."""
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")                         # change request
    s.run()
    buyer_turn(e, "Northstar")                         # accept v3: £528 owed
    s.run()
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    assert e.finance(n)["net_received"] == 331200 and n["status"] == "confirmed"
    return s, n


def _overdue(e):
    s, n = _booked_and_reduced(e)
    e.advance_clock(to="2027-02-09T09:00:00+00:00")   # the £828 installment was due 8 Feb
    return s, n


def _pending(e):
    return [a for a in e.s["approvals"].values() if a["status"] == "pending"]


def _intents(e, n):
    return [o for o in e.s["operations"].values() if o["deal_id"] == n["id"] and o["kind"] == "installment"
            and o["status"] != "cancelled"]


def test_the_demo_story_overdue_link_the_seller_decides_the_date_then_paid_and_split(eng):
    e = eng
    s, n = _overdue(e)
    assert n["status"] == "installment_overdue"
    cv = e.collection_view(n)
    assert cv["current"]["amount"] == 82800 and cv["current"]["due"] == "2027-02-08" and cv["days_late"] == 1
    r = cv["risk"]
    assert (r["net_received"], r["paid_pct"], r["demand_pct"], r["busy"], r["seller_extension_days"]) == (331200, 40, 75, True, 3)
    assert (r["days_to_arrival"], r["release_deadline"], r["cancellation_pct_today"], r["cancellation_charge_today"]) == \
        (29, "2027-02-24", 50, 414000)

    s.run()                                            # the seller sends a payment link for exactly £828
    assert n["status"] == "installment_requested"
    [op] = _intents(e, n)
    assert op["amount"] == 82800 and op["payment_intent_id"].startswith("int_")
    assert "£828" in n["thread"][-1]["text"] and "8 February" in n["thread"][-1]["text"]

    buyer_turn(e, "Northstar")                         # "could we pay by 15 February?"
    assert n["thread"][-1]["action"] == "payment_extension" and "£828" in n["thread"][-1]["text"]
    s.run()                         # 7 days > its 3 on a busy week: the seller gives its own 3, and says why
    assert not _pending(e) and n["status"] == "installment_requested"
    assert e.collection_view(n)["current"]["due"] == "2027-02-11"
    reply = n["thread"][-1]["text"]
    assert "high demand" in reply and "11 February" in reply and "£828" in reply
    assert n["extensions"]["50% paid"]["history"][0]["approval"] is None
    assert any("Offering Thursday 11 February instead" in a["text"] and "75% (busy)" in a["text"]
               for a in e.s["activity"] if a["kind"] == "reasoning")
    assert [o["id"] for o in _intents(e, n)] == [op["id"]]   # the same payment link: only the date moved

    e.provider.pay(op["payment_intent_id"], CARD)
    e.check_payment(n["id"], source="return page")
    e.check_payment(n["id"], source="return page")     # a second look changes nothing
    assert n["status"] == "confirmed" and op["status"] == "succeeded"
    assert [x["amount"] for x in e.s["ledger"] if x["type"] == "installment_received"] == [82800]
    fin, pv = e.finance(n), e.payout_view(n)
    assert (fin["net_received"], fin["still_due"]) == (414000, 414000)
    split = next(o for o in e.s["operations"].values() if o["kind"] == "payout" and o.get("source_op") == op["id"])
    assert (split["amount"], split["commission"], split["status"]) == (74520, 8280, "paid")
    assert pv["hotel_net"] + pv["platform_fee"] == fin["net_received"]
    assert e.collection_view(n)["current"] == {**e.collection_view(n)["current"], "label": "100% paid", "amount": 414000,
                                               "due": "2027-03-03", "overdue": False}


def test_on_quiet_dates_the_seller_gives_the_same_week_alone(eng):
    e = eng
    s, n = _overdue(e)
    e.scenario["rms"]["displacement"]["demand_by_night_pct"] = {k: 40 for k in n["booking"]["rooms"]}
    s.run()
    buyer_turn(e, "Northstar")                         # 15 Feb: 7 days, within the seller's own 7 on quiet dates
    s.run()
    assert not _pending(e)
    assert n["status"] == "installment_requested" and e.collection_view(n)["current"]["due"] == "2027-02-15"
    hist = n["extensions"]["50% paid"]["history"]
    assert hist == [{**hist[0], "from": "2027-02-08", "to": "2027-02-15", "approval": None}]


def test_quiet_dates_beyond_a_week_get_the_sellers_week(eng):
    e = eng
    s, n = _overdue(e)
    e.scenario["rms"]["displacement"]["demand_by_night_pct"] = {k: 40 for k in n["booking"]["rooms"]}
    s.run()
    e.buyer_action(n["id"], n["buyer_token"], "payment_extension", "By the 20th?", {"pay_by": "2027-02-20"})
    s.run()
    assert not _pending(e) and e.collection_view(n)["current"]["due"] == "2027-02-15"
    assert "up to 7 days" in n["thread"][-1]["text"]


def test_the_manager_decides_what_the_seller_cant_a_second_move(eng):
    e = eng
    s, n = _overdue(e)
    s.run()
    buyer_turn(e, "Northstar")                         # 15 Feb asked; the seller gives 11 Feb
    s.run()
    e.buyer_action(n["id"], n["buyer_token"], "payment_extension", "Could we have until the 13th?",
                   {"pay_by": "2027-02-13"})
    s.run()                                            # no date of its own left: the manager decides
    assert n["status"] == "extension_review"
    [a] = _pending(e)
    assert a["subject"] == "installment_extension" and a["pay_by"] == "2027-02-13" and a["amount"] == 82800
    assert {x["rule"] for x in a["rules"]} == {"Installment extension (busy dates)", "Extensions per installment"}
    assert "40%" in a["summary"] and "£4,140" in a["summary"] and "24 Feb" in a["summary"]
    e.decide_approval(a["id"], "approve", "hotel_manager")
    s.run()
    assert n["status"] == "installment_requested" and e.collection_view(n)["current"]["due"] == "2027-02-13"
    assert "13 February" in n["thread"][-1]["text"]


def test_a_date_past_the_release_deadline_is_refused_and_the_seller_offers_its_own(eng):
    e = eng
    s, n = _overdue(e)
    s.run()
    with pytest.raises(Refused, match="release deadline"):
        e.extend_installment(n["id"], "2027-02-25", "too late")
    act_ = e.buyer_action(n["id"], n["buyer_token"], "payment_extension", "Can we pay on 1 March?",
                          {"pay_by": "2027-03-01"})
    assert act_["recorded"]
    s.run()
    assert not _pending(e)                             # nobody could approve it: the seller offers what it may
    assert e.collection_view(n)["current"]["due"] == "2027-02-11"
    assert "11 February" in n["thread"][-1]["text"]


def test_a_manager_refusal_leads_to_the_sellers_own_date(eng):
    e = eng
    s, n = _overdue(e)
    s.run()
    assert e.extend_installment(n["id"], "2027-02-15", "asked")["status"] == "pending_approval"   # e.g. the Claude seller
    [a] = _pending(e)
    e.decide_approval(a["id"], "reject", "hotel_manager")
    s.run()
    assert e.collection_view(n)["current"]["due"] == "2027-02-11"
    assert "11 February" in n["thread"][-1]["text"] and n["status"] == "installment_requested"
    r = e.extend_installment(n["id"], "2027-02-15", "again")   # the declined date can't come back unapproved
    assert r["status"] == "pending_approval" and e.collection_view(n)["current"]["due"] == "2027-02-11"


def test_overdue_again_after_an_extension_goes_to_a_person_with_the_numbers(eng):
    e = eng
    s, n = _overdue(e)
    s.run()
    buyer_turn(e, "Northstar")                         # moved to 11 Feb by the seller
    s.run()
    e.advance_clock(to="2027-02-12T09:00:00+00:00")   # 11 Feb passed, still unpaid
    assert n["status"] == "installment_overdue"
    s.run()
    assert n["status"] == "handoff"
    assert "£4,140" in n["handoff"]["todo"] and "£3,312" in n["handoff"]["todo"]
    assert len(_intents(e, n)) == 1                    # no second payment link


def test_a_second_extension_needs_the_manager_even_within_the_sellers_days(eng):
    e = eng
    s, n = _overdue(e)
    e.scenario["rms"]["displacement"]["demand_by_night_pct"] = {k: 40 for k in n["booking"]["rooms"]}
    s.run()
    e.extend_installment(n["id"], "2027-02-10", "moved")
    r = e.extend_installment(n["id"], "2027-02-12", "moved again")
    assert r["status"] == "pending_approval"
    assert any(x["rule"] == "Extensions per installment" for x in _pending(e)[0]["rules"])


def test_a_webhook_for_the_installment_is_verified_and_recorded_once(eng):
    e = eng
    s, n = _overdue(e)
    s.run()
    [op] = _intents(e, n)
    e.provider.pay(op["payment_intent_id"], CARD)
    evt = {"id": "evt_inst_1", "name": "payment_intent.succeeded",
           "data": {"object": {"id": op["payment_intent_id"], "metadata": {"run_id": e.s["run_id"]}}}}
    e.handle_event(evt, verified=True)
    e.handle_event(evt, verified=True)                 # duplicate
    e.handle_event({**evt, "id": "evt_inst_2"}, verified=True)
    assert [x["amount"] for x in e.s["ledger"] if x["type"] == "installment_received"] == [82800]
    assert len([o for o in e.s["operations"].values() if o["kind"] == "payout" and o.get("source_op") == op["id"]]) == 1
    assert n["status"] == "confirmed"


def test_a_declined_card_leaves_the_installment_open(eng):
    e = eng
    s, n = _overdue(e)
    s.run()
    [op] = _intents(e, n)
    e.provider.pay(op["payment_intent_id"], "4000 0000 0000 0002")
    e.check_payment(n["id"], source="return page")
    assert op["status"] == "created" and n["status"] == "installment_requested"
    assert not [x for x in e.s["ledger"] if x["type"] == "installment_received"]


def test_the_buyer_sees_what_it_owes_and_never_the_hotels_risk(eng):
    e = eng
    s, n = _overdue(e)
    s.run()
    v = e.deal_view(n["id"], for_="buyer")
    assert v["installment"]["amount"] == 82800 and v["installment"]["due"] == "2027-02-08" and v["installment"]["pay_url"]
    text = json.dumps(v)
    for secret in ("demand", "release_deadline", "cancellation_charge", "busy", "seller_extension", "collection"):
        assert secret not in text
    assert e.deal_view(n["id"])["collection"]["risk"]["busy"] is True     # the seller and the hotel see it


def test_nothing_is_overdue_before_the_date_or_while_a_refund_is_unresolved(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-02-08T09:00:00+00:00")   # due today: not yet overdue
    assert n["status"] == "confirmed"
    assert e.collection_view(n)["current"] == {**e.collection_view(n)["current"], "amount": 96000, "overdue": False}
    assert deal_by(e, "Summit")["status"] != "installment_overdue"


@pytest.mark.parametrize("new_due,used,demand,verdict", [
    ("2027-02-11", 0, 80, "alone"),          # 3 days on busy dates
    ("2027-02-12", 0, 80, "manager"),        # 4 days on busy dates
    ("2027-02-15", 0, 40, "alone"),          # 7 days on quiet dates
    ("2027-02-16", 0, 40, "manager"),        # 8 days on quiet dates
    ("2027-02-10", 1, 40, "manager"),        # a second move
    ("2027-02-25", 0, 40, "hard"),           # after the release deadline
    ("2027-02-09", 0, 40, "hard"),           # not after today
])
def test_extension_rule(eng, new_due, used, demand, verdict):
    chk = extension_check("2027-02-08", "2027-02-08", new_due, used, demand, "2027-02-24", "2027-02-09", eng.policy)
    got = "hard" if chk["hard"] else "manager" if chk["approvals"] else "alone"
    assert got == verdict
