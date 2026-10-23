"""Suggested rule changes and hotel actions from the revenue dashboard. Offline, with the fakes."""
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import test_offer_desk as base
from test_offer_desk import ROOT, TERMS, text_turn, tool_turn

from agentbank.offers import load_hotels
from agentbank.rule_tuning import RuleBook


class RuleTuningTests(unittest.TestCase):
    setUp = base.OfferDeskTests.setUp
    tearDown = base.OfferDeskTests.tearDown
    negotiate = base.OfferDeskTests.negotiate
    offer = base.OfferDeskTests.offer
    hold = base.OfferDeskTests.hold

    def walk_away(self, total, **terms):
        self.claude.turns = [tool_turn(("price_check", {**TERMS, "deposit_pct": "0.6", "total": str(total), **terms})),
                             text_turn("We can't go that low.")]
        self.desk.negotiate("htl_paris_marais", f"EUR {total}?")

    def later(self, hours=1):
        t = self.desk.now() + timedelta(hours=hours)
        self.desk.now = lambda: t

    def floor_suggestion(self):
        return next(s for s in self.desk.insights()["suggestions"] if s["kind"] == "night_floor")

    def test_lost_deals_below_the_floor_suggest_a_lower_floor_on_those_nights(self):
        marais = self.desk.hotels["htl_paris_marais"]
        self.walk_away(440)
        self.walk_away(450)
        self.later()
        s = self.floor_suggestion()
        self.assertEqual(set(s["change"]["nights"]), {"2026-11-13", "2026-11-14"})
        self.assertIn("2 travellers walked away", s["why"])
        night = date(2026, 11, 13)
        before = marais.floor_rate("DBL", night)
        applied = self.desk.apply_suggestion(s["id"])["applied"]
        self.assertLess(marais.floor_rate("DBL", night), before)
        self.assertLessEqual(before - marais.floor_rate("DBL", night), before * Decimal("0.15"))
        self.assertIn("DBL 2026-11-13", marais.rules()["night_floor_overrides"])   # the seller agent sees it
        self.assertNotIn(s["id"], [x["id"] for x in self.desk.insights()["suggestions"]])
        self.assertIn("error", self.desk.apply_suggestion(s["id"]))
        # Persisted and re-applied at start-up, then reverted to the original floor.
        path = Path(self.tmp.name) / "rules.json"
        RuleBook(self.desk.hotels, path).changes = self.desk.rulebook.changes
        self.desk.rulebook.path = path
        self.desk.rulebook._save()
        fresh = load_hotels(ROOT / "data/hotels.json")
        RuleBook(fresh, path)
        self.assertEqual(fresh["htl_paris_marais"].floor_rate("DBL", night), marais.floor_rate("DBL", night))
        self.desk.revert_change(applied["id"])
        self.assertEqual(marais.floor_rate("DBL", night), before)
        self.assertEqual(self.desk.insights()["rule_changes"][0]["reverted_at"], self.desk.now().isoformat())

    def test_one_lost_deal_is_not_enough_evidence(self):
        self.walk_away(440)
        self.later()
        self.assertFalse([s for s in self.desk.insights()["suggestions"] if s["kind"] == "night_floor"])

    def test_change_effect_on_conversion(self):
        self.walk_away(440)
        self.walk_away(450)
        self.later()
        self.desk.apply_suggestion(self.floor_suggestion()["id"])
        self.later()
        self.hold()
        effect = self.desk.insights()["rule_changes"][0]["effect"]
        self.assertEqual(effect["before"], {"negotiations": 2, "conversion_pct": "0"})
        self.assertEqual(effect["after"], {"negotiations": 1, "conversion_pct": "100"})

    def test_deals_near_the_floor_suggest_holding_out(self):
        marais = self.desk.hotels["htl_paris_marais"]
        for day in (13, 16, 19):   # three sales just above the autonomous floor
            terms = {"check_in": f"2026-11-{day}", "check_out": f"2026-11-{day + 2}", "extras": [], "cancellation": "non_refundable"}
            self.claude.turns = [tool_turn(("make_offer", {**TERMS, **terms, "deposit_pct": "1", "total": "435"})),
                                 text_turn("Offer made.")]
            offer_id = self.desk.negotiate("htl_paris_marais", "hi")["offers"][0]["offer_id"]
            self.desk.book(offer_id, True, "card")
        s = next(s for s in self.desk.insights()["suggestions"] if s["kind"] == "approval_band")
        self.desk.apply_suggestion(s["id"])
        self.assertEqual(marais.approval_band, Decimal("0.10"))

    def test_busy_nights_in_demand_suggest_a_higher_rate(self):
        opera = self.desk.hotels["htl_paris_opera"]
        room, night = next((r, n) for (r, n) in opera.booked
                           if opera.occupancy(r, n) >= opera.high_occupancy and opera.available(r, n))
        for _ in range(2):
            self.desk.search(night.isoformat(), (night + timedelta(days=1)).isoformat(), 2, [], "free_until_48h")
        s = next(s for s in self.desk.insights()["suggestions"] if s["kind"] == "night_rate" and s["change"]["room"] == room)
        before = opera.rate(room, night)
        self.desk.apply_suggestion(s["id"])
        self.assertGreater(opera.rate(room, night), before)

    def test_hotel_actions_capture_and_pay_out(self):
        ref = self.hold()
        out = self.desk.hotel_action(ref, "check-out")
        self.assertEqual(out["status"], "checked_out")
        self.assertTrue(self.desk.bookings.get(ref)["settlement"]["funds_split_id"])
        self.assertIn("error", self.desk.hotel_action(ref, "check-out"))
        self.assertIn("error", self.desk.hotel_action(ref, "refund-everything"))
        self.assertEqual(self.desk.feed[-1]["kind"], "booking")   # the traveller's panel hears about it


if __name__ == "__main__":
    unittest.main()
