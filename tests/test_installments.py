"""Instalments: after the deposit, the seller collects the rest of the contract on the hotel's schedule
(50% paid in total 30 days before arrival, 100% 7 days before). The engine computes every amount from the
signed contract; the seller only asks for it and checks it.

Northstar: v3 £8,280 after the reduction, £3,312 net paid (£3,840 deposit less the £528 refund), arriving
10 March 2027. So £828 is due on 8 Feb (to £4,140) and £4,140 on 3 Mar (to £8,280): £4,968 in all.

Run: PYTHONPATH=src python -m pytest tests/test_installments.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentbank.groupsales import folio
from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import TOOLS, ClaudeSeller, RulesSeller
from test_group_sales import buyer_turn, deal_by, run_to_confirmed

CARD = "4035 5010 0000 0008"


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("GS_HOTEL_ACCOUNT", "acct_test_harbour")
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def story(e):
    """The demo through step 8: booked, reduced to v3, £528 refunded. Returns (seller, Northstar)."""
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


def ops(e, n):
    return e._installment_ops(n)


def pay_open(e, n):
    op = ops(e, n)[-1]
    e.provider.pay(op["payment_intent_id"], CARD)
    return e.check_balance_payment(n["id"], source="return page")


def ledger(e, n, typ):
    return [x for x in e.s["ledger"] if x["deal_id"] == n["id"] and x["type"] == typ]


def test_nothing_is_asked_for_before_the_first_due_date(eng):
    e = eng
    s, n = story(e)
    inst = e.installments(n)
    assert inst["due_now"] == 0 and inst["next"]["due"] == "2027-02-08" and inst["next"]["amount"] == 82800
    s.run()
    assert ops(e, n) == []
    with pytest.raises(Refused, match=r"nothing is due yet: 50% paid \(£828\) is due on 2027-02-08"):
        e.request_balance(n["id"])


def test_the_seller_asks_for_each_instalment_on_its_date_and_the_contract_ends_paid(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-02-08T09:00:00+00:00")
    assert e.waiting_on(n) == "seller: request instalment"
    s.run()
    [first] = ops(e, n)
    assert first["amount"] == 82800 and first["due_by"] == "2027-02-08" and first["status"] == "created"
    msg = n["thread"][-1]
    assert msg["from"] == "seller" and msg["installment_op"] == first["id"]
    assert "is due: £828." in msg["text"] and e.balance_url(n) in msg["text"]
    assert "50% of the total is due by 8 February 2027" in msg["text"] and "£4,140 of £8,280" in msg["text"]
    assert e.waiting_on(n) == "buyer: pay instalment"
    s.run()
    assert len(ops(e, n)) == 1                         # an open request is never made twice
    pay_open(e, n)
    assert first["status"] == "succeeded" and [x["amount"] for x in ledger(e, n, "installment_received")] == [82800]

    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    s.run()
    second = ops(e, n)[-1]
    assert second["amount"] == 414000 and second["due_by"] == "2027-03-03"
    pay_open(e, n)
    fin = e.finance(n)
    assert fin["net_received"] == 828000 and fin["still_due"] == 0
    assert e.installments(n)["due_now"] == 0 and e.waiting_on(n) is None
    s.run()
    assert len(ops(e, n)) == 2


def test_after_both_dates_one_request_covers_everything_due_and_the_hotel_is_paid_its_share(eng):
    """The demo's jump to 3 Mar: £4,968 in one request; hotel £4,471.20, Groupy fee £496.80."""
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    s.run()
    [op] = ops(e, n)
    assert op["amount"] == 496800 and op["milestones"] == ["50% paid", "100% paid"]
    pay_open(e, n)
    payout = next(o for o in e._payout_ops(n, "payout") if o["payment_intent_id"] == op["payment_intent_id"])
    assert payout["status"] == "paid" and payout["amount"] == 447120 and payout["commission"] == 49680
    pv = e.payout_view(n)
    assert pv["hotel_net"] == 298080 + 447120 and pv["platform_fee"] == 33120 + 49680
    trail = e.money_trail(n)
    assert trail["totals"]["net_received"] == 828000 and trail["totals"]["reconciles"] is True
    assert any(r["kind"] == "installment" and r["amount"] == 496800 for r in trail["rows"])


def test_checking_again_or_a_repeated_notification_records_the_payment_once(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    s.run()
    op = ops(e, n)[-1]
    e.provider.pay(op["payment_intent_id"], CARD)
    event = {"id": "evt_inst_1", "name": "payment_intent.succeeded",
             "data": {"object": {"id": op["payment_intent_id"], "metadata": {"run_id": e.s["run_id"]}}}}
    assert e.handle_event(event, verified=True)["result"].startswith("re-read intent")
    assert e.handle_event(event, verified=True)["result"] == "duplicate ignored"
    e.check_balance_payment(n["id"])
    e.check_balance_payment(n["id"])
    assert len(ledger(e, n, "installment_received")) == 1
    assert len([o for o in e._payout_ops(n, "payout") if o["payment_intent_id"] == op["payment_intent_id"]]) == 1


def test_a_timeout_is_looked_up_not_resubmitted(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    e.provider.knobs["timeout_next_create"] = True
    e.request_balance(n["id"])
    [op] = ops(e, n)
    assert op["status"] == "created" and op["payment_intent_id"]
    assert len(e.provider.find_intents(op["merchant_order_id"])) == 1
    e.request_balance(n["id"])                         # reused, not a second intent
    assert len(ops(e, n)) == 1


def test_nothing_is_requested_while_a_refund_is_unresolved(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")                         # accept v3; the £528 refund waits for the manager
    s.run()
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    assert e.installments(n)["blocked"]
    with pytest.raises(Refused, match="refund|finish that first"):
        e.request_balance(n["id"])
    s.run()
    assert ops(e, n) == []


def test_an_unpaid_request_goes_to_hotel_staff_after_the_grace_period(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    s.run()
    e.advance_clock(to="2027-03-06T09:00:00+00:00")
    assert e.waiting_on(n) == "buyer: pay instalment"
    e.advance_clock(to="2027-03-07T09:00:00+00:00")
    assert e.installments(n)["overdue"] and e.waiting_on(n) == "hotel staff: instalment overdue"
    s.run()
    assert len(ops(e, n)) == 1                         # no chasing with new requests


def test_the_booking_cannot_change_while_an_instalment_is_open(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-02-08T09:00:00+00:00")
    s.run()
    with pytest.raises(Refused, match="instalment payment is open"):
        e.prepare_reduction(n["id"], rooms_per_night=1)


def test_a_payment_that_does_not_match_its_request_is_held_for_staff(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    s.run()
    op = ops(e, n)[-1]
    e.provider.pay(op["payment_intent_id"], CARD)
    e.provider.db["intents"][op["payment_intent_id"]]["amount"] = 100
    e.check_balance_payment(n["id"])
    assert op["status"] == "mismatch" and ledger(e, n, "installment_received") == []
    assert e.waiting_on(n) == "hotel staff: instalment payment mismatch"
    with pytest.raises(Refused, match="didn't match"):
        e.request_balance(n["id"])


def test_the_final_folio_counts_instalments_so_only_the_extras_remain(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    s.run()
    pay_open(e, n)
    e.folio_action(n["id"], "demo_checkout", {})
    v = e.folio_action(n["id"], "simulate", {})
    assert v["paid"] == 828000 and v["airwallex_paid"] == 828000
    assert v["master_total"] == 978000 and v["balance_due"] == 150000
    assert "installment_received" in folio.RECEIPTS


def test_the_buyer_view_never_carries_instalment_internals(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    s.run()
    v = e.deal_view(n["id"], for_="buyer")
    text = repr(v)
    assert "client_secret" not in text and "acct_test_harbour" not in text


# ------------------------------------------------------------------ the Claude seller's tools
def test_the_claude_seller_has_the_instalment_tools_and_no_amount_to_choose():
    by = {t["name"]: t for t in TOOLS}
    for name in ("request_balance", "check_balance_payment"):
        assert set(by[name]["input_schema"]["properties"]) == {"deal_id"}


def test_the_claude_seller_is_told_how_to_handle_instalments(eng):
    from agentbank.groupsales.seller import system_prompt
    p = system_prompt(eng)
    assert "request_balance" in p and "never choose or change the amount" in p


class InstalmentClient:
    """Plays the model: request the instalment, then check it, then stop."""
    def __init__(self, ref):
        self.ref, self.turn = ref, 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.turn += 1
        calls = {1: "request_balance", 2: "request_balance", 3: "check_balance_payment"}
        if self.turn in calls:
            content = [SimpleNamespace(type="tool_use", id=f"t{self.turn}", name=calls[self.turn],
                                       input={"deal_id": self.ref})]
            return SimpleNamespace(stop_reason="tool_use", content=content)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done.")])


def test_the_claude_seller_requests_the_engine_computed_amount_once(eng):
    e = eng
    s, n = story(e)
    e.advance_clock(to="2027-03-03T09:00:00+00:00")
    assert "call request_balance" in e.next_step(n)
    ClaudeSeller(e, client=InstalmentClient(n["ref"])).run("clock")
    [op] = ops(e, n)                                   # asked twice, one request
    assert op["amount"] == 496800 and op["status"] == "created"
    assert "open: waiting for the buyer" in e.next_step(n)
