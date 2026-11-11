"""The UX scenario harness (tests/ux_scenarios.py) runs against the real Claude API, which costs money. These tests
run it for free: the dry run end to end, and the paid path (Claude seller, buyer simulator, judge, cost meter) with a
fake API client that answers in the real response shapes."""
import json
from types import SimpleNamespace

import pytest

import ux_scenarios as ux


def usage(i=1000, o=100):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0)


class FakeAPI:
    """Seller calls (with tools): read the queue, then stop. Simulator calls (with an output format): valid JSON."""
    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if "tools" in kw:
            if len(kw["messages"]) == 1:
                content = [SimpleNamespace(type="tool_use", id=f"t{len(self.calls)}", name="list_queue", input={})]
                return SimpleNamespace(stop_reason="tool_use", content=content, usage=usage())
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Done.")],
                                   usage=usage())
        props = kw["output_config"]["format"]["schema"]["properties"]
        data = ({"addressed": "yes", "clear": "yes", "moves_forward": "yes", "invented": "no", "score": 4, "note": ""}
                if "score" in props else {"text": "King rooms please. The payer is Test Demo Ltd."})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(data))],
                               usage=usage(500, 50))


def test_cases_cover_every_dimension():
    cases = ux.make_cases(100, seed=1)
    assert len(cases) == 100 and len({c["id"] for c in cases}) == 100
    for dim, values in (("request", ux.REQUESTS), ("behaviour", ux.BEHAVIOURS), ("manager", ux.MANAGER),
                        ("payment", ux.PAYMENT)):
        assert {c[dim] for c in cases} == set(values)


def test_dry_run_completes_every_case_without_errors(tmp_path):
    cases = ux.make_cases(len(ux.REQUESTS) * 2, seed=7)
    results = [ux.run_case(c, None, dry=True) for c in cases]
    assert not [r["error"] for r in results if r["error"]]
    assert all(r["final_status"] and r["thread"] for r in results)
    assert ux.write_report(results, None, tmp_path, dry=True).exists()


def test_paid_path_runs_with_the_claude_seller_and_meters_cost(monkeypatch, tmp_path):
    api = FakeAPI()
    monkeypatch.setattr("agentbank.agent.loop.make_client", lambda: api)
    meter = ux.Meter(budget=100)
    case = next(c for c in ux.make_cases(20, seed=3) if c["request"] == "vague_typos")
    r = ux.run_case(case, meter, dry=False, max_rounds=3)
    assert r["error"] is None, r["error"]
    assert any("tools" in c for c in api.calls)                     # the Claude seller ran
    assert all(not t.get("strict") for c in api.calls if "tools" in c for t in c["tools"])
    assert r["cost_usd"] > 0 and meter.calls == len(api.calls) and meter.usd == pytest.approx(r["cost_usd"])
    assert ux.write_report([r], meter, tmp_path, dry=False).exists()


def test_the_budget_stops_a_run(monkeypatch):
    monkeypatch.setattr("agentbank.agent.loop.make_client", lambda: FakeAPI())
    meter = ux.Meter(budget=0.004)                                    # about one seller call
    r = ux.run_case(ux.make_cases(1, seed=1)[0], meter, dry=False)
    assert r["error"] and "budget" in r["error"]


def test_buyer_simulator_and_judge_parse_the_api_replies():
    api = FakeAPI()
    case = ux.make_cases(1, seed=1)[0]
    view = {"thread": [{"from": "buyer", "text": case["email"]["body"], "at": "t0"},
                       {"from": "seller", "text": "Which room type, and who pays?", "at": "t1"}]}
    assert ux.buyer_message(api, case, view, "answer the hotel's questions") == "King rooms please. The payer is Test Demo Ltd."
    grades = ux.judge(api, case, view)
    assert len(grades) == 1 and grades[0]["score"] == 4 and grades[0]["reply_at"] == "t1"
    system = api.calls[0]["system"]
    assert "Never mention that this is a test" in system and case["facts"]["payer"] in system
