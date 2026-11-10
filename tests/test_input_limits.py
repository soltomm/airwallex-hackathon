"""Oversized or malformed input is refused quickly, with a reason."""
import pytest

from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.extract import extract
from agentbank.groupsales.provider import SimulatedProvider


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def deal_by(e, company):
    return next(d for d in e.s["deals"].values() if d["company"].startswith(company) or d["domain"] == company)



def test_a_long_run_of_letters_does_not_freeze_the_extractor():
    """The payer pattern was quadratic on a long unbroken run of letters (a pasted base64 blob): 20,000 letters took
    about 8 seconds and 100,000 about 3 minutes, holding up the whole server."""
    import time
    t = time.time()
    r = extract("x", "Please quote 5 king rooms. " + "A" * 200_000 + " Billed to Groupie LLC.")
    assert time.time() - t < 2 and r["payer"] == "Groupie LLC"
    assert extract("x", "Billed to Northstar Analytics Demo Ltd.")["payer"] == "Northstar Analytics Demo Ltd"


def test_oversized_or_malformed_emails_and_messages_are_refused_with_a_reason(eng):
    e = eng
    with pytest.raises(Refused, match="too long"):
        e.ingest_email("Lo <lo@long.example>", "Trip", "Please quote 5 king rooms. " + "x " * 12_500, wake=False)
    with pytest.raises(Refused, match="must be text"):
        e.ingest_email(["x"], "Trip", "hello", wake=False)
    n = deal_by(e, "Northstar")
    with pytest.raises(Refused, match="too long"):
        e.buyer_action(n["id"], n["buyer_token"], "message", "y" * 10_001)
    assert e.ingest_email("Ok <ok@ok.example>", "Trip", "Please quote 2 king rooms.", wake=False)["duplicate"] is False
