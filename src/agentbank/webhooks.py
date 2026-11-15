"""Airwallex webhook signatures.

Signature: hex HMAC-SHA256 of the `x-timestamp` header followed by the raw body, keyed with the
notification URL's secret; checked on the raw bytes, never a re-serialised body. Deduping on the event
id and re-reading the payment from Airwallex happen in the group-sales engine.
"""
from __future__ import annotations

import hashlib
import hmac
import time

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
