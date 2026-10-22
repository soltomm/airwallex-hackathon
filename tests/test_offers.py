"""Offline tests for hotel offers and the seller policy. Run: python -m unittest discover tests"""
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank.approvals import ApprovalError, ApprovalStore
from agentbank.audit import AuditLog
from agentbank.money import Money
from agentbank.offers import (Cancellation, Negotiation, Offer, SellerPolicy, load_hotels,
                              offer_from_dict, offer_to_dict)
from agentbank.policy import Verdict

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def offer(hotel="htl_paris_marais", deposit="0.6", total="500", extras=("breakfast", "late_checkout"),
          cancellation=Cancellation.FREE_48H, **kw):
    return Offer(hotel, kw.get("room", "DBL"), date(2026, 11, 13), date(2026, 11, 15), kw.get("guests", 2),
                 extras, cancellation, Decimal(deposit), Money.of(total, "EUR"), NOW + timedelta(minutes=15))


class SellerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.hotels = load_hotels(ROOT / "data/hotels.json")
        self.marais = SellerPolicy(self.hotels["htl_paris_marais"], now=lambda: NOW)
        self.opera = SellerPolicy(self.hotels["htl_paris_opera"], now=lambda: NOW)

    def test_published_price_misses_budget_but_negotiated_offer_fits(self):
        self.assertGreater(self.hotels["htl_paris_marais"].published_total(offer()), Money.of(500, "EUR"))
        self.assertEqual(self.marais.evaluate(offer()).verdict, Verdict.EXECUTE)

    def test_bigger_deposit_buys_cheaper_free_cancellation(self):
        self.assertEqual(self.marais.evaluate(offer(deposit="0")).verdict, Verdict.REJECT)
        self.assertEqual(self.marais.evaluate(offer(deposit="0.2")).verdict, Verdict.NEEDS_APPROVAL)
        self.assertEqual(self.marais.evaluate(offer(deposit="0.6")).verdict, Verdict.EXECUTE)

    def test_public_reasons_never_leak_the_floor(self):
        d = self.marais.evaluate(offer(total="300"))
        self.assertEqual(d.verdict, Verdict.REJECT)
        self.assertTrue(any("floor" in r for r in d.internal))
        self.assertFalse(any(ch.isdigit() for r in d.public for ch in r))

    def test_busy_hotel_refuses_late_checkout_and_counter_drops_it(self):
        self.assertIn("late checkout", self.opera.evaluate(offer("htl_paris_opera")).public[0])
        c = self.opera.counter(offer("htl_paris_opera", deposit="0"))
        self.assertEqual(c.extras, ("breakfast",))
        self.assertEqual(c.deposit_pct, Decimal("0.20"))
        self.assertEqual(self.opera.evaluate(c).verdict, Verdict.EXECUTE)

    def test_counter_never_lowers_buyer_price(self):
        self.assertEqual(self.marais.counter(offer(total="650")).total, Money.of(650, "EUR"))

    def test_rejects_sold_out_expired_and_oversized(self):
        self.assertIn("no Junior Suite", self.marais.evaluate(offer(room="STE", total="2000")).public[0])
        self.assertEqual(self.marais.evaluate(replace(offer(), expires_at=NOW)).public, ("offer expired",))
        self.assertEqual(self.marais.evaluate(offer(guests=3)).verdict, Verdict.REJECT)
        self.assertEqual(self.marais.evaluate(offer(extras=("spa",))).verdict, Verdict.REJECT)

    def test_non_refundable_charges_full_amount(self):
        o = offer(deposit="0", cancellation=Cancellation.NON_REFUNDABLE, total="470")
        self.assertEqual(o.deposit, Money.of(470, "EUR"))
        self.assertTrue(self.marais.evaluate(o).allowed)

    def test_approval_bound_to_negotiated_terms(self):
        store = ApprovalStore()
        o = offer()
        aid = store.request(o.to_approval("Marais"))
        store.approve(aid, "traveller")
        with self.assertRaises(ApprovalError):
            store.consume(aid, replace(o, deposit_pct=Decimal("1")).to_approval("Marais"))
        self.assertEqual(store.consume(aid, o.to_approval("Marais")), "traveller")

    def test_dict_round_trip(self):
        o = offer()
        self.assertEqual(offer_from_dict(offer_to_dict(o), "EUR"), o)

    def test_negotiation_round_limit_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLog(Path(tmp) / "audit.jsonl")
            n = Negotiation(self.marais, max_rounds=2, audit=audit)
            n.propose("buyer", offer(deposit="0"))
            n.propose("seller", offer())
            with self.assertRaises(RuntimeError):
                n.propose("buyer", offer())
            self.assertEqual([e["verdict"] for e in audit.entries()], ["reject", "execute"])


if __name__ == "__main__":
    unittest.main()
