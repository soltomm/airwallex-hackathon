"""Offline tests for opening the hotel's Airwallex connected account (hotel_onboarding.open_account)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank.airwallex import AirwallexError
from agentbank.hotel_onboarding import account_details, accepts_cards, open_account


class FakeAccounts:
    """The Accounts API as the sandbox behaves: created -> submitted -> ACTIVE by simulation, then cards."""

    def __init__(self, fail_create=False):
        self.fail_create = fail_create
        self.calls, self.accounts, self.caps = [], {}, set()

    def post(self, path, body=None, on_behalf_of=None):
        self.calls.append(path)
        if path == "/accounts/create":
            if self.fail_create:
                raise AirwallexError(400, "validation_error", "business_name is invalid", {})
            acct = f"acct_{len(self.accounts) + 1}"
            self.accounts[acct] = "CREATED"
            return {"id": acct}
        acct = on_behalf_of or next(part for part in path.split("/") if part.startswith("acct_"))
        if path.endswith("/submit"):
            self.accounts[acct] = "SUBMITTED"
        elif path.endswith("/update_status"):
            self.accounts[acct] = body["next_status"]
        elif path.endswith("/enable"):
            self.caps.add(on_behalf_of)
        return {}

    def get(self, path, on_behalf_of=None):
        if path.startswith("/account_capabilities/"):
            return {"status": "ENABLED" if on_behalf_of in self.caps else "PENDING"}
        return {"status": self.accounts[path.split("/")[2]]}


class OpenAccountTests(unittest.TestCase):
    def open(self, api, **kw):
        steps = []
        ok = open_account(api, "Harbour House", "gm@harbour.example", request_id="req-1",
                          step=lambda status, info: steps.append(status), sleep=lambda s: None, **kw)
        return ok, steps

    def test_creates_activates_and_enables_cards(self):
        api = FakeAccounts()
        ok, steps = self.open(api)
        self.assertTrue(ok)
        self.assertEqual(steps, ["account_created", "submitted", "active", "capabilities_requested"])
        self.assertEqual(api.accounts, {"acct_1": "ACTIVE"})
        self.assertTrue(accepts_cards(api, "acct_1"))

    def test_resuming_an_existing_account_does_not_create_another(self):
        api = FakeAccounts()
        self.open(api)
        api.calls.clear()
        ok, steps = self.open(api, account="acct_1")
        self.assertTrue(ok)
        self.assertEqual(steps, [])
        self.assertNotIn("/accounts/create", api.calls)

    def test_a_refused_account_raises(self):
        with self.assertRaises(AirwallexError):
            self.open(FakeAccounts(fail_create=True))

    def test_store_name_has_no_accents(self):
        self.assertEqual(account_details("Hôtel Opéra")["store_details"]["store_name"], "Hotel Opera")


if __name__ == "__main__":
    unittest.main()
