"""Offline tests for the safety-critical pieces. Run: python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentbank.airwallex.client import AirwallexClient, Response
from agentbank.airwallex.errors import AirwallexError, AmbiguousResult
from agentbank.airwallex.states import TransferState, card_outcome, transfer_failure, transfer_state
from agentbank.approvals import ApprovalError, ApprovalRequest, ApprovalStore
from agentbank.config import Config, ConfigError
from agentbank.ledger import DuplicateOperation, OperationLedger, OpStatus
from agentbank.money import Money
from agentbank.policy import PaymentPolicy, Verdict

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


class MoneyTests(unittest.TestCase):
    def test_decimal_rounding_and_mismatch(self):
        self.assertEqual(Money.of("0.1", "usd") + Money.of("0.2", "USD"), Money.of("0.30", "USD"))
        self.assertEqual(Money.of("100.005", "EUR").amount, Decimal("100.01"))
        self.assertEqual(Money.of("1000.4", "JPY").amount, Decimal("1000"))
        with self.assertRaises(ValueError):
            Money.of(1, "USD") < Money.of(1, "EUR")


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


class StateTests(unittest.TestCase):
    def test_transfer_failure_shape_from_sandbox(self):
        t = {"status": "CANCELLED", "failure": {"code": "90802", "message": "Beneficiary bank returned",
                                                "details": {"type": "INCORRECT_ROUTING"}}}
        self.assertIs(transfer_state(t), TransferState.FAILED)
        self.assertEqual(transfer_failure(t).type, "INCORRECT_ROUTING")
        self.assertIs(transfer_state({"status": "SENT"}), TransferState.IN_FLIGHT)

    def test_declined_card_txn_is_not_accepted_despite_clearing(self):
        declined = {"transaction_type": "CLEARING", "status": "FAILED", "failure_reason": "LIMIT_EXCEEDED"}
        self.assertFalse(card_outcome(declined).accepted)
        self.assertEqual(card_outcome(declined).decline_reason, "LIMIT_EXCEEDED")
        ok = {"transaction_type": "CLEARING", "status": "PENDING"}
        self.assertTrue(card_outcome(ok).accepted)
        self.assertFalse(card_outcome(ok).settled)


class LedgerTests(unittest.TestCase):
    def test_runs_once_per_key(self):
        ledger = OperationLedger(":memory:")
        calls = []
        ledger.run("pay:INV-1", lambda rid: calls.append(rid) or {"id": "t1"})
        with self.assertRaises(DuplicateOperation):
            ledger.run("pay:INV-1", lambda rid: calls.append(rid) or {"id": "t2"})
        self.assertEqual(len(calls), 1)

    def test_ambiguous_then_reconcile_reuses_request_id(self):
        ledger = OperationLedger(":memory:")

        def boom(rid):
            raise AmbiguousResult("POST", "/transfers/create", rid, "timeout")

        with self.assertRaises(AmbiguousResult):
            ledger.run("pay:INV-2", boom)
        first = ledger.get("pay:INV-2")
        self.assertIs(first.status, OpStatus.UNKNOWN)
        with self.assertRaises(DuplicateOperation):
            ledger.run("pay:INV-2", lambda rid: {"id": "x"})  # no blind retry
        ledger.resolve_unknown("pay:INV-2", found=None)  # lookup by request_id found nothing
        seen = []
        ledger.run("pay:INV-2", lambda rid: seen.append(rid) or {"id": "t3"})
        self.assertEqual(seen, [first.request_id])


class ApprovalTests(unittest.TestCase):
    def req(self, amount="4200.00", **kw):
        return ApprovalRequest("transfer", Money.of(amount, "USD"), "Acme (ben_1)", "rent", **kw)

    def test_bound_to_exact_terms_and_single_use(self):
        store = ApprovalStore()
        aid = store.request(self.req())
        with self.assertRaises(ApprovalError):
            store.consume(aid, self.req())  # not approved yet
        store.approve(aid, "tommaso")
        with self.assertRaises(ApprovalError):
            store.consume(aid, self.req("4200.01"))  # price moved
        self.assertEqual(store.consume(aid, self.req()), "tommaso")
        with self.assertRaises(ApprovalError):
            store.consume(aid, self.req())  # already used

    def test_expiry(self):
        store = ApprovalStore(ttl_seconds=-1)
        aid = store.request(self.req())
        store.approve(aid, "x")
        with self.assertRaises(ApprovalError):
            store.consume(aid, self.req())


class PolicyTests(unittest.TestCase):
    policy = PaymentPolicy(reserve_floor={"USD": Money.of(10_000, "USD"), "EUR": Money.of(0, "EUR")},
                           autonomous_limit={"USD": Money.of(2_500, "USD")})

    def test_verdicts(self):
        avail = Money.of(20_000, "USD")
        self.assertIs(self.policy.evaluate_payout(Money.of(1_800, "USD"), "LOCAL", avail).verdict, Verdict.EXECUTE)
        self.assertIs(self.policy.evaluate_payout(Money.of(4_200, "USD"), "LOCAL", avail).verdict, Verdict.NEEDS_APPROVAL)
        self.assertIs(self.policy.evaluate_payout(Money.of(10_001, "USD"), "LOCAL", avail).verdict, Verdict.REJECT)
        low = self.policy.evaluate_payout(Money.of(100, "USD"), "LOCAL", avail, confidence=Decimal("0.5"))
        self.assertIs(low.verdict, Verdict.NEEDS_APPROVAL)

    def test_swift_fee_counts_against_floor(self):
        d = self.policy.evaluate_payout(Money.of(100, "EUR"), "SWIFT", Money.of("112.84", "EUR"))
        self.assertIs(d.verdict, Verdict.REJECT)  # 100 + 12.85 fee > 112.84


if __name__ == "__main__":
    unittest.main()
