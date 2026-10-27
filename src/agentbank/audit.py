"""Append-only JSONL log of every plan, decision, approval and API action.
Doubles as the source for your demo's "what the agent did and why" timeline."""
from __future__ import annotations

import time
from pathlib import Path

from .storage import stream

_SECRET_KEYS = {"api_key", "x-api-key", "token", "authorization", "card_number", "cvc", "number", "client_secret"}


def _redact(value):
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _SECRET_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, path: str | Path = "var/audit.jsonl"):
        self.path = Path(path)
        self.stream = stream(self.path)   # the file, or Postgres when DATABASE_URL is set

    def record(self, event: str, **data) -> None:
        self.stream.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **_redact(data)})

    def entries(self) -> list[dict]:
        return self.stream.entries()
