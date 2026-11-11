"""Tests read buyer text with the pattern extractor unless a test injects a reader: a developer's Anthropic key
must never turn the suite into live, billed, non-deterministic model calls."""
import pytest


@pytest.fixture(autouse=True)
def _no_live_model_reader(monkeypatch):
    monkeypatch.setenv("GS_LLM_READER", "off")
