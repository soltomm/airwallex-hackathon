"""Offline tests for the seller agent's tools and the offer desk. A scripted fake stands in
for Claude, so no API calls are made. Run: python -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank.audit import AuditLog
from agentbank.bookings import BookingStore
from agentbank.ledger import OperationLedger
from agentbank.offer_desk import OfferDesk
from agentbank.offers import load_hotels

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
TERMS = {"room_code": "DBL", "check_in": "2026-11-13", "check_out": "2026-11-15", "guests": 2,
         "extras": ["breakfast", "late_checkout"], "cancellation": "free_until_48h"}


class FakeAirwallex:
    """Records payment-intent calls; intents move through the states Airwallex reports."""

    def __init__(self):
        self.intents, self.calls = {}, []

    def create_payment_intent(self, request_id, amount, merchant_order_id):
        pid = f"int_{len(self.intents) + 1}"
        self.intents[pid] = {"id": pid, "status": "REQUIRES_PAYMENT_METHOD", "amount": amount.api(),
                             "client_secret": "secret"}
        self.calls.append(("create", str(amount)))
        return self.intents[pid]

    def get_payment_intent(self, pid):
        return self.intents[pid]

    def increment_authorization(self, request_id, pid, amount):
        self.calls.append(("increment", str(amount)))
        return self.intents[pid]

    def capture_payment_intent(self, request_id, pid, amount):
        self.calls.append(("capture", str(amount)))
        self.intents[pid]["status"] = "SUCCEEDED"
        return self.intents[pid]

    def cancel_payment_intent(self, request_id, pid, reason):
        self.calls.append(("cancel", reason))
        self.intents[pid]["status"] = "CANCELLED"
        return self.intents[pid]


class FakeClaude:
    """Replays assistant turns; records the requests it received."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})   # snapshot: the list keeps growing
        return self.turns.pop(0)


def tool_turn(*calls):
    return NS(stop_reason="tool_use", content=[
        NS(type="tool_use", id=f"tu_{i}", name=n, input=a) for i, (n, a) in enumerate(calls)])


def text_turn(text):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)])


class OfferDeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.claude = FakeClaude()
        self.desk = OfferDesk(load_hotels(ROOT / "data/hotels.json"), AuditLog(tmp / "audit.jsonl"),
                              OperationLedger(":memory:"), BookingStore(tmp / "bookings.json"),
                              api=FakeAirwallex(), client=self.claude, now=lambda: NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def negotiate(self, *turns, message="2 nights, breakfast, late checkout, free cancellation, EUR 500"):
        self.claude.turns = list(turns)
        return self.desk.negotiate("htl_paris_marais", message)

    def test_rules_are_configurable_per_hotel(self):
        h = self.desk.hotels
        self.assertIn("Protect the room rate", h["htl_paris_marais"].negotiation_style)
        self.assertEqual(str(h["htl_paris_opera"].breakfast_cost), "10")

    def test_seller_commits_only_what_policy_allows(self):
        reply = self.negotiate(
            tool_turn(("make_offer", {**TERMS, "deposit_pct": "0.2", "total": "450"})),
            tool_turn(("price_check", {**TERMS, "deposit_pct": "0.6", "total": "500"}),
                      ("make_offer", {**TERMS, "deposit_pct": "0.6", "total": "500"})),
            text_turn("We can do EUR 500 with a 60% deposit hold."))
        self.assertEqual(len(reply["offers"]), 1)
        self.assertEqual(reply["offers"][0]["total"], "500.00 EUR")
        # The rejected 450 offer came back to the seller as not committed, with the floor.
        first = json.loads(self.claude.requests[1]["messages"][-1]["content"][0]["content"])
        self.assertFalse(first["committed"])
        self.assertTrue(any("floor" in r for r in first["reasons"]))
        # Both tool results of one assistant turn go back in a single user message.
        self.assertEqual(len(self.claude.requests[2]["messages"][-1]["content"]), 2)

    def offer(self, **terms):
        reply = self.negotiate(tool_turn(("make_offer", {**TERMS, "deposit_pct": "0.6", "total": "500", **terms})),
                               text_turn("Offer made."))
        return reply["offers"][0]["offer_id"]

    def test_book_needs_confirmation_and_runs_once(self):
        offer_id = self.offer()
        self.assertEqual(self.desk.book(offer_id, False, "card")["result"], "not_booked")
        booked = self.desk.book(offer_id, True, "card")
        self.assertEqual(booked["result"], "booked")
        self.assertEqual(booked["status"], "awaiting_payment")
        self.assertEqual(booked["deposit"], "300.00")
        self.assertIn("/pay/" + booked["booking_ref"], booked["next_step"])
        self.assertNotIn("client_secret", booked)
        self.assertEqual(self.desk.book(offer_id, True, "card")["result"], "already_booked")
        self.assertEqual(self.desk.payments.api.calls, [("create", "300.00 EUR")])
        self.assertEqual(self.desk.hotels["htl_paris_marais"].booked[("DBL", datetime(2026, 11, 13).date())], 13)

    def hold(self, **terms):
        ref = self.desk.book(self.offer(**terms), True, "card")["booking_ref"]
        pid = self.desk.bookings.get(ref)["payment_intent_id"]
        self.desk.payments.api.intents[pid]["status"] = "REQUIRES_CAPTURE"   # traveller confirmed the hold
        self.assertEqual(self.desk.booking_status(ref)["status"], "deposit_held")
        return ref

    def test_free_cancellation_releases_hold_and_room(self):
        ref = self.hold()
        self.assertEqual(self.desk.booking_status(ref)["free_cancellation_until"], "2026-11-11T15:00:00+00:00")
        self.assertEqual(self.desk.cancel_booking(ref)["status"], "cancelled")
        self.assertEqual(self.desk.payments.api.calls[-1], ("cancel", "Free cancellation"))
        self.assertEqual(self.desk.hotels["htl_paris_marais"].booked[("DBL", datetime(2026, 11, 13).date())], 12)

    def test_late_cancellation_captures_deposit(self):
        ref = self.hold()
        self.desk.payments.now = lambda: datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.assertEqual(self.desk.cancel_booking(ref)["status"], "cancelled_fee_charged")
        self.assertEqual(self.desk.payments.api.calls[-1], ("capture", "300.00 EUR"))

    def test_check_out_raises_hold_to_total_then_captures(self):
        ref = self.hold()
        self.assertEqual(self.desk.payments.check_out(ref)["status"], "checked_out")
        self.assertEqual(self.desk.payments.api.calls[-2:], [("increment", "500.00 EUR"), ("capture", "500.00 EUR")])

    def test_metal_booking_is_paid_by_the_agent(self):
        booked = self.desk.book(self.offer(), True, "metal")
        ref = booked["booking_ref"]
        self.assertEqual(booked["metal"]["usdz"], "0.30")   # EUR 300 deposit at the demo testnet rate
        self.assertEqual(booked["metal"]["pay_to"], self.desk.hotels["htl_paris_marais"].metal_payee)
        self.assertIn(f"/bookings/{ref}/deposit", booked["next_step"])
        self.assertEqual(self.desk.payments.api.calls, [])   # no card payment for Metal bookings
        with self.assertRaises(Exception):
            self.desk.payments.record_metal_payment(ref, "0xabc", "0xpayer", "0.29")
        self.desk.payments.record_metal_payment(ref, "0xabc", "0xpayer", "0.30")
        self.assertEqual(self.desk.booking_status(ref)["status"], "paid")
        self.assertEqual(self.desk.cancel_booking(ref)["status"], "cancelled_refund_due")

    def test_bookings_survive_restart(self):
        ref = self.hold()
        again = OfferDesk(load_hotels(ROOT / "data/hotels.json"), self.desk.audit, self.desk.ledger,
                          self.desk.bookings, api=self.desk.payments.api, now=lambda: NOW)
        self.assertEqual(again.hotels["htl_paris_marais"].booked[("DBL", datetime(2026, 11, 13).date())], 13)
        self.assertEqual(again.booking_status(ref)["status"], "deposit_held")

    def test_conversation_continues_by_id(self):
        first = self.negotiate(text_turn("What is your budget?"))
        self.claude.turns = [text_turn("Thanks.")]
        second = self.desk.negotiate("htl_paris_marais", "EUR 500", first["negotiation_id"])
        self.assertEqual(second["seller_message"], "Thanks.")
        self.assertEqual(len(self.claude.requests[-1]["messages"]), 3)   # user, assistant, user
        self.assertIn("error", self.desk.negotiate("htl_paris_opera", "hi", first["negotiation_id"]))

    def test_bad_tool_input_is_an_error_result(self):
        self.negotiate(tool_turn(("make_offer", {**TERMS, "room_code": "PENTHOUSE", "deposit_pct": "0",
                                                 "total": "1"})), text_turn("Sorry."))
        result = self.claude.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])


if __name__ == "__main__":
    unittest.main()
