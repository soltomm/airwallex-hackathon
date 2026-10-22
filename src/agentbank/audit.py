"""Append-only JSONL log of every plan, decision, approval and API action.
Doubles as the source for your demo's "what the agent did and why" timeline."""
from __future__ import annotations

import json
import time
from pathlib import Path

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
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **data) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **_redact(data)}
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]
