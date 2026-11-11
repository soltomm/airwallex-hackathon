"""The buyer-facing wording of a room reduction agrees in number with what it counts.

Run: PYTHONPATH=src python -m pytest tests/test_reduction_wording.py -q
"""
from __future__ import annotations

import pytest

from agentbank.groupsales.buyer import act
from agentbank.groupsales.engine import Engine
from agentbank.groupsales.provider import SimulatedProvider
from test_group_sales import buyer_turn, run_to_confirmed


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def test_one_room_night_is_singular_and_the_rest_plural(eng):
    e = eng
    s, n = run_to_confirmed(e)
    e.advance_clock(to="2027-01-20T09:00:00+00:00")
    buyer_turn(e, "Northstar")                     # 2 rooms a night fewer: 6 of the 7 free room-nights
    s.run()
    first = n["versions"][-1]["message"]
    assert "We can release 6 room-nights under your attrition allowance (1 room-night remains for later release" in first

    buyer_turn(e, "Northstar")                     # accept v3
    s.run()
    appr = next(a for a in e.s["approvals"].values() if a["subject"] == "refund")
    e.decide_approval(appr["id"], "approve", "hotel_manager")
    s.run()
    act(e, n["id"], {"action": "change_request", "text": "Please release 1 more room a night.",
                     "payload": {"rooms_per_night": 1}})
    s.run()
    second = n["versions"][-1]["message"]
    assert "We can release 3 room-nights back to the hotel. 1 comes off your total" in second
    assert "the other 2 remain payable" in second
    assert "1 come off" not in second and "room-night(s)" not in first + second
