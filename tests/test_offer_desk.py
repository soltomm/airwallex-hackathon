"""Offline tests for the seller agent's tools and the offer desk. A scripted fake stands in
for Claude, so no API calls are made. Run: python -m unittest discover tests"""
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank.airwallex import AirwallexError
from agentbank.audit import AuditLog
from agentbank.bookings import BookingStore
from agentbank.ledger import OperationLedger
from agentbank.offer_desk import Limits, OfferDesk
from agentbank.offers import load_hotels

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
MARAIS_ACCOUNT = load_hotels(ROOT / "data/hotels.json")["htl_paris_marais"].connected_account
TERMS = {"room_code": "DBL", "check_in": "2026-11-13", "check_out": "2026-11-15", "guests": 2,
         "extras": ["breakfast", "late_checkout"], "cancellation": "free_until_48h"}


class FakeAirwallex:
    """Records payment-intent calls; intents move through the states Airwallex reports."""

    def __init__(self):
        self.intents, self.calls = {}, []

    fail_next = False

    def create_payment_intent(self, request_id, amount, merchant_order_id, connected_account_id=None):
        if self.fail_next:
            self.fail_next = False
            raise AirwallexError(403, "unauthorized", "login failed", {})
        pid = f"int_{len(self.intents) + 1}"
        self.intents[pid] = {"id": pid, "status": "REQUIRES_PAYMENT_METHOD", "amount": amount.api(),
                             "client_secret": "secret", "connected_account_id": connected_account_id}
        self.calls.append(("create", str(amount)))
        return self.intents[pid]

    def create_funds_split(self, request_id, pid, amount, destination, metadata=None):
        if self.intents[pid]["status"] != "SUCCEEDED":
            raise AirwallexError(400, "validation_error", "Invalid source status for PAYMENT_INTENT creation", {})
        self.calls.append(("split", str(amount), destination))
        return {"id": f"spt_{len(self.calls)}", "status": "CREATED"}

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
        self.assertEqual(reply["round"], 1)
        self.assertEqual(reply["seller_activity"][1], "checked a pricing option against the hotel's rules")
        self.assertFalse(any("450" in a for a in reply["seller_activity"]))   # failed attempts reveal nothing
        self.assertIn("committed", reply["seller_activity"][-1])
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

    def test_book_from_worker_threads_like_the_mcp_sdk(self):
        offer_id = self.offer()   # the ledger's connection was opened on this (main) thread
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.desk.book(offer_id, True, "card")["result"]))
                   for _ in range(4)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(len(results), 4, "a booking call raised instead of answering")
        self.assertEqual(results.count("booked"), 1, results)
        self.assertTrue(set(results) <= {"booked", "already_booked", "booking_in_progress"}, results)
        self.assertEqual(self.desk.payments.api.calls, [("create", "300.00 EUR")])

    def test_airwallex_failure_keeps_booking_and_retries(self):
        self.desk.payments.api.fail_next = True
        booked = self.desk.book(self.offer(), True, "card")
        self.assertEqual(booked["result"], "booked_payment_unavailable")
        self.assertIn("login failed", booked["reason"])
        self.assertIn("retry", booked["next_step"])
        status = self.desk.booking_status(booked["booking_ref"])   # Airwallex is back
        self.assertIn("/pay/", status["next_step"])
        self.assertEqual(self.desk.payments.api.calls, [("create", "300.00 EUR")])

    def test_live_feed_for_the_negotiation_panel(self):
        self.desk.search("2026-11-13", "2026-11-15", 2, ["breakfast"], "free_until_48h")
        reply = self.negotiate(
            tool_turn(("get_inventory", {"check_in": "2026-11-13", "check_out": "2026-11-15"})),
            tool_turn(("make_offer", {**TERMS, "deposit_pct": "0.2", "total": "450"})),
            tool_turn(("make_offer", {**TERMS, "deposit_pct": "0.6", "total": "500"})),
            text_turn("EUR 500 with a 60% hold."))
        self.desk.book(reply["offers"][0]["offer_id"], True, "card")
        feed = self.desk.feed_since(0)
        kinds = [e["kind"] for e in feed["events"]]
        self.assertEqual(kinds, ["search", "buyer", "activity", "activity", "activity", "seller", "booking"])
        self.assertEqual(feed["events"][1]["round"], 1)
        self.assertEqual(feed["events"][5]["offers"][0]["total"], "500.00 EUR")
        self.assertNotIn("client_secret", feed["events"][-1]["booking"])
        text = json.dumps(feed)
        for secret in ("floor", "490.80", "450", "breakfast_cost", "occupancy_by_night"):   # floors, costs, failed tries
            self.assertNotIn(secret, text)
        self.assertEqual(self.desk.feed_since(feed["last"])["events"], [])

    def test_seller_unavailable_is_a_clean_reply(self):
        import anthropic
        def boom(**kw):
            raise anthropic.APIConnectionError(message="credit balance too low", request=None)
        self.claude.beta.messages.create = boom
        reply = self.desk.negotiate("htl_paris_marais", "2 nights please")
        self.assertIn("unavailable", reply["seller_message"])
        self.assertEqual(reply["offers"], [])
        seller = self.desk.sellers[reply["negotiation_id"]]
        self.assertEqual((seller.messages, seller.buyer_turns), ([], 0))   # nothing half-recorded

    def test_tenants_are_isolated(self):
        self.claude.turns = [tool_turn(("make_offer", {**TERMS, "deposit_pct": "0.6", "total": "500"})),
                             text_turn("Offer made.")]
        alice = self.desk.negotiate("htl_paris_marais", "hi", tenant="alice")
        offer_id = alice["offers"][0]["offer_id"]
        # Bob cannot continue Alice's negotiation, book her offer, or see her feed.
        self.assertIn("error", self.desk.negotiate("htl_paris_marais", "hi", alice["negotiation_id"], tenant="bob"))
        self.assertEqual(self.desk.book(offer_id, True, "card", tenant="bob")["result"], "not_booked")
        self.assertEqual(self.desk.feed_since(0, tenant="bob")["events"], [])
        ref = self.desk.book(offer_id, True, "card", tenant="alice")["booking_ref"]
        self.assertIn("error", self.desk.booking_status(ref, tenant="bob"))
        self.assertIn("error", self.desk.cancel_booking(ref, tenant="bob"))
        self.assertEqual(self.desk.booking_status(ref, tenant="alice")["status"], "awaiting_payment")
        self.assertEqual(self.desk.booking_status(ref, tenant=None)["status"], "awaiting_payment")   # hotel pages
        kinds = [e["kind"] for e in self.desk.feed_since(0, tenant="alice")["events"]]
        self.assertEqual(kinds, ["buyer", "activity", "seller", "booking"])
        self.assertNotIn("tenant", json.dumps(self.desk.feed_since(0, tenant="alice")))
        # Inventory is shared: Alice's booking took a room for everyone.
        self.assertEqual(self.desk.hotels["htl_paris_marais"].booked[("DBL", datetime(2026, 11, 13).date())], 13)

    def test_limits_for_a_shared_deployment(self):
        self.desk.limits = Limits(negotiations_per_tenant=1, turns_per_tenant=2)
        self.claude.turns = [text_turn("a"), text_turn("b")]
        first = self.desk.negotiate("htl_paris_marais", "hi", tenant="t")
        self.assertIn("error", self.desk.negotiate("htl_paris_opera", "hi", tenant="t"))   # second negotiation
        self.desk.negotiate("htl_paris_marais", "again", first["negotiation_id"], tenant="t")
        self.assertIn("limit", self.desk.negotiate("htl_paris_marais", "more", first["negotiation_id"],
                                                   tenant="t")["error"])
        self.desk.limits = Limits(daily_usd=Decimal("0"))
        self.assertIn("daily budget", self.desk.negotiate("htl_paris_marais", "hi", tenant="other")["error"])

    def test_payment_methods_can_be_restricted(self):
        self.desk.payment_methods = ("card",)
        self.assertIn("card", self.desk.book(self.offer(), True, "metal")["reason"])

    def test_prompt_caching_and_cost(self):
        from agentbank.seller_agent import usage_cost
        self.negotiate(tool_turn(("get_inventory", {"check_in": "2026-11-13", "check_out": "2026-11-15"})),
                       text_turn("Hello."))
        self.assertTrue(all(r.get("cache_control") == {"type": "ephemeral"} for r in self.claude.requests))
        million = {"input": 10**6, "cache_write": 10**6, "cache_read": 10**6, "output": 10**6}
        self.assertEqual(usage_cost(million, model="claude-sonnet-5-5"), Decimal("14.70"))   # 2 + 2.50 + 0.20 + 10
        self.assertEqual(usage_cost(million, model="claude-opus-5-5"), Decimal("29.20"))     # 4 + 5 + 0.20 + 20
        self.assertEqual(usage_cost({"input": 3_000_000}, {"input": 1_000_000}, "claude-sonnet-5-5"), Decimal("4"))
        self.assertEqual(self.claude.requests[0]["model"], "claude-sonnet-5-5")

    def test_offers_are_whole_euros(self):
        reply = self.negotiate(tool_turn(("make_offer", {**TERMS, "deposit_pct": "1", "total": "480.90"})),
                               tool_turn(("make_offer", {**TERMS, "deposit_pct": "1", "total": "495"})),
                               text_turn("EUR 495."))
        self.assertEqual([o["total"] for o in reply["offers"]], ["495.00 EUR"])
        self.assertTrue(self.claude.requests[1]["messages"][-1]["content"][0]["is_error"])

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
        self.assertEqual(self.desk.payments.api.calls[-2:], [("capture", "300.00 EUR"),
                                                             ("split", "264.00 EUR", MARAIS_ACCOUNT)])

    def test_check_out_raises_hold_to_total_then_captures(self):
        ref = self.hold()
        self.assertEqual(self.desk.payments.check_out(ref)["status"], "checked_out")
        self.assertEqual(self.desk.payments.api.calls[-3:], [("increment", "500.00 EUR"), ("capture", "500.00 EUR"),
                                                             ("split", "440.00 EUR", MARAIS_ACCOUNT)])

    def test_card_payments_pay_the_hotel_its_share(self):
        ref = self.hold()
        b = self.desk.bookings.get(ref)
        pid = b["payment_intent_id"]
        self.assertEqual(self.desk.payments.api.intents[pid]["connected_account_id"], MARAIS_ACCOUNT)
        self.assertNotIn("payout", self.desk.booking_status(ref))   # commission terms are not the buyer's business
        self.desk.payments.check_out(ref)
        settlement = self.desk.bookings.get(ref)["settlement"]
        self.assertEqual((settlement["captured"], settlement["commission"], settlement["hotel_payout"]),
                         ("500.00", "60.00", "440.00"))
        self.assertTrue(settlement["funds_split_id"].startswith("spt_"))
        self.desk.payments.settle(ref)   # already paid out: no second split
        self.assertEqual(sum(c[0] == "split" for c in self.desk.payments.api.calls), 1)

    def test_failed_payout_keeps_the_capture_and_retries(self):
        ref = self.hold()
        api = self.desk.payments.api
        real = api.create_funds_split
        api.create_funds_split = lambda *a, **k: (_ for _ in ()).throw(AirwallexError(500, "x", "down", {}))
        self.assertEqual(self.desk.payments.check_out(ref)["status"], "checked_out")
        self.assertIn("error", self.desk.bookings.get(ref)["settlement"])
        api.create_funds_split = real
        self.assertTrue(self.desk.payments.settle(ref)["settlement"]["funds_split_id"])

    def test_insights_for_the_hotel(self):
        self.desk.search("2026-11-13", "2026-11-15", 2, ["breakfast"], "free_until_48h")
        ref = self.hold()                                                    # one deal at EUR 500
        self.claude.turns = [tool_turn(("price_check", {**TERMS, "deposit_pct": "0.6", "total": "300"})),
                             text_turn("We can't go that low.")]
        self.desk.negotiate("htl_paris_marais", "EUR 300 please")             # one lost below the floor
        self.desk.payments.check_out(ref)
        self.desk.now = lambda: NOW + timedelta(hours=1)
        r = self.desk.insights()
        marais = next(h for h in r["hotels"] if h["hotel_id"] == "htl_paris_marais")
        self.assertEqual((marais["funnel"]["negotiations"], marais["funnel"]["bookings"], marais["funnel"]["lost"]),
                         (2, 1, 1))
        self.assertEqual(marais["conversion_pct"], "50.0")
        lost = marais["lost"][0]
        self.assertEqual((lost["reason"], lost["asked"]), ("price below floor", "300.00"))
        self.assertGreater(Decimal(lost["gap"]), 0)
        self.assertIsNotNone(marais["margin"]["kept_of_band_pct"])
        self.assertEqual((marais["money"]["commission"], marais["money"]["hotel_payout"]), ("60.00", "440.00"))
        self.assertEqual(marais["nights"][0], {"night": "2026-11-13", "requested": 1, "booked": 1, "lost": 1})
        self.assertTrue(any("below the floor" in line for line in r["headlines"]))

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
