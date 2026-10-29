"""Offline tests for the Kit 10 merchant side: the GMC catalogue and the signed create_checkout.
Run: python -m unittest discover tests"""
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from agentbank.agentic_commerce import (GMC_FIELDS, MerchantCheckout, Plan, batches, catalogue, nightly_price,
                                        parse_checkout, tsv)
from agentbank.audit import AuditLog
from agentbank.bookings import BookingStore
from agentbank.ledger import OperationLedger
from agentbank.money import Money
from agentbank.offer_desk import OfferDesk
from agentbank.offers import Cancellation, load_hotels
from test_offer_desk import NOW, FakeAirwallex

SECRET = "checkout-secret"
TS = str(int(NOW.timestamp() * 1000))
BASE = "https://hotels.example"


def signed(body: dict, secret: str = SECRET) -> tuple[dict, bytes]:
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), TS.encode() + raw, hashlib.sha256).hexdigest()
    return {"x-timestamp": TS, "x-signature": sig}, raw


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.hotels = load_hotels(ROOT / "data/hotels.json")
        self.rows = catalogue(self.hotels, BASE + "/")

    def test_at_least_100_unique_products_with_the_gmc_fields(self):
        self.assertGreaterEqual(len(self.rows), 100)   # Kit 10 asks for 100+
        self.assertEqual(len({r["id"] for r in self.rows}), len(self.rows))
        for r in self.rows:
            self.assertEqual(set(r), set(GMC_FIELDS))
            self.assertRegex(r["price"], r"^\d+\.\d{2} [A-Z]{3}$")
            self.assertTrue(r["link"].startswith(BASE + "/catalogue/"))
        self.assertTrue(all(len(b["gmc_products"]) <= 300 for b in batches(self.rows)))

    def test_price_is_room_plus_breakfast_per_guest_plus_refundable_premium(self):
        h = self.hotels["htl_paris_marais"]   # DBL 260, breakfast 25 per guest, free_until_48h +10%
        row = next(r for r in self.rows if r["id"] == "htl_paris_marais.DBL.bb2.f48")
        self.assertEqual(row["price"], "341.00 EUR")
        self.assertEqual(nightly_price(h, Plan.parse("htl_paris_marais.DBL.ro.nr")), Money.of(260, "EUR"))

    def test_feed_file_has_one_header_and_a_line_per_product(self):
        lines = tsv(self.rows).splitlines()
        self.assertEqual(lines[0].split("\t"), list(GMC_FIELDS))
        self.assertEqual(len(lines), len(self.rows) + 1)
        self.assertTrue(all(len(line.split("\t")) == len(GMC_FIELDS) for line in lines))


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.api = FakeAirwallex()
        self.desk = OfferDesk(load_hotels(ROOT / "data/hotels.json"), AuditLog(tmp / "audit.jsonl"),
                              OperationLedger(":memory:"), BookingStore(tmp / "bookings.json"), api=self.api,
                              pay_base_url=BASE, now=lambda: NOW)
        self.checkout = MerchantCheckout(self.desk, SECRET)

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, body: dict, secret: str = SECRET) -> tuple[int, dict]:
        headers, raw = signed(body, secret)
        return self.checkout.receive(headers, raw, now_ms=int(TS))

    def body(self, **kw) -> dict:
        return {"checkout_id": "chk_1", "items": [{"product_id": "htl_paris_marais.DBL.bb2.f48", "quantity": 1}],
                "check_in": "2026-11-20", "check_out": "2026-11-22", "guests": 2, **kw}

    def test_unsigned_or_wrongly_signed_calls_book_nothing(self):
        self.assertEqual(self.send(self.body(), secret="forged")[0], 401)
        self.assertEqual(MerchantCheckout(self.desk, "").receive({}, b"{}")[0], 404)   # off without a secret
        self.assertEqual(self.desk.bookings.all(), {})

    def test_catalogue_checkout_books_at_the_published_price_and_opens_the_deposit(self):
        status, out = self.send(self.body())
        self.assertEqual(status, 200, out)
        self.assertEqual(out["status"], "created")
        self.assertTrue(out["merchant_order_id"].startswith("BKG-"))
        self.assertEqual(out["total"], "682.00")   # two nights of the 341.00 product
        item = out["line_items"][0]
        self.assertEqual((item["guests"], item["extras"], item["cancellation"]), (2, ["breakfast"], "free_until_48h"))
        self.assertEqual(out["payment"]["type"], "deposit_hold")
        self.assertEqual(out["payment"]["url"], f"{BASE}/pay/{out['merchant_order_id']}")
        self.assertEqual([c[0] for c in self.api.calls], ["create"])   # one payment intent

    def test_a_retried_checkout_returns_the_same_order_and_never_a_second_room(self):
        first = self.send(self.body())[1]
        again = self.send(self.body())[1]
        self.assertEqual(again["merchant_order_id"], first["merchant_order_id"])
        self.assertEqual(len(self.desk.bookings.all()), 1)
        self.assertEqual(len(self.api.calls), 1)
        other = self.send(self.body(checkout_id="chk_2"))[1]
        self.assertNotEqual(other["merchant_order_id"], first["merchant_order_id"])

    def test_negotiated_offer_is_booked_at_its_agreed_price(self):
        catalogue_offer = self.checkout._from_catalogue(parse_checkout(self.body()))[0]
        cheaper = replace(self.desk.offers[catalogue_offer], total=Money.of(Decimal("600"), "EUR"))
        offer_id = self.desk.commit(cheaper, tenant="traveller_session")
        status, out = self.send({"checkout_id": "chk_neg", "items": [{"offer_id": offer_id}]})
        self.assertEqual((status, out["total"]), (200, "600.00"), out)
        self.assertEqual(self.desk.bookings.get(out["merchant_order_id"])["tenant"], "traveller_session")

    def test_bad_requests_are_refused_with_a_reason(self):
        cases = [self.body(items=[{"product_id": "htl_nowhere.DBL.ro.nr"}]),
                 self.body(items=[{"product_id": "htl_paris_opera.DBL.bb3.nr"}]),   # DBL sleeps 2
                 self.body(guests=1),                                                # breakfast for 2
                 self.body(check_out="2026-11-20"),
                 self.body(items=[{"product_id": "htl_paris_marais.DBL.ro.nr", "quantity": 2}]),
                 self.body(items=[{"offer_id": "ofr_unknown"}])]
        for body in cases:
            status, out = self.send(body)
            self.assertEqual(status, 422, body)
            self.assertIn("error", out)
        self.assertEqual(self.desk.bookings.all(), {})

    def test_sold_out_nights_are_reported_not_booked(self):
        h = self.desk.hotels["htl_paris_opera"]
        self.desk._reserve(h.id, "DBL", "2026-11-20", "2026-11-22", rooms=h.rooms["DBL"].total_rooms)
        out = self.send(self.body(items=[{"product_id": "htl_paris_opera.DBL.ro.nr"}], guests=2))[1]
        self.assertIn(out["status"], ("unavailable", "price_changed"))
        self.assertEqual(self.desk.bookings.all(), {})


if __name__ == "__main__":
    unittest.main()
