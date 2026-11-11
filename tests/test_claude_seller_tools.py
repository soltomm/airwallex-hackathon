"""The Claude seller's tools. On the live demo every Claude seller call failed with "The compiled grammar is too large":
all of its tools were strict, and the API compiles strict schemas into one grammar. The tools are no longer strict;
the seller checks each call against its schema itself before it runs."""
from types import SimpleNamespace

import pytest

from agentbank.groupsales.engine import Engine
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import TOOLS, ClaudeSeller, schema_errors


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def schema(name):
    return next(t["input_schema"] for t in TOOLS if t["name"] == name)


def test_no_seller_tool_is_strict():
    assert not [t["name"] for t in TOOLS if t.get("strict")]


def test_schema_errors_catch_what_strict_mode_used_to():
    s = schema("read_deal")
    assert schema_errors({"deal_id": "GS-1001"}, s) == []
    assert schema_errors({}, s) == ["input.deal_id is required"]
    assert schema_errors({"deal_id": 1001}, s) == ["input.deal_id should be string, not int"]
    assert schema_errors({"deal_id": "GS-1001", "sneaky": True}, s) == ["input.sneaky is not a known field"]
    assert schema_errors({"x": True}, {"type": "object", "properties": {"x": {"type": "integer"}}}) \
        == ["input.x should be integer, not bool"]                       # a bool is not a number in JSON
    assert schema_errors(None, {"anyOf": [{"type": "string", "enum": ["twin", "king"]}, {"type": "null"}]}) == []
    assert schema_errors("suite", {"anyOf": [{"type": "string", "enum": ["twin", "king"]}, {"type": "null"}]})


class StubClient:
    """Plays the model: first a malformed read_deal call, then a correct one, then it stops."""
    def __init__(self):
        self.requests, self.turn = [], 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})     # the seller keeps appending to its list
        self.turn += 1
        if self.turn == 1:
            content = [SimpleNamespace(type="tool_use", id="t1", name="read_deal", input={"deal": "GS-1001"})]
        elif self.turn == 2:
            content = [SimpleNamespace(type="tool_use", id="t2", name="read_deal", input={"deal_id": "GS-1001"})]
        else:
            content = [SimpleNamespace(type="text", text="Done.")]
        return SimpleNamespace(stop_reason="tool_use" if self.turn < 3 else "end_turn", content=content)


def test_a_malformed_tool_call_goes_back_to_the_model_and_never_reaches_the_engine(eng):
    client = StubClient()
    ClaudeSeller(eng, client=client).run("test")
    assert all(not t.get("strict") for t in client.requests[0]["tools"])
    first_results = client.requests[1]["messages"][-1]["content"]
    assert first_results[0]["is_error"] and "input.deal_id is required" in first_results[0]["content"]
    second_results = client.requests[2]["messages"][-1]["content"]
    assert not second_results[0].get("is_error") and "GS-1001" in second_results[0]["content"]
