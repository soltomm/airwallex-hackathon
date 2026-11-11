"""Demand forecast and busy-night rates: busy dates with rooms free, wherever they fall in the calendar.

Run: PYTHONPATH=src python -m pytest tests/test_demand_forecast.py -q
"""
from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from agentbank.groupsales import calc, generator
from agentbank.groupsales.engine import Refused
from agentbank.groupsales.policy import hotel_check

from test_group_sales import H_ALL, MARCH, POL, RMS, S, eng, northstar_terms  # noqa: F401
from test_generator import drive


def stay(nights, rooms=8, rate=22000, att=10, dep=30):
    return {"room_type": "twin", "rate": rate, "rooms": {n: rooms for n in nights}, "released": {n: 0 for n in nights},
            "breakfast": {(calc.d(n) + timedelta(days=1)).isoformat(): rooms * 2 for n in nights},
            "meeting": None, "attrition_pct": att, "deposit_pct": dep, "currency": "GBP"}


def busy_window(min_free=6):
    """Three nights forecast busy (rates raised) with at least `min_free` twins free on each."""
    nights = H_ALL["nights"]
    for i in range(len(nights) - 3):
        ns = nights[i:i + 3]
        if any(n in RMS["displacement"]["demand_by_night_pct"] for n in ns):
            continue
        if min(H_ALL["capacity"][n]["twin"] for n in ns) >= min_free and calc.rate_bands(stay(ns), H_ALL, RMS, POL)["uplift"]:
            return ns
    raise AssertionError("no busy window with rooms free")


def test_the_forecast_adds_expected_pickup_to_what_is_taken():
    june = [n for n in H_ALL["nights"] if n.startswith("2027-06")]
    for n in june:
        alloc, free = H_ALL["availability"]["twin"], H_ALL["capacity"][n]["twin"]
        taken = (alloc - free) * 100 // alloc
        assert H_ALL["forecast"][n]["twin"] == min(100, taken + round((100 - taken) * 0.65))
        assert calc.demand_pct(n, "twin", H_ALL, RMS) == H_ALL["forecast"][n]["twin"]
    # Busy with rooms still free: what the old "share taken" demand could never produce.
    assert any(H_ALL["capacity"][n]["twin"] >= 8 and H_ALL["forecast"][n]["twin"] >= 80 for n in june)
    assert [calc.demand_pct(n, "twin", H_ALL, RMS) for n in MARCH] == [80, 80, 64]      # still stated for the demo


def test_busy_dates_raise_the_stays_rates_to_the_lowest_that_keeps_the_margin():
    ns = busy_window()
    b = calc.rate_bands(stay(ns), H_ALL, RMS, POL)
    assert b["busy"] and b["uplift"] > 0 and b["target"] % 100 == 0
    assert (b["autonomous_floor"] - 21000, b["absolute_min"] - 19500) == (b["uplift"], b["uplift"])
    ok = calc.price(stay(ns, rate=b["target"]), H_ALL, RMS)
    below = calc.price(stay(ns, rate=b["target"] - 100), H_ALL, RMS)
    assert min(ok["margin_all_rooms"]["margin_pct"], ok["margin_full_release"]["margin_pct"]) >= 40
    assert min(below["margin_all_rooms"]["margin_pct"], below["margin_full_release"]["margin_pct"]) < 40
    # So any concession breaks the floor: more free release at that rate, or the (raised) floor rate.
    for t in (stay(ns, rate=b["target"], att=20, dep=40), stay(ns, rate=b["autonomous_floor"])):
        assert not hotel_check(t, calc.price(t, H_ALL, RMS), {**S, "hotel": H_ALL})["ok"]
    assert hotel_check(stay(ns, rate=b["target"]), ok, {**S, "hotel": H_ALL})["ok"]


def test_quiet_dates_and_the_demo_stay_keep_the_usual_rates():
    quiet = ["2027-01-18", "2027-01-19", "2027-01-20"]
    assert calc.rate_bands(stay(quiet), H_ALL, RMS, POL)["uplift"] == 0
    b = calc.rate_bands(northstar_terms(10, 30), H_ALL, RMS, POL)
    assert (b["target"], b["autonomous_floor"], b["absolute_min"], b["uplift"]) == (22000, 21000, 19500, 0)


def test_busy_night_pricing_can_be_switched_off():
    rms = copy.deepcopy(RMS)
    rms["busy_night_pricing"] = False
    b = calc.rate_bands(stay(busy_window()), H_ALL, rms, POL)
    assert (b["target"], b["uplift"]) == (22000, 0)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_busy_dates_preset_quotes_above_target_and_holds_its_terms(eng, seed):
    for d0 in list(eng.s["deals"].values()):
        eng.decline(d0["id"], "only the generated inquiry", "x")
    r = generator.send(eng, generator.generate(eng, preset="busy_dates", seed=seed))
    d = eng.deal(r["deal_id"])
    path = drive(eng, d["id"])
    assert d["status"] == "confirmed" and "approve hotel_manager offer" not in path
    v1 = d["versions"][0]
    assert len(d["versions"]) == 1 and d["accepted_version"] == 1
    assert v1["terms"]["rate"] > 22000 and (v1["terms"]["attrition_pct"], v1["terms"]["deposit_pct"]) == (10, 30)
    assert v1["hotel_check"]["bands"]["target"] == v1["terms"]["rate"]
    texts = [m["text"] for m in d["thread"] if m["from"] == "seller"]
    assert any("demand on your dates is high" in t.lower() and "stays open" in t for t in texts)
    notes = [a["text"] for a in eng.s["activity"] if a["deal_id"] == d["id"] and a["kind"] == "reasoning"]
    assert any(t.startswith("Busy dates: forecast demand") for t in notes)
    assert "forecast" not in str(eng.deal_view(d["id"], for_="buyer"))


def test_offer_stands_needs_an_open_offer(eng):
    d = next(iter(eng.s["deals"].values()))
    with pytest.raises(Refused, match="no open offer"):
        eng.offer_stands(d["id"], "Our offer stands.")
