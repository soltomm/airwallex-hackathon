"""Offline tests for configuration loading and the Airwallex client (retries, ambiguous results)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentbank.airwallex.client import AirwallexClient, Response
from agentbank.airwallex.errors import AirwallexError, AmbiguousResult
from agentbank.config import Config, ConfigError

KEY = "a" * 96


class FakeTransport:
    """Replays queued responses; records calls."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/authentication/login"):
            return Response(201, {"token": "t", "expires_at": "2099-01-01T00:00:00+0000"})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses):
    t = FakeTransport(*responses)
    return AirwallexClient(Config("id", KEY), transport=t, sleep=lambda s: None), t


class ConfigTests(unittest.TestCase):
    def test_rejects_quoted_or_padded_client_id(self):
        # The bug that caused the HTML 403: a 24-char client ID with two stray characters.
        for bad in ('"AbCdEf1234567890xyzABC"', "AbCdEf1234567890xyzABC ", "Client IDAbCdEf1234567890xyzABC",
                    "IDAbCdEf1234567890xyzABC"):
            with self.subTest(bad=bad):
                os.environ.update(AWX_CLIENT_ID=bad, AWX_API_KEY=KEY)
                with self.assertRaises(ConfigError):
                    Config.from_env("/nonexistent")

    def test_dotenv_strips_quotes(self):
        for k in ("AWX_CLIENT_ID", "AWX_API_KEY"):
            os.environ.pop(k, None)
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write(f'AWX_CLIENT_ID="AbCdEf1234567890xyzABC"\nexport AWX_API_KEY={KEY}\n')
        cfg = Config.from_env(f.name)
        self.assertEqual(cfg.client_id, "AbCdEf1234567890xyzABC")
        for k in ("AWX_CLIENT_ID", "AWX_API_KEY"):
            os.environ.pop(k, None)


class ClientTests(unittest.TestCase):
    def test_get_retries_5xx_then_succeeds(self):
        c, t = client(Response(503, {}), Response(200, {"items": []}))
        self.assertEqual(c.get("/global_accounts"), {"items": []})

    def test_post_5xx_is_ambiguous_not_retried(self):
        c, t = client(Response(500, {}))
        with self.assertRaises(AmbiguousResult) as cm:
            c.post("/transfers/create", {"request_id": "r1"})
        self.assertEqual(cm.exception.request_id, "r1")
        self.assertEqual(len([x for x in t.calls if "transfers" in x[1]]), 1)

    def test_post_429_is_retried(self):
        c, _ = client(Response(429, {}), Response(201, {"id": "t1"}))
        self.assertEqual(c.post("/transfers/create", {"request_id": "r1"})["id"], "t1")

    def test_4xx_raises_with_code(self):
        c, _ = client(Response(400, {"code": "balance_insufficient", "message": "no"}))
        with self.assertRaises(AirwallexError) as cm:
            c.post("/transfers/create", {"request_id": "r1"})
        self.assertTrue(cm.exception.insufficient_funds)

    def test_on_behalf_of_header(self):
        c, t = client(Response(200, []))
        c.get("/balances/current", on_behalf_of="acct_123")
        self.assertEqual(t.calls[-1][2]["x-on-behalf-of"], "acct_123")


if __name__ == "__main__":
    unittest.main()
