"""The layout question offers only what the hotel's meeting rooms are set up for."""
import pytest

from agentbank.groupsales.engine import Engine
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def deal_by(e, domain):
    return next(d for d in e.s["deals"].values() if d["domain"] == domain)


def test_layout_question_offers_only_layouts_the_hotel_has(eng):
    """The Thames Room is set up for classroom only. The question used to offer "classroom, boardroom, theatre...",
    and a buyer who chose boardroom was handed to staff with no explanation."""
    e = eng
    e.ingest_email("Bo <bo@board.example>", "Board", "Please quote 2 king rooms for 2 employees arriving 10 March and "
                   "departing 13 March 2027 with breakfast. We need a meeting room for 6 people on 11 March in boardroom "
                   "layout. Billed to Board Demo Ltd.", wake=False)
    RulesSeller(e).run()
    d = deal_by(e, "board.example")
    assert d["status"] == "needs_info" and not d["handoff"]
    assert d["thread"][-1]["questions"] == ["meeting room layout (classroom): the Thames Room can't be set up as boardroom"]
    e.buyer_action(d["id"], d["buyer_token"], "reply", "Classroom layout.", {"answers": {"meeting_layout": "classroom"}})
    RulesSeller(e).run()
    d = deal_by(e, "board.example")
    assert d["status"] == "offer_sent" and d["versions"][-1]["terms"]["meeting"]["layout"] == "classroom"
