"""The Claude seller's tools carry the whole demo story, with a scripted model and no network.

This does not test Claude's judgement: the moves are scripted. It checks that every tool the Claude seller needs
for the recorded demo still takes the arguments its schema describes and reaches the engine without a refusal, so a
change elsewhere can't quietly break the Claude path (the rest of the suite runs the rules seller). The figures at the
end are the spec figures from GROUP_SALES.md.

Run: PYTHONPATH=src python -m pytest tests/test_claude_seller_story.py -q
"""
from __future__ import annotations

import pytest

from agentbank.groupsales.engine import Engine
from agentbank.groupsales.extract import extract
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import TOOLS, ClaudeSeller, schema_errors
from test_group_sales import buyer_turn, deal_by, pay

REQ_FIELDS = set(next(t for t in TOOLS if t["name"] == "record_requirements")
                 ["input_schema"]["properties"]["requirements"]["properties"])


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ScriptedClaude:
    """Stands in for the Anthropic client. Each run() is one scripted turn of tool calls, then end_turn; every
    tool result the seller sends back is kept so the test can fail on any invalid argument or engine refusal."""

    def __init__(self):
        self.beta = self.messages = self
        self.turn: list[tuple[str, dict]] = []
        self.sent = False
        self.errors: list[str] = []
        self.results: dict[str, str] = {}

    def create(self, **kw):
        last = kw["messages"][-1]
        if isinstance(last.get("content"), list):
            for r in last["content"]:
                if isinstance(r, dict) and r.get("type") == "tool_result":
                    self.results[r["tool_use_id"]] = r["content"]
                    if r.get("is_error"):
                        self.errors.append(f"{r['tool_use_id']}: {r['content']}")
        if self.sent:
            return _Block(stop_reason="end_turn", content=[])
        self.sent = True
        return _Block(stop_reason="tool_use", content=[
            _Block(type="tool_use", id=f"{name}#{i}", name=name, input=args) for i, (name, args) in enumerate(self.turn)])


def _requirements(fields: dict) -> dict:
    """Extracted requirements as the record_requirements tool takes them (pounds, schema fields only)."""
    r = {k: v for k, v in fields.items() if k in REQ_FIELDS and v not in (None, [], "")}
    if "budget_total" in r:
        r["budget_total_gbp"] = r.pop("budget_total") / 100
        r.pop("budget_currency", None)
    if "target_per_room_night" in fields and fields["target_per_room_night"]:
        r["target_per_room_night_gbp"] = fields["target_per_room_night"] / 100
    if r.get("meeting"):
        r["meeting"] = {k: v for k, v in r["meeting"].items() if k in ("attendees", "date", "hours", "layout") and v}
    return r


@pytest.fixture
def story(tmp_path):
    e = Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))
    fake = _ScriptedClaude()
    seller = ClaudeSeller(e, client=fake, model="scripted")

    def turn(*calls):
        fake.turn, fake.sent = list(calls), False
        for name, args in calls:                     # the test's own script must match the schemas too
            tool = next(t for t in TOOLS if t["name"] == name)
            assert not schema_errors(args, tool["input_schema"]), (name, schema_errors(args, tool["input_schema"]))
        seller.run("scripted turn")
        assert not fake.errors, fake.errors
    return e, turn, fake


def _offer(deal, rate, dep, att, room_type, message, explanation="", justification=""):
    return ("propose_offer", {"deal_id": deal, "rate_gbp": rate, "deposit_pct": dep, "attrition_pct": att,
                              "rooms_per_night": None, "room_type": room_type, "message_to_buyer": message,
                              "explanation": explanation, "unmet": [], "justification_for_manager": justification})


def _price(deal, rate, dep, att, room_type):
    return ("price_terms", {"deal_id": deal, "rate_gbp": rate, "deposit_pct": dep, "attrition_pct": att,
                            "rooms_per_night": None, "room_type": room_type})


def test_the_claude_seller_tools_carry_the_demo_story(story):
    e, turn, fake = story
    n, sm = deal_by(e, "Northstar"), deal_by(e, "Summit")
    ns, ss = (extract(d["subject"], d["thread"][0]["text"]) for d in (n, sm))

    # 1. Both RFPs: Northstar is missing details, so questions; Summit gets a quote within authority.
    turn(("list_queue", {}), ("read_deal", {"deal_id": "GS-1001"}),
         ("record_requirements", {"deal_id": "GS-1001", "requirements": _requirements(ns)}),
         ("check_systems", {"deal_id": "GS-1001"}),
         ("record_requirements", {"deal_id": "GS-1002", "requirements": _requirements(ss)}),
         ("check_systems", {"deal_id": "GS-1002"}),
         _price("GS-1002", 190, 30, 10, "king"),
         _offer("GS-1002", 190, 30, 10, "king", "Our offer for your sales visit."))
    assert n["missing"] and sm["versions"][0]["pricing"]["total"] == 492000 and sm["versions"][0]["pricing"]["deposit"] == 147600
    turn(("ask_buyer", {"deal_id": "GS-1001", "questions": list(n["missing"]), "message": "A few questions first."}))
    assert n["status"] == "needs_info"

    # 2. Maya answers; v1 on standard terms.
    buyer_turn(e, "Northstar")
    answers = n["thread"][-1]["payload"]["answers"]
    turn(("read_deal", {"deal_id": "GS-1001"}),
         ("record_requirements", {"deal_id": "GS-1001", "requirements": {
             "payer": answers["payer"], "budget_includes_taxes": answers["budget_includes_taxes"],
             "meeting": {**_requirements(ns)["meeting"], "layout": answers["meeting_layout"]}}}),
         _price("GS-1001", 220, 30, 10, "twin"), _offer("GS-1001", 220, 30, 10, "twin", "Our offer, itemized."))
    assert n["versions"][-1]["status"] == "sent" and n["versions"][-1]["pricing"]["total"] == 960000

    # 3. The counter: price the ask, then the trade; 20% attrition needs the manager.
    buyer_turn(e, "Northstar")
    turn(_price("GS-1001", 220, 25, 20, "twin"), _price("GS-1001", 220, 40, 20, "twin"),
         _offer("GS-1001", 220, 40, 20, "twin", "20% attrition for a 40% deposit.", "trade: flexibility for cash",
                "Attendance uncertain; rate held; 40% deposit covers it."))
    v2 = n["versions"][-1]
    assert v2["status"] == "pending_approval" and v2["pricing"]["deposit"] == 384000
    appr = next(a for a in e.s["approvals"].values() if a["deal_id"] == n["id"] and a["status"] == "pending")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    turn(("send_offer", {"deal_id": "GS-1001", "version": 2}))

    # 4-5. Accept, checkout, pay, verify.
    buyer_turn(e, "Northstar")
    turn(("open_checkout", {"deal_id": "GS-1001"}))
    pay(e, n)
    turn(("check_payment", {"deal_id": "GS-1001"}))
    assert n["status"] == "confirmed" and e.payout_view(n)["hotel_paid"] == 345600

    # 6-7. Fewer rooms: priced by the contract, refund above £500 needs the manager, then executed and checked.
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")
    turn(("prepare_reduction", {"deal_id": "GS-1001", "rooms_per_night": 2, "message_to_buyer": ""}))
    buyer_turn(e, "Northstar")
    assert n["finance"]["refund_owed"] == 52800
    turn(("refund_excess", {"deal_id": "GS-1001", "justification": "Within the allowance, before the installment."}))
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund" and a["status"] == "pending")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    turn(("refund_excess", {"deal_id": "GS-1001", "justification": "Approved by the hotel manager."}),
         ("check_refund", {"deal_id": "GS-1001"}),
         ("log_reasoning", {"deal_id": "GS-1001", "note": "Refund checked, not created twice."}))

    # 8. The spec figures, reached through the Claude seller's tools.
    fin, po = e.finance(n), e.payout_view(n)
    assert (fin["net_received"], fin["still_due"]) == (331200, 496800)
    assert po["hotel_net"] + po["platform_fee"] == fin["net_received"] == 331200
    run = e.s["agent_runs"][-1]
    assert run["agent"] == "claude" and all(s["ok"] for s in run["steps"])
