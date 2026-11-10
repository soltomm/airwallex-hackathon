"""The rules seller reads buyer text with the model (understand.read). These tests stub the model's reading, so they
check what the seller does with it; the engine's rules are covered elsewhere."""
import json
from types import SimpleNamespace

import pytest

from agentbank.groupsales import understand
from agentbank.groupsales.engine import Engine
from agentbank.groupsales.provider import SimulatedProvider
from agentbank.groupsales.seller import RulesSeller

TESTGUEST = ("I need 10 rooms for 10 employees traveling from 12th Nov to 15th Novemeber, breakfast included. "
             "We also need a meeting room on the 13th Nov")


def model_json(**kw):
    """A reply shaped exactly like understand.SCHEMA, with nulls for everything not given."""
    base = {k: None for k in understand.SCHEMA["required"]}
    base.update(no_meeting=False, unsupported=[])
    base.update(kw)
    return base


class StubReader:
    """Stands in for understand.read: returns canned model readings in order for this test's messages (those that
    contain one of `markers`) and records what it was asked. The scenario's seeded inquiries get the pattern reader."""
    def __init__(self, *readings, markers=("TestGuest", "Novemeber", "whatever room", "Microshift", "first one")):
        self.readings, self.calls, self.markers = list(readings), [], markers

    def __call__(self, text, today, subject="", context=None):
        if not any(m in text or m in subject for m in self.markers):
            return None, None
        self.calls.append({"text": text, "context": context})
        r = self.readings.pop(0)
        return (understand.to_requirements(r), None) if r is not None else (None, "stub: model unavailable")


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def deal(e):
    return next(d for d in e.s["deals"].values() if d["domain"] == "testguest.example")


def test_natural_language_inquiry_is_understood(eng):
    reader = StubReader(model_json(rooms=10, guests=10, arrival="2027-11-12", departure="2027-11-15", breakfast=True,
                                   meeting={"attendees": None, "date": "2027-11-13", "layout": None, "hours": None}))
    eng.ingest_email("TestGuest <guest@testguest.example>", "Group inquiry", TESTGUEST, wake=False)
    RulesSeller(eng, reader=reader).run()
    d = deal(eng)
    req = d["requirements"]
    assert (req["rooms"], req["arrival"], req["departure"], req["breakfast"]) == (10, "2027-11-12", "2027-11-15", True)
    assert req["meeting"]["date"] == "2027-11-13" and req["read_by"] == "model"
    asked = d["thread"][-1].get("questions") or []
    # Only what the email really leaves open; never the dates or the meeting date it gave.
    assert "arrival and departure dates" not in asked and "meeting date" not in asked
    assert "room type (king or twin)" in asked and any("legal name" in q for q in asked)
    assert TESTGUEST in reader.calls[0]["text"]


def test_free_text_reply_is_understood_in_context(eng):
    reader = StubReader(
        model_json(rooms=10, guests=10, arrival="2027-11-12", departure="2027-11-15", breakfast=True,
                   meeting={"attendees": 10, "date": "2027-11-13", "layout": None, "hours": None}),
        model_json(room_type="any", payer="TestGuest Holdings Ltd",
                   meeting={"attendees": None, "date": None, "layout": "boardroom", "hours": None}))
    eng.ingest_email("TestGuest <guest@testguest.example>", "Group inquiry", TESTGUEST, wake=False)
    seller = RulesSeller(eng, reader=reader)
    seller.run()
    d = deal(eng)
    eng.buyer_action(d["id"], d["buyer_token"], "message",
                     "whatever room is fine, boardroom please, invoice TestGuest Holdings Ltd")
    seller.run()
    d = deal(eng)
    assert d["requirements"]["room_type"] == "king" and d["requirements"]["payer"] == "TestGuest Holdings Ltd"
    assert d["requirements"]["meeting"]["layout"] == "boardroom"
    assert d["status"] in ("offer_sent", "dates_proposed", "pending_approval") and not d["missing"]
    ctx = reader.calls[1]["context"]                  # the reply is read knowing what was asked and already known
    assert ctx["hotel_asked"] and ctx["already_known"]["arrival"] == "2027-11-12"


def test_buyer_picks_an_offered_date_option_in_words(eng):
    e = eng
    rfp = ("Please quote 4 king rooms for 4 employees arriving 3 January and departing 6 January 2027 with breakfast. "
           "No meeting room is needed. Billed to Microshift Demo Ltd.")
    reader = StubReader(model_json(rooms=4, room_type="king", guests=4, arrival="2027-01-03",
                                   departure="2027-01-06", breakfast=True, no_meeting=True,
                                   payer="Microshift Demo Ltd"),
                        model_json(chosen_date_option=1))
    e.ingest_email("Jo <jo@microshift.example>", "Group RFP", rfp, wake=False)
    seller = RulesSeller(e, reader=reader)
    seller.run()
    d = next(x for x in e.s["deals"].values() if x["domain"] == "microshift.example")
    assert d["status"] == "dates_proposed"
    first = d["thread"][-1]["date_options"][0]
    e.buyer_action(d["id"], d["buyer_token"], "message", "the first one works for us")
    seller.run()
    d = e.deal(d["id"])
    assert d["requirements"]["arrival"] == first["arrival"] and d["status"] == "offer_sent"
    assert "1. " in reader.calls[1]["context"]["date_options_offered"][0]


def test_when_the_model_is_unavailable_the_patterns_read_the_email(eng):
    e = eng
    rfp = ("Please quote 4 king rooms for 4 employees arriving 10 March and departing 13 March 2027 with breakfast. "
           "Billed to Microshift Demo Ltd.")
    e.ingest_email("Jo <jo@microshift.example>", "Group RFP", rfp, wake=False)
    RulesSeller(e, reader=StubReader(None)).run()
    d = next(x for x in e.s["deals"].values() if x["domain"] == "microshift.example")
    assert d["requirements"]["rooms"] == 4 and d["requirements"]["arrival"] == "2027-03-10"
    assert "read_by" not in d["requirements"]
    assert any("patterns instead" in a["text"] for a in e.s["activity"] if a.get("deal_id") == d["id"])


def test_model_fields_are_validated_before_they_reach_the_engine():
    got = understand.to_requirements(model_json(
        rooms=-3, guests=True, arrival="2027-02-30", departure="2027-03-02", budget_amount="20,000.50",
        budget_currency="usd", room_type="single", unsupported=["spa", "not-a-service"],
        meeting={"attendees": 0, "date": "nope", "layout": "ballroom", "hours": "all day"}))
    assert "rooms" not in got and "guests" not in got and "arrival" not in got
    assert (got["budget_total"], got["budget_currency"]) == (2000050, "USD")
    assert got["unsupported"] == ["single rooms", "spa"] and got["meeting"] == {}


def test_read_calls_the_api_with_a_json_schema_and_parses_the_reply():
    sent = {}

    class Msgs:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(
                type="text", text=json.dumps(model_json(rooms=10, arrival="2027-11-12", departure="2027-11-15")))])

    client = SimpleNamespace(beta=SimpleNamespace(messages=Msgs()))
    got, err = understand.read(TESTGUEST, today="2027-01-11T09:00:00+00:00", client=client)
    assert err is None and got == {"rooms": 10, "arrival": "2027-11-12", "departure": "2027-11-15"}
    assert sent["output_config"]["format"]["schema"] is understand.SCHEMA
    assert "2027-01-11" in sent["messages"][0]["content"] and TESTGUEST in sent["messages"][0]["content"]


def test_read_falls_back_on_refusal_and_errors():
    class Refuses:
        def create(self, **kw):
            return SimpleNamespace(stop_reason="refusal", content=[])

    class Fails:
        def create(self, **kw):
            raise RuntimeError("boom")

    for msgs in (Refuses(), Fails()):
        got, err = understand.read("hi", today="2027-01-11", client=SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
        assert got is None and err


def test_reader_client_sends_the_workspace_for_user_level_keys(monkeypatch):
    """User-level keys (sk-ant-usr-...) are rejected without the workspace header; the reader must send it, as the
    Claude seller's client does. Without it every model read failed and the patterns answered instead."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    monkeypatch.setattr(understand, "_client", None)
    client = understand._default_client()
    assert client.default_headers.get("anthropic-workspace-id") == "wrkspc_test"
    monkeypatch.setattr(understand, "_client", None)
