"""Group-sales checks: calculations, authority, inventory, versions, and uncertain payment recovery.

Run: PYTHONPATH=src python -m pytest tests/test_group_sales.py -q
Everything runs against the simulated provider; no network, no API keys.
"""
from __future__ import annotations

import json

import pytest

from agentbank.groupsales import calc
from agentbank.groupsales.buyer import act, suggest
from agentbank.groupsales.engine import SCENARIO, Engine, Refused
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller

S = json.loads(SCENARIO.read_text())
H, RMS, POL = S["hotel"], S["rms"], S["policy"]


def northstar_terms(att=20, dep=40, rate=22000):
    return {"room_type": "twin", "rate": rate, "rooms": {n: 12 for n in H["nights"]}, "released": {n: 0 for n in H["nights"]},
            "breakfast": {m: 24 for m in ("2027-03-11", "2027-03-12", "2027-03-13")},
            "meeting": {"room": "thames", "date": "2027-03-11", "layout": "classroom", "attendees": 24},
            "attrition_pct": att, "deposit_pct": dep, "currency": "GBP"}


@pytest.fixture
def eng(tmp_path):
    p = SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0)
    return Engine(tmp_path / "var", p)


def deal_by(e, company):
    return next(d for d in e.s["deals"].values() if d["company"].startswith(company))


def buyer_turn(e, company):
    d = deal_by(e, company)
    s = suggest(e, d["id"])
    assert s, f"buyer {company} has nothing to do in {d['status']}"
    return act(e, d["id"], s)


def pay(e, d):
    op = e._deposit_op(d)
    e.provider.pay(op["payment_intent_id"], "4035 5010 0000 0008")
    return e.check_payment(d["id"], source="return page")


def run_to_confirmed(e):
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")               # answers
    s.run()
    buyer_turn(e, "Northstar")               # counter 20% / 25%
    s.run()
    n = deal_by(e, "Northstar")
    appr = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    r = buyer_turn(e, "Northstar")           # accept v2 -> organizer needed
    assert r["accepted"] is False
    e.decide_approval(r["approval_id"], "approve", "organizer")
    s.run()
    pay(e, n)
    return s, n


# ------------------------------------------------------------------------------ calculations
def test_spec_figures():
    p = calc.price(northstar_terms(10, 30), H, RMS)
    assert (p["total"], p["deposit"]) == (960000, 288000)
    p = calc.price(northstar_terms(), H, RMS)
    assert (p["total"], p["deposit"], p["attrition_allowance"]) == (960000, 384000, 7)
    assert p["margin_all_rooms"] == {"revenue": 960000, "costs": 313200, "contribution": 646800, "margin_pct": 67.4}
    assert p["margin_full_release"]["revenue"] == 806000 and p["margin_full_release"]["costs"] == 264200
    r = calc.evaluate_reduction(northstar_terms(), {n: 2 for n in H["nights"]}, "2027-01-20", H, POL, RMS, 384000)
    assert (r["new_pricing"]["total"], r["revised_deposit"], r["refund_due"], r["remaining_after"]) == (828000, 331200, 52800, 1)
    assert r["new_pricing"]["margin_all_rooms"]["costs"] == 271200
    sched = calc.schedule(828000, 40, 331200, H, POL, "2027-01-11")
    assert [x["amount"] for x in sched] == [0, 82800, 414000]


def test_summit_offer():
    t = {"room_type": "king", "rate": 19000, "rooms": {n: 8 for n in H["nights"]}, "released": {},
         "breakfast": {m: 8 for m in ("2027-03-11", "2027-03-12", "2027-03-13")}, "meeting": None,
         "attrition_pct": 10, "deposit_pct": 30}
    p = calc.price(t, H, RMS)
    assert (p["total"], p["deposit"]) == (492000, 147600)


def test_cumulative_attrition_and_rules():
    t = northstar_terms()
    first = calc.evaluate_reduction(t, {n: 2 for n in H["nights"]}, "2027-01-20", H, POL, RMS, 384000)
    later = calc.evaluate_reduction(first["new_terms"], {"2027-03-10": 2}, "2027-01-25", H, POL, RMS, 331200)
    assert later["already_released"] == 6 and not later["within_allowance"] and later["needs_manager_review"]
    nine = calc.evaluate_reduction(t, {n: 3 for n in H["nights"]}, "2027-01-20", H, POL, RMS, 384000)
    assert not nine["within_allowance"] and nine["refund_due"] == 0
    late = calc.evaluate_reduction(t, {n: 2 for n in H["nights"]}, "2027-02-25", H, POL, RMS, 384000)
    assert not late["before_release_deadline"] and late["needs_manager_review"]
    after_inst = calc.evaluate_reduction(t, {n: 2 for n in H["nights"]}, "2027-02-10", H, POL, RMS, 384000)
    assert after_inst["refund_mode"] == "manager reconciliation" and after_inst["refund_due"] == 0


# ------------------------------------------------------------------------------ the demo story
def test_full_story(eng):
    e = eng
    s, n = run_to_confirmed(e)
    sm = deal_by(e, "Summit")
    assert sm["status"] == "offer_sent" and sm["versions"][0]["pricing"]["total"] == 492000
    assert any("earlier deadline" in a["text"] for a in e.s["activity"])
    assert n["status"] == "confirmed"
    v2 = n["versions"][-1]
    assert (v2["terms"]["rate"], v2["terms"]["attrition_pct"], v2["terms"]["deposit_pct"]) == (22000, 20, 40)
    inv = e.s["inventory"]["2027-03-10"]["twin"]
    assert (inv["held"], inv["booked"]) == (0, 12)
    assert [x["amount"] for x in e.s["ledger"]] == [384000]

    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    assert deal_by(e, "Summit")["status"] == "expired"          # unaccepted hold lapsed and released
    assert e.s["inventory"]["2027-03-10"]["king"]["held"] == 0
    buyer_turn(e, "Northstar")                                  # change request
    s.run()
    assert n["status"] == "amendment_sent"
    buyer_turn(e, "Northstar")                                  # accept amendment
    assert n["finance"]["refund_owed"] == 52800
    assert e.s["inventory"]["2027-03-10"]["twin"]["booked"] == 10
    s.run()                                                      # refund above £500 -> approval
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    assert appr["amount"] == 52800 and appr["status"] == "pending"
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    fin = e.finance(n)
    assert fin["refunded"] == 52800 and fin["net_received"] == 331200 and fin["still_due"] == 496800
    assert [x["amount"] for x in fin["schedule"]] == [0, 82800, 414000]
    ops = e.ops_instructions(n)
    assert "20" in ops["front_office"] and "24" in ops["food_and_beverage"]
    # held notification arrives late and twice: ledger changes once only
    assert e.release_notifications()["delivered"] == 1
    evt = {"id": "evt_again", "name": "refund.accepted", "data": {"object": {"id": e._refund_ops(n)[0]["provider_id"],
                                                                              "metadata": {"run_id": e.s["run_id"]}}}}
    e.handle_event(evt, verified=True)
    e.handle_event(evt, verified=True)
    assert len([x for x in e.s["ledger"] if x["type"] == "refund_completed"]) == 1
    assert e.s["inventory"]["2027-03-10"]["twin"]["booked"] == 10


# ------------------------------------------------------------------------------ authority & versions
def test_stale_approval_and_hard_limits(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    n = deal_by(e, "Northstar")
    old = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    with pytest.raises(Refused):
        e.decide_approval(old["id"], "approve", "organizer")          # wrong approver
    e.propose_offer(n["id"], 21000, 40, 20, "revised")                # materially changed offer
    assert e.s["approvals"][old["id"]]["status"] == "stale"
    with pytest.raises(Refused):
        e.decide_approval(old["id"], "approve", "hotel_manager")
    with pytest.raises(Refused):
        e.send_offer(n["id"], n["versions"][-1]["n"])                 # not approved
    with pytest.raises(Refused):
        e.propose_offer(n["id"], 19000, 40, 10, "too low")            # below absolute minimum
    with pytest.raises(Refused):
        e.propose_offer(n["id"], 22000, 30, 20, "attrition without 40% deposit")
    with pytest.raises(Refused):
        e.buyer_action(n["id"], "wrong-token", "accept", payload={"version": 1})


def test_no_overselling(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()                                                          # holds 12 of 13 twins
    e.ingest_email("Ops <ops@other.example>", "Group RFP", "Please quote 5 twin rooms for 10 guests arriving 10 March "
                   "and departing 13 March 2027. No meeting room is needed. Billed to Other Corp Ltd. Reply by 14 "
                   "January at noon.", wake=False)
    s.run()
    other = deal_by(e, "other.example")
    v = other["versions"][-1]
    assert v["terms"]["rooms"]["2027-03-10"] == 1 and v["unmet"]     # alternative: only 1 left
    for night in H["nights"]:
        inv = e.s["inventory"][night]["twin"]
        assert inv["held"] + inv["booked"] <= inv["capacity"]


def test_duplicate_email_and_threading(eng):
    e = eng
    first = S["inbox"][0]
    r = e.ingest_email(first["from"], first["subject"], first["body"])
    assert r["duplicate"]
    n = deal_by(e, "Northstar")
    r = e.ingest_email(first["from"], "Re: " + first["subject"], "Classroom please. Payer Northstar Analytics Demo Ltd.")
    assert r["threaded"] and r["deal_id"] == n["id"]
    assert len(e.s["deals"]) == 2


def test_out_of_scope_goes_to_person(eng):
    e = eng
    e.ingest_email("x <a@b.example>", "RFP", "Please quote 4 king rooms arriving 20 April and departing 22 April 2027 "
                   "with a group dinner. Reply by 15 January.", wake=False)
    RulesSeller(e).run()
    d = deal_by(e, "b.example")
    assert d["status"] == "handoff" and "outside" in d["handoff"]["reason"]


# ------------------------------------------------------------------------------ payments & recovery
def test_refund_limit_and_unresolved_refund(eng):
    e = eng
    e.s["sim"]["delay_refund_notifications"] = True
    e.provider.refund_settle_seconds = 3600                          # refund stays RECEIVED
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    with pytest.raises(Refused):
        e.decide_approval(appr["id"], "approve", "organizer")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    r = e.refund_excess(n["id"])
    assert r["status"] == "RECEIVED"
    with pytest.raises(Refused, match="unresolved"):
        e.refund_excess(n["id"])                                     # no second refund while pending
    fin = e.finance(n)
    assert fin["refund_pending"] == 52800 and fin["received"] == 384000 and fin["collection_blocked"]
    e.provider.refund_settle_seconds = 0
    e.check_refund(n["id"])
    e.check_refund(n["id"])
    assert len([x for x in e.s["ledger"] if x["type"] == "refund_completed"]) == 1
    assert len(e._refund_ops(n)) == 1


def test_refund_rejected_keeps_entitlement(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(appr["id"], "reject", "hotel_manager", "check with finance")
    s.run()
    assert n["finance"]["refund_owed"] == 52800
    assert n["booking"]["rooms"]["2027-03-10"] == 10                 # reduction stays
    assert n["handoff"] and not e._refund_ops(n)


def test_refund_failure_stays_visible(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    e.provider.knobs["fail_next_refund"] = True
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    assert n["finance"]["refund_owed"] == 52800 and n["status"] == "refund_failed" and n["handoff"]
    assert len(e._refund_ops(n)) == 1                                # no automatic retry
    assert e.s["inventory"]["2027-03-10"]["twin"]["booked"] == 10    # released rooms not reinstated


def test_reduction_after_installment_and_beyond_allowance(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-02-10T09:00:00+00:00")
    r = e.prepare_reduction(n["id"], rooms_per_night=2)
    assert r["status"] == "sent" and r["evaluation"]["refund_mode"] == "manager reconciliation"
    r = e.prepare_reduction(n["id"], rooms_per_night=3)              # 9 room-nights > 7
    assert r["status"] == "pending_approval"
    assert n["booking"]["rooms"]["2027-03-10"] == 12                 # existing agreement still in effect


def test_timeout_on_refund_creation_recovers_without_duplicate(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    e.provider.knobs["timeout_next_create"] = True
    e.refund_excess(n["id"])
    op = e._refund_ops(n)[0]
    assert op["provider_id"]                                        # found by request_id, not resubmitted
    assert len(e.provider.db["refunds"]) == 1


def test_hold_expiry_during_checkout(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    n = deal_by(e, "Northstar")
    appr = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    r = buyer_turn(e, "Northstar")
    e.decide_approval(r["approval_id"], "approve", "organizer")
    s.run()
    assert n["status"] == "awaiting_payment"
    e.advance_clock(hours=30)                                       # unpaid: intent cancelled, hold released
    assert n["status"] == "expired" and e._deposit_op(n)["status"] == "cancelled"
    assert e.s["inventory"]["2027-03-10"]["twin"]["held"] == 0
    with pytest.raises(Refused):
        e.open_checkout(n["id"])


def test_payment_then_commit_failure(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    n = deal_by(e, "Northstar")
    appr = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    r = buyer_turn(e, "Northstar")
    e.decide_approval(r["approval_id"], "approve", "organizer")
    s.run()
    e.s["sim"]["fail_next_commit"] = True
    pay(e, n)
    assert n["status"] == "payment_received_booking_exception"
    assert [x["amount"] for x in e.s["ledger"]] == [384000]
    e.check_payment(n["id"])                                        # no second receipt
    assert len(e.s["ledger"]) == 1


def test_old_event_after_reset(eng):
    e = eng
    s, n = run_to_confirmed(e)
    pi = e._deposit_op(n)["payment_intent_id"]
    old_run = e.s["run_id"]
    e.reset()
    r = e.handle_event({"id": "evt_old", "name": "payment_intent.succeeded",
                        "data": {"object": {"id": pi, "metadata": {"run_id": old_run}}}}, verified=True)
    assert "earlier run" in r["result"]
    assert not e.s["ledger"] and all(d["status"] == "new" for d in e.s["deals"].values())


def test_out_of_order_refund_status_does_not_regress(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    op = e._refund_ops(n)[0]
    assert op["provider_status"] in ("ACCEPTED", "SETTLED")
    e._apply_refund_status(op, {"id": op["provider_id"], "status": "RECEIVED", "amount": op["amount"]})
    assert op["provider_status"] in ("ACCEPTED", "SETTLED") and op["status"] == "completed"


def test_persistence_across_restart(eng, tmp_path):
    e = eng
    RulesSeller(e).run()
    e2 = Engine(tmp_path / "var", e.provider)
    assert e2.s["run_id"] == e.s["run_id"] and len(e2.s["deals"]) == 2


# ------------------------------------------------------------------------------ privacy between the two sides
from agentbank.groupsales.calc import gbp  # noqa: E402
from agentbank.groupsales.policy import buyer_check  # noqa: E402
from agentbank.groupsales.seller import ClaudeSeller  # noqa: E402


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def _seller_surface(e, d):
    """Everything the seller's tools can read about the deals (the Claude seller's read tools)."""
    tools = ClaudeSeller(e, client=object())
    return json.dumps([tools._do("list_queue", {}), tools._do("read_deal", {"deal_id": d["id"]}),
                       [m["text"] for m in d["thread"]]], default=str)


def _to_organizer(e):
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    n = deal_by(e, "Northstar")
    appr = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    r = buyer_turn(e, "Northstar")
    assert r["accepted"] is False
    return s, n, r


def test_seller_never_sees_buyer_mandate(eng):
    e = eng
    s, n, r = _to_organizer(e)
    mandate = S["buyers"]["northstar.example"]
    v = n["versions"][-1]
    reasons = buyer_check(v["terms"], v["pricing"], mandate)
    assert reasons                                                  # the organizer approval is really needed
    organizer = e.s["approvals"][r["approval_id"]]
    forbidden = reasons + [organizer["summary"], gbp(mandate["deposit_limit"]), f"{mandate['deposit_limit_pct']}% limit",
                           "limit", "authority", mandate["organizer"], mandate["flexibility"]]
    assert gbp(mandate["deposit_limit"]) not in n["thread"][0]["text"]   # not something the RFP disclosed
    surface = _seller_surface(e, n)
    for f in forbidden:
        assert f not in surface, f
    for k in ("deposit_limit", "deposit_limit_pct", "budget_mandate", "objectives", "known_answers"):
        assert k not in set(_keys(json.loads(surface)))
    view = e.deal_view(n["id"], for_="seller")
    org = next(a for a in view["approvals"] if a["approver"] == "organizer")
    assert org["status"] == "pending" and "reasons" not in org and "pending buyer-side authorization" in org["summary"]
    assert view["status"] == "acceptance_pending_organizer"
    assert "organizer" in view["waiting_on"]                          # the seller may know it is pending
    # Still private after the organizer decides, and the seller can carry on to checkout.
    e.decide_approval(r["approval_id"], "approve", "organizer", note="fine, within the offsite budget")
    s.run()
    surface = _seller_surface(e, n)
    for f in forbidden + ["within the offsite budget"]:
        assert f not in surface, f
    assert n["status"] == "awaiting_payment"


def test_buyer_never_sees_hotel_internals(eng):
    e = eng
    s = RulesSeller(e)
    s.run()
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()                                                           # v2 waits for the hotel manager
    n = deal_by(e, "Northstar")
    manager = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    rates, costs = RMS["rates"]["twin"], RMS["delivery_costs"]
    hotel_only = {"margin_all_rooms", "margin_full_release", "hotel_check", "records", "records_consulted", "hold",
                  "approvals", "flags", "costs", "contribution", "margin_pct", "next_step", "key_dates"}

    def check(view, extra=()):
        raw = json.dumps(view, default=str)
        assert not hotel_only & set(_keys(view)), hotel_only & set(_keys(view))
        for f in [gbp(rates["autonomous_floor"]), gbp(rates["absolute_min"]), gbp(costs["twin"]),
                  "margin", "Seller's case", manager["summary"], *extra]:
            assert f not in raw, f

    view = e.deal_view(n["id"], for_="buyer")
    check(view)
    assert [v["n"] for v in view["versions"]] == [1]                  # v2 is not sent yet
    assert view["waiting_on"] == "hotel"
    e.decide_approval(manager["id"], "approve", "hotel_manager")
    s.run()
    view = e.deal_view(n["id"], for_="buyer")
    check(view)
    assert view["versions"][-1]["n"] == 2 and view["versions"][-1]["pricing"]["deposit"] == 384000
    r = buyer_turn(e, "Northstar")
    e.decide_approval(r["approval_id"], "approve", "organizer")
    s.run()
    pay(e, n)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    s.run()
    buyer_turn(e, "Northstar")
    s.run()                                                           # refund over £500 waits for the manager
    refund = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(refund["id"], "reject", "hotel_manager", "check with finance")
    s.run()
    view = e.deal_view(n["id"], for_="buyer")
    check(view, extra=[refund["summary"], "check with finance", "escalated"])
    assert view["finance"]["refund_owed"] == 52800                    # the buyer still sees what it is owed
    assert view["versions"][-1]["reduction"]["refund_due"] == 52800
