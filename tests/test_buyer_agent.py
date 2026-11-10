"""The Claude buyer agent, with a fake client that records what it was told.

Run: PYTHONPATH=src python -m pytest tests/test_buyer_agent.py -q
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentbank.groupsales import generator
from agentbank.groupsales.buyer import BuyerAgent
from agentbank.groupsales.engine import Engine
from agentbank.groupsales.provider import SimulatedProvider

from test_generator import drive


class FakeClient:
    """Answers every call with no tool use (the agent waits) and keeps each deal view it was sent."""

    def __init__(self):
        self.views = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        text = kw["messages"][0]["content"]
        self.views.append(json.loads(text.split("\n", 1)[1]))
        return SimpleNamespace(content=[])


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def test_the_organizer_news_is_not_repeated_after_the_change_was_asked_for(eng):
    r = generator.send(eng, generator.generate(eng, preset="full_lifecycle", seed=1))
    d = eng.deal(r["deal_id"])
    path = drive(eng, d["id"])
    assert "buyer change_request" in path and d["status"] == "confirmed"
    # The hotel asks something after the reduction: the buyer takes a turn, but must not hear the
    # organizer's news again (it used to, and asked for the same reduction over and over).
    eng.s["deals"][d["id"]]["thread"].append({"from": "seller", "text": "Anything else we can help with?",
                                             "questions": ["Anything else?"], "at": eng.now()})
    fake = FakeClient()
    BuyerAgent(eng, client=fake).run(d["id"])
    assert len(fake.views) == 1 and "event" not in fake.views[0]


def test_the_organizer_news_is_told_before_the_change_is_asked_for(eng):
    r = generator.send(eng, generator.generate(eng, preset="full_lifecycle", seed=1))
    d = eng.deal(r["deal_id"])
    drive(eng, d["id"])
    # Drop the buyer's change request and what followed: the news is due and not yet acted on.
    i = next(i for i, m in enumerate(d["thread"]) if m.get("action") == "change_request")
    del d["thread"][i:]
    d["thread"].append({"from": "seller", "text": "Anything else?", "questions": ["Anything else?"], "at": eng.now()})
    fake = FakeClient()
    BuyerAgent(eng, client=fake).run(d["id"])
    assert fake.views and fake.views[0]["event"].startswith("News from your organizer")
