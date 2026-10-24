"""Hotel payouts with reserves, the payout cycle, hotel incidents and changed terms at booking.
Offline, with the fakes."""
import unittest
from datetime import date, timedelta
from decimal import Decimal

import test_offer_desk as base
from test_offer_desk import TERMS, text_turn, tool_turn

from agentbank.airwallex import AmbiguousResult
from agentbank.reserves import ReservePolicy


class SettlementTests(unittest.TestCase):
    setUp = base.OfferDeskTests.setUp
    tearDown = base.OfferDeskTests.tearDown
    negotiate = base.OfferDeskTests.negotiate
    offer = base.OfferDeskTests.offer
    hold = base.OfferDeskTests.hold

    @property
    def api(self):
        return self.desk.payments.api

    def charged_now(self, **terms):
        """A non-refundable booking, charged in full at booking."""
        ref = self.desk.book(self.offer(cancellation="non_refundable", deposit_pct="1", **terms), True, "card")["booking_ref"]
        self.api.intents[self.desk.bookings.get(ref)["payment_intent_id"]]["status"] = "SUCCEEDED"
        self.assertEqual(self.desk.booking_status(ref)["status"], "paid")
        return ref

    def test_payout_holds_a_reserve_for_a_new_hotel(self):
        ref = self.hold()
        self.desk.payments.check_out(ref)
        st = self.desk.bookings.get(ref)["settlement"]
        self.assertEqual((st["paid_now"], st["reserve"], st["reserve_pct"]), ("352.00", "88.00", "0.20"))
        self.assertIn("new hotel", st["reserve_why"])
        self.assertEqual(st["parts"]["reserve"]["status"], "CREATED")   # held: auto_release off

    def test_ambiguous_payout_is_looked_up_before_retrying(self):
        ref = self.hold()
        real = self.api.create_funds_split

        def lands_then_times_out(*a, **k):
            real(*a, **k)
            raise AmbiguousResult("POST", "/pa/funds_splits/create", "rid", TimeoutError("read timeout"))
        self.api.create_funds_split = lands_then_times_out
        self.desk.payments.check_out(ref)
        self.assertIn("error", self.desk.bookings.get(ref)["settlement"])
        self.api.create_funds_split = real
        st = self.desk.payments.settle(ref)["settlement"]
        self.assertNotIn("error", st)
        self.assertEqual([c[0] for c in self.api.calls].count("split"), 1)   # never paid twice
        self.assertEqual([c[0] for c in self.api.calls].count("reserve"), 1)
        self.assertTrue(st["parts"]["now"]["id"] and st["parts"]["reserve"]["id"])

    def test_concurrent_refreshes_pay_the_hotel_once(self):
        import threading
        ref = self.desk.book(self.offer(cancellation="non_refundable", deposit_pct="1"), True, "card")["booking_ref"]
        self.api.intents[self.desk.bookings.get(ref)["payment_intent_id"]]["status"] = "SUCCEEDED"
        threads = [threading.Thread(target=self.desk.booking_status, args=(ref,), kwargs={"tenant": None})
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        b = self.desk.bookings.get(ref)
        self.assertEqual(b["status"], "paid")
        self.assertTrue(b["settlement"]["parts"]["now"]["id"])
        self.assertNotIn("summary", b["settlement"]["parts"])
        self.assertEqual([c[0] for c in self.api.calls].count("split"), 1)

    def test_payout_cycle_releases_reserves_of_delivered_stays_only(self):
        delivered, pending = self.hold(), self.charged_now(check_in="2026-11-20", check_out="2026-11-22")
        self.desk.payments.check_out(delivered)
        out = self.desk.run_payout_cycle()
        self.assertEqual([r["booking_ref"] for r in out["released"]], [delivered])
        self.assertEqual(out["held"], [{"booking_ref": pending, "why": "stay not delivered yet"}])
        self.assertEqual(self.desk.bookings.get(delivered)["settlement"]["parts"]["reserve"]["status"], "RELEASED")
        self.assertEqual(self.desk.run_payout_cycle()["released"], [])   # once only

    def test_overbooking_refunds_guests_and_recovers_the_hotels_share(self):
        held, paid = self.hold(), self.charged_now()
        room_night = ("DBL", date(2026, 11, 13))
        rooms_before = self.desk.hotels["htl_paris_marais"].booked[room_night]
        out = self.desk.sim.overbook("htl_paris_marais", "2026-11-13")
        by_ref = {o["booking_ref"]: o for o in out["bookings"]}
        self.assertEqual(by_ref[held]["status"], "cancelled_by_hotel")      # hold released, nothing charged
        self.assertEqual(by_ref[paid]["status"], "refunded_by_hotel")       # refunded in full
        calls = self.api.calls
        self.assertIn(("refund", "500.00 EUR"), calls)
        reversed_ = [c for c in calls if c[0] == "reverse"]
        self.assertEqual(sorted(c[1] for c in reversed_), ["352.00 EUR", "88.00 EUR"])   # reserve, then the rest
        self.assertEqual(self.desk.hotels["htl_paris_marais"].booked[room_night], rooms_before - 2)
        self.assertGreater(Decimal(out["reserve_after"]["pct"]), Decimal(out["reserve_before"]["pct"]))
        self.assertIn("not delivered", out["reserve_after"]["why"])
        self.assertEqual(self.desk.feed[-1]["kind"], "booking")   # the travellers' panels hear about it
        st = next(h for h in self.desk.insights()["hotels"] if h["hotel_id"] == "htl_paris_marais")["settlement"]
        self.assertEqual((st["owed"], st["recovered"], st["refunded"], st["reconciled"]), ("440.00", "440.00", "500.00", True))
        self.assertEqual(st["frozen_by"]["reason"], "overbooked")

    def test_incident_freezes_the_hotels_reserves(self):
        ref = self.hold()
        self.desk.payments.check_out(ref)
        self.desk.sim.overbook("htl_paris_marais", "2026-11-25")   # another night, no stays affected
        out = self.desk.run_payout_cycle()
        self.assertEqual(out["released"], [])
        self.assertIn("incident", out["held"][0]["why"])
        later = self.desk.now() + timedelta(days=31)
        self.desk.now = lambda: later
        self.assertEqual(len(self.desk.run_payout_cycle()["released"]), 1)

    def test_reserve_policy_from_the_hotels_own_record(self):
        policy = ReservePolicy()
        bookings = {str(i): {"hotel_id": "h", "status": "checked_out"} for i in range(6)}
        self.assertEqual(policy.pct("h", bookings)[0], Decimal("0.05"))
        bookings["x"] = {"hotel_id": "h", "status": "refunded_by_hotel"}
        self.assertEqual(policy.pct("h", bookings)[0], Decimal("0.29"))   # 1 of 7 failed, reserve 2x
        bookings["other"] = {"hotel_id": "g", "status": "refunded_by_hotel"}
        self.assertEqual(policy.pct("h", bookings)[0], Decimal("0.29"))   # other hotels don't count

    def test_changed_terms_at_booking_need_a_new_approval(self):
        offer_id = self.offer()   # EUR 500 for 2026-11-13..15, priced off normal-demand floors
        self.desk.sim.sell_rooms("htl_paris_marais", "DBL", "2026-11-14", 2)   # another channel: the night is now 85% full
        out = self.desk.book(offer_id, True, "card")
        self.assertEqual(out["result"], "terms_changed")
        self.assertGreater(Decimal(out["new_offer"]["total"].split()[0]), Decimal("500"))
        self.assertNotIn("late_checkout", out["new_offer"]["extras"]) if "late_checkout" in out["changed"] else None
        self.assertEqual(self.desk.bookings.all(), {})   # nothing booked or charged
        self.assertEqual(self.desk.feed[-1]["offers"][0]["offer_id"], out["new_offer"]["offer_id"])
        booked = self.desk.book(out["new_offer"]["offer_id"], True, "card")
        self.assertEqual(booked["result"], "booked")


if __name__ == "__main__":
    unittest.main()
