"""A repeated email is reported, with the inquiry it already is."""
import pytest

from agentbank.groupsales.engine import Engine, Refused
from agentbank.groupsales.extract import extract
from agentbank.groupsales.provider import SimulatedProvider


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path / "var", SimulatedProvider(tmp_path / "sim.json", refund_settle_seconds=0))


def deal_by(e, company):
    return next(d for d in e.s["deals"].values() if d["company"].startswith(company) or d["domain"] == company)



def test_a_repeated_email_says_which_inquiry_it_already_is(eng):
    e = eng
    body = "Please quote 2 king rooms for 2 employees arriving 10 March and departing 12 March 2027."
    first = e.ingest_email("Rae <rae@repeat.example>", "Trip", body, wake=False)
    again = e.ingest_email("Rae <rae@repeat.example>", "Trip", "  " + body.upper() + " ", wake=False)
    assert again == {"deal_id": first["deal_id"], "duplicate": True, "ref": e.deal(first["deal_id"])["ref"]}
