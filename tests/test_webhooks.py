"""Airwallex webhook signatures: checked on the raw bytes, within a timestamp window."""
import hashlib
import hmac
import json
import time
import unittest

from agentbank.webhooks import signature_ok

SECRET = "whsec_test"


def signed(event: dict, ts: int | None = None, secret: str = SECRET) -> tuple[dict, bytes]:
    raw = json.dumps(event, separators=(",", ":")).encode()
    ts = str(ts if ts is not None else int(time.time() * 1000))
    sig = hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()
    return {"X-Timestamp": ts, "X-Signature": sig}, raw


class WebhookTests(unittest.TestCase):
    def test_signature_is_checked_on_the_raw_bytes_within_five_minutes(self):
        headers, raw = signed({"id": "evt_1"})
        ts, sig = headers["X-Timestamp"], headers["X-Signature"]
        self.assertTrue(signature_ok(SECRET, ts, raw, sig))
        self.assertFalse(signature_ok(SECRET, ts, raw + b" ", sig))                       # re-serialised body
        self.assertFalse(signature_ok("other", ts, raw, sig))
        self.assertFalse(signature_ok(SECRET, ts, raw, sig, now_ms=int(ts) + 6 * 60 * 1000))   # replayed later
        self.assertFalse(signature_ok("", ts, raw, sig))
        self.assertFalse(signature_ok(SECRET, "not a number", raw, sig))


if __name__ == "__main__":
    unittest.main()
