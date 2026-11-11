"""RFP generator: realistic inquiries plus a private buyer brief; the agents and the engine do the rest.

Each preset is run end to end with the rules seller and the automated buyer. A small driver stands in for
the people: it approves what is pending, pays deposits on the simulated provider and jumps the clock.
Run: PYTHONPATH=src python -m pytest tests/test_generator.py -q
"""
from __future__ import annotations

import json

import pytest

from agentbank.groupsales import generator
from agentbank.groupsales.buyer import act, suggest
from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.extract import extract
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def drive(e: Engine, deal_id: str, max_steps: int = 40, review_pricing: bool = False) -> list[str]:
    """Run a deal to rest. Returns the path: buyer moves, approvals, payment and clock jumps."""
    d, s, path, jumped = e.deal(deal_id), RulesSeller(e), [], False
    for _ in range(max_steps):
        s.run()
        if review_pricing and (d.get('pricing_review') or {}).get('status') == 'pending':
            rate=e.rms['rates'][d['requirements']['room_type']]['autonomous_floor']
            payload={'review_id':d['pricing_review']['id'],'rate':rate}
            preview=e.pricing_review_action(deal_id,'preview',payload)
            assert not preview['problems']
            e.pricing_review_action(deal_id,'approve',{**payload,'preview_hash':preview['hash'],'note':'Revenue counteroffers at the normal seller floor.'})
            path.append('revenue counteroffer')
            continue
        pending = [a for a in e.s["approvals"].values() if a["deal_id"] == deal_id and a["status"] == "pending"]
        if pending:
            for a in pending:
                e.decide_approval(a["id"], "approve", a["approver"])
                path.append(f"approve {a['approver']} {a['subject']}")
            continue
        sug = suggest(e, deal_id)
        if sug:
            act(e, deal_id, sug)
            path.append(f"buyer {sug['action']}")
            continue
        op = e._deposit_op(d)
        if d["status"] == "awaiting_payment" and op:
            e.provider.pay(op["payment_intent_id"], "4035 5010 0000 0008")
            e.check_payment(deal_id, source="return page")
            path.append("paid")
            continue
        ev = (e.buyer_brief(d) or {}).get("change_event")
        if d["status"] == "confirmed" and ev and not jumped:
            e.advance_clock(hours=ev["after_booked_hours"] + 1)
            jumped = True
            path.append("jump")
            continue
        break
    return path


def new(e, preset=None, seed=1, **traits):
    r = generator.send(e, generator.generate(e, traits=traits, preset=preset, seed=seed))
    return e.deal(r["deal_id"]), r


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_straight_booking_is_accepted_and_paid_without_approvals(eng, seed):
    d, _ = new(eng, "straight_booking", seed)
    path = drive(eng, d["id"])
    assert path == ["buyer accept", "paid"]
    assert d["status"] == "confirmed" and eng.finance(d)["net_received"] > 0


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_dates_short_haggle_routes_to_revenue_then_holds_the_floor(eng, seed):
    d, _ = new(eng, "dates_short_haggle", seed)
    path = drive(eng, d["id"], review_pricing=True)
    assert path == ["buyer reply", "buyer counter", "revenue counteroffer", "buyer accept", "paid"]
    assert any(m.get("date_options") for m in d["thread"])               # the seller offered other dates
    counter = next(m for m in d["thread"] if m.get("action") == "counter")
    floor = eng.rms["rates"][d["versions"][-1]["terms"]["room_type"]]["autonomous_floor"]
    assert counter["payload"]["rate"] < floor                            # asked below the seller's floor
    assert d["versions"][-1]["terms"]["rate"] == floor                   # the rule held, in code
    assert d["status"] == "confirmed"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_full_lifecycle_ends_in_a_partial_refund_with_the_payout_reversed(eng, seed):
    d, _ = new(eng, "full_lifecycle", seed)
    path = drive(eng, d["id"])
    assert path[:4] == ["buyer counter", "approve hotel_manager offer", "buyer accept", "paid"]
    assert path[4:7] == ["jump", "buyer change_request", "buyer accept"]
    fin = eng.finance(d)
    assert d["status"] == "confirmed" and d["finance"]["refund_owed"] == 0
    assert fin["refunded"] > 0 and fin["net_received"] == fin["received"] - fin["refunded"]
    po = eng.payout_view(d)
    assert po["hotel_net"] + po["platform_fee"] == fin["net_received"]   # the hotel's share follows the refund
    assert d["versions"][-1]["terms"]["meeting"]["layout"] == "classroom"


def test_payer_abroad_records_the_payer_country_and_refunds(eng):
    d, r = new(eng, "payer_abroad", 4)
    assert d["payer_origin"]["country_code"] == "DE" and d["payer_origin"]["local_currency"] == "EUR"
    assert "GmbH" in r["rfp"]["body"] and "Germany" in r["rfp"]["body"]
    drive(eng, d["id"])
    assert d["status"] == "confirmed" and eng.finance(d)["refunded"] > 0


def test_missing_details_make_the_seller_ask_and_the_buyer_answer_from_its_brief(eng):
    d, r = new(eng, "missing_details", 5)
    req = extract(r["rfp"]["subject"], r["rfp"]["body"])
    assert req["payer"] is None and req["budget_includes_taxes"] is None
    path = drive(eng, d["id"])
    assert path[0] == "buyer reply" and d["status"] == "confirmed"
    assert any(m.get("questions") for m in d["thread"] if m["from"] == "seller")
    assert d["requirements"]["payer"] == eng.buyer_brief(d)["known_answers"]["payer"]


def test_the_rfp_reads_back_into_the_brief_it_came_with(eng):
    for preset in generator.PRESETS:
        g = generator.generate(eng, preset=preset, seed=7)
        req = extract(g["subject"], g["body"])
        b = g["brief"]
        assert req["rooms"] and req["room_type"] and req["arrival"] and req["departure"], preset
        assert req["budget_total"] == g["budget"], preset
        assert not {"budget_total", "deposit_limit", "deposit_limit_pct"} & set(b), preset   # no buyer-side limits
        if "payer" not in g["traits"]["missing"]:
            assert req["payer"] == b["known_answers"]["payer"], preset


def test_same_seed_same_inquiry_and_other_seeds_differ(tmp_path):
    def gen(seed, sub):
        e = Engine(tmp_path / sub / "var", SimulatedProvider(tmp_path / sub / "sim.json"))
        return generator.generate(e, preset="full_lifecycle", seed=seed)
    a, b, c = gen(11, "a"), gen(11, "b"), gen(12, "c")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["body"] != c["body"] and a["domain"] != c["domain"]


def test_every_preset_finds_dates_against_the_seeded_calendar(eng):
    """The room count used to come from total capacity (20), so with the calendar's existing bookings some
    seeds asked for more rooms than any window had free and generate() refused (seed 11, full_lifecycle)."""
    for preset in generator.PRESETS:
        for seed in range(1, 41):
            g = generator.generate(eng, preset=preset, seed=seed)
            assert g["body"] and g["brief"], (preset, seed)


def test_a_replayed_seed_in_the_same_run_is_refused_not_merged(eng):
    new(eng, "straight_booking", 9)
    with pytest.raises(Refused, match="already has an inquiry"):
        new(eng, "straight_booking", 9)


def test_the_seller_never_sees_the_generated_brief(eng):
    d, _ = new(eng, "dates_short_haggle", 3)
    drive(eng, d["id"])
    b = eng.buyer_brief(d)
    seller_view = json.dumps(eng.deal_view(d["id"], for_="seller"), default=str)
    snap = json.dumps(eng.snapshot(), default=str)
    # The stated budget is in the RFP itself; the goals, notes and prepared answers are private.
    for secret in ("objectives", "known_answers", "target_rate", "change_event"):
        assert secret not in seller_view and secret not in snap
    assert b["flexibility"] not in seller_view


def test_generated_deals_are_marked_for_the_presenter_only(eng):
    d, _ = new(eng, "payer_abroad", 21)
    assert d["generated"] == {"preset": "payer_abroad", "seed": 21}
    snap = next(x for x in eng.snapshot()["deals"] if x["id"] == d["id"])
    assert snap["generated"] == {"preset": "payer_abroad", "seed": 21}
    assert "generated" not in eng.deal_view(d["id"], for_="seller")
    assert "generated" not in eng.deal_view(d["id"], for_="buyer")
    custom, _ = new(eng, None, 22, availability="fits")
    assert custom["generated"] == {"preset": None, "seed": 22}


def test_every_preset_has_a_title_and_description():
    assert set(generator.PRESET_INFO) == set(generator.PRESETS)
    assert all(i["title"] and i["shows"] for i in generator.PRESET_INFO.values())


def test_traits_are_checked(eng):
    with pytest.raises(Refused, match="unknown preset"):
        generator.generate(eng, preset="nope")
    with pytest.raises(Refused, match="unknown traits"):
        generator.generate(eng, traits={"colour": "blue"})
    with pytest.raises(Refused, match="availability"):
        generator.generate(eng, traits={"availability": "sometimes"})
    with pytest.raises(Refused, match="seed"):
        generator.generate(eng, seed="abc")
    with pytest.raises(Refused, match="country"):
        generator.generate(eng, traits={"payer_country": "XX"})


def test_short_rooms_asks_for_more_than_is_free_on_some_night(eng):
    g = generator.generate(eng, traits={"availability": "short_rooms", "room_type": "king"}, seed=2)
    req = extract(g["subject"], g["body"])
    nights = [n for n in eng.hotel["capacity"] if req["arrival"] <= n < req["departure"]]
    free = [eng.inventory_at(n)["king"]["capacity"] - eng.inventory_at(n)["king"]["held"] for n in nights]
    assert req["rooms"] > min(free)


def test_too_soon_dates_are_inside_the_notice_period(eng):
    g = generator.generate(eng, traits={"availability": "too_soon"}, seed=3)
    req = extract(g["subject"], g["body"])
    assert eng.lead_days(req["arrival"]) < eng.policy["min_days_before_arrival"]
