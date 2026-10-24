"""Airwallex webhooks: verify, dedupe, answer fast, then let the API say what happened.

Signature: hex HMAC-SHA256 of the `x-timestamp` header followed by the raw body, keyed with the
notification URL's secret; checked on the raw bytes, never a re-serialised body. Delivery is
at-least-once, so events are deduped on their id. The handler only uses an event as a trigger: it
re-reads the PaymentIntent from Airwallex and moves the booking from that, so a replayed, reordered
or forged-but-valid-looking event can't put a booking in a state Airwallex doesn't report.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

MAX_SKEW_MS = 5 * 60 * 1000   # refuse events signed more than 5 minutes away from now


def signature_ok(secret: str, timestamp: str, raw: bytes, signature: str, now_ms: int | None = None) -> bool:
    if not (secret and timestamp and signature):
        return False
    try:
        if abs((now_ms if now_ms is not None else int(time.time() * 1000)) - int(timestamp)) > MAX_SKEW_MS:
            return False
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class WebhookInbox:
    def __init__(self, secret: str, handle: Callable[[dict], None], path: str | Path | None = None,
                 background: bool = True):
        self.secret = secret
        self.background = background   # answer first, handle after (tests handle inline)
        self.handle = handle
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.seen: set[str] = set()
        if self.path and self.path.exists():
            self.seen = {json.loads(line)["id"] for line in self.path.read_text().splitlines() if line}

    def receive(self, headers: dict, raw: bytes, now_ms: int | None = None) -> tuple[int, dict]:
        """(status, body) to answer with. Processing happens after the answer, on a thread."""
        h = {k.lower(): v for k, v in headers.items()}
        if not signature_ok(self.secret, h.get("x-timestamp", ""), raw, h.get("x-signature", ""), now_ms):
            return 401, {"error": "bad signature"}
        try:
            event = json.loads(raw)
            event_id = str(event["id"])
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "not an event"}
        with self._lock:
            if event_id in self.seen:
                return 200, {"duplicate": True}
            self.seen.add(event_id)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(json.dumps({"id": event_id, "name": event.get("name"), "at": time.time()}) + "\n")
        if self.background:
            threading.Thread(target=self._safe_handle, args=(event,), daemon=True).start()
        else:
            self._safe_handle(event)
        return 200, {"received": True}

    def _safe_handle(self, event: dict) -> None:
        try:
            self.handle(event)
        except Exception as e:   # a failing handler must not break delivery; the next status read catches up
            print(f"webhook {event.get('id')} ({event.get('name')}) failed: {e}", file=sys.stderr)   # stdout is MCP


def payment_intent_id(event: dict) -> str | None:
    """The PaymentIntent an event is about (payment_intent.* or payment_attempt.* events)."""
    obj = (event.get("data") or {}).get("object") or {}
    name = event.get("name", "")
    if name.startswith("payment_intent."):
        return obj.get("id")
    return obj.get("payment_intent_id")
