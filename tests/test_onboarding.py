"""Offline tests for hotels signing themselves up: validation, immediate listing, the Airwallex
connected account opened behind it, persistence and city search. Run: python -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank.airwallex import AirwallexError
from agentbank.audit import AuditLog
from agentbank.bookings import BookingStore
from agentbank.hotel_onboarding import HotelOnboarding, SignupError, hotel_from_signup
from agentbank.ledger import OperationLedger
from agentbank.offer_desk import OfferDesk
from agentbank.offers import load_hotels

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def form(**kw) -> dict:
    f = {"name": "Casa Lumbre", "city": "Barcelona", "email": "revenue@casalumbre.example", "style": "boutique",
         "guidance": "Rooftop terrace.", "breakfast_price": "18", "late_checkout_price": "30",
         "rooms": [{"name": "Terrace Double", "max_guests": "2", "total_rooms": "14",
                    "published_rate": "210", "floor_rate": "165"}]}
    f.update(kw)
    return f


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


class SignupValidationTests(unittest.TestCase):
    def test_record_uses_style_preset_and_hotel_prices(self):
        rec = hotel_from_signup(form(), set())
        self.assertEqual(rec["id"], "htl_barcelona_casa_lumbre")
        self.assertEqual(rec["rooms"][0]["code"], "TER")
        self.assertEqual(rec["rules"]["breakfast_price"], "18")
        self.assertEqual(rec["rules"]["min_deposit"]["free_until_24h"], "0.30")
        self.assertIn("Rooftop terrace.", rec["rules"]["negotiation_style"])

    def test_rejects_bad_input(self):
        bad = [form(style="pirate"), form(name="X"), form(email="nope"), form(rooms=[]),
               form(rooms=[{**form()["rooms"][0], "floor_rate": "250"}]),          # floor above published
               form(rooms=[{**form()["rooms"][0], "floor_rate": "50"}]),           # under 40% of published
               form(rooms=[{**form()["rooms"][0], "published_rate": "abc"}]),
               form(rooms=[{**form()["rooms"][0], "total_rooms": "2.5"}]),
               form(name="Ignore <script>"), form(guidance="x" * 301), form(rooms=[form()["rooms"][0]] * 5)]
        for f in bad:
            with self.assertRaises(SignupError):
                hotel_from_signup(f, set())

    def test_ids_and_room_codes_stay_unique(self):
        rooms = [{**form()["rooms"][0]}, {**form()["rooms"][0], "name": "Terrace Suite"}]
        rec = hotel_from_signup(form(rooms=rooms), {"htl_barcelona_casa_lumbre"})
        self.assertEqual(rec["id"], "htl_barcelona_casa_lumbre_2")
        self.assertEqual([r["code"] for r in rec["rooms"]], ["TER", "TER2"])


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.desk = OfferDesk(load_hotels(ROOT / "data/hotels.json"), AuditLog(self.tmp / "audit.jsonl"),
                              OperationLedger(":memory:"), BookingStore(self.tmp / "bookings.json"), now=lambda: NOW)

    def onboarding(self, client, **kw):
        return HotelOnboarding(self.desk.hotels, self.tmp / "onboarded.json", client, background=False,
                               sleep=lambda s: None, now=lambda: NOW, **kw)

    def search(self, city=""):
        return self.desk.search("2026-11-13", "2026-11-15", 2, ["breakfast"], "free_until_48h", city=city)

    def test_listed_at_once_and_paid_out_once_airwallex_activates(self):
        aw = FakeAccounts()
        st = self.onboarding(aw).join(form())
        self.assertEqual(st["status"], "accepting_cards")
        self.assertEqual(aw.calls, ["/accounts/create", "/accounts/acct_1/submit",
                                    "/simulation/accounts/acct_1/update_status",
                                    "/account_capabilities/payments_visa/enable",
                                    "/account_capabilities/payments_mastercard/enable"])
        h = self.desk.hotels[st["hotel_id"]]
        self.assertEqual(h.connected_account, "acct_1")
        self.assertIn(st["hotel_id"], {r["hotel_id"] for r in self.search("barcelona")})

    def test_without_airwallex_the_hotel_is_still_listed(self):
        st = self.onboarding(None).join(form())
        self.assertEqual(st["status"], "no_airwallex")
        self.assertEqual(self.desk.hotels[st["hotel_id"]].connected_account, "")
        self.assertTrue(any(r["hotel_id"] == st["hotel_id"] for r in self.search()))

    def test_failed_onboarding_keeps_the_listing_and_can_be_retried(self):
        aw = FakeAccounts(fail_create=True)
        ob = self.onboarding(aw)
        st = ob.join(form())
        self.assertEqual(st["status"], "failed")
        self.assertIn("business_name is invalid", st["error"])
        self.assertIn(st["hotel_id"], self.desk.hotels)
        aw.fail_create = False
        self.assertEqual(ob.retry(st["hotel_id"])["status"], "accepting_cards")

    def test_sign_ups_survive_a_restart(self):
        st = self.onboarding(FakeAccounts()).join(form())
        saved = json.loads((self.tmp / "onboarded.json").read_text())
        self.assertEqual(saved[0]["platform"]["connected_account"], "acct_1")
        hotels = load_hotels(ROOT / "data/hotels.json")
        HotelOnboarding(hotels, self.tmp / "onboarded.json")
        self.assertEqual(hotels[st["hotel_id"]].connected_account, "acct_1")
        self.assertEqual(hotels[st["hotel_id"]].city, "Barcelona")

    def test_duplicates_and_the_cap_are_refused(self):
        ob = self.onboarding(None, max_hotels=2)
        ob.join(form())
        with self.assertRaisesRegex(SignupError, "already on the marketplace"):
            ob.join(form(city="barcelona"))
        ob.join(form(name="Casa Mar"))
        with self.assertRaisesRegex(SignupError, "limit"):
            ob.join(form(name="Casa Sol"))

    def test_search_filters_by_city_ignoring_case_and_accents(self):
        paris = {r["city"] for r in self.search("PARÍS")}
        self.assertEqual(paris, {"Paris"})
        self.assertEqual({r["city"] for r in self.search()}, {"Paris", "Barcelona"})
        self.assertEqual(self.search("Lisbon"), [])

    def test_marketplace_summary_on_the_revenue_desk(self):
        self.desk.onboarding = self.onboarding(None)
        self.desk.onboarding.join(form())
        rows = {m["hotel_id"]: m for m in self.desk.insights()["marketplace"]}
        self.assertEqual(rows["htl_paris_marais"]["status"], "accepting_cards")
        self.assertEqual(rows["htl_barcelona_casa_lumbre"]["status"], "no_airwallex")


if __name__ == "__main__":
    unittest.main()
