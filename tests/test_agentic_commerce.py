"""Offline tests for the Kit 10 merchant side: the GMC catalogue of room-nights, the signed
create_checkout quote, and the booking made when Airwallex reports the payment.
Run: python -m unittest discover tests"""
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from agentbank.agentic_commerce import (DAYS, GMC_FIELDS, MerchantCheckout, Night, batches, catalogue, checkout_id_in,
                                        night_price, plans, tsv)
from agentbank.audit import AuditLog
from agentbank.bookings import BookingStore
from agentbank.ledger import OperationLedger
from agentbank.money import Money
from agentbank.offer_desk import OfferDesk
from agentbank.offers import load_hotels
from test_offer_desk import NOW, FakeAirwallex

SECRET = "checkout-secret"
TS = str(int(NOW.timestamp() * 1000))
BASE = "https://hotels.example"
PLAN = "htl_paris_marais.DBL.bb2.f48"   # DBL 260, breakfast 25 per guest, free_until_48h +10%: 341.00 a night


def signed(body: dict, secret: str = SECRET) -> tuple[dict, bytes]:
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), TS.encode() + raw, hashlib.sha256).hexdigest()
    return {"x-timestamp": TS, "x-signature": sig}, raw


def cart(*nights: str, plan: str = PLAN, qty: int = 1, **kw) -> dict:
    return {"line_items": [{"id": f"li_{i}", "item_id": f"{plan}.{n}", "requested_quantity": qty}
                           for i, n in enumerate(nights)], "currency": "EUR", **kw}


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.hotels = load_hotels(ROOT / "data/hotels.json")
        self.rows = catalogue(self.hotels, BASE + "/", NOW.date())

    def test_one_product_per_plan_and_night_with_the_gmc_fields(self):
        self.assertEqual(len(self.rows), sum(len(plans(h)) for h in self.hotels.values()) * DAYS)
        self.assertEqual(len({r["id"] for r in self.rows}), len(self.rows))
        for r in self.rows:
            self.assertEqual(set(r), set(GMC_FIELDS))
            self.assertRegex(r["price"], r"^\d+\.\d{2} [A-Z]{3}$")
            self.assertRegex(r["id"], r"\.\d{8}$")   # one product per night
            self.assertTrue(r["link"].startswith(BASE + "/catalogue/"))
        self.assertTrue(all(len(b["gmc_products"]) <= 300 for b in batches(self.rows)))

    def test_price_is_that_nights_rate_plus_breakfast_per_guest_plus_refundable_premium(self):
        row = next(r for r in self.rows if r["id"] == f"{PLAN}.20261120")
        self.assertEqual(row["price"], "341.00 EUR")
        n = Night.parse("htl_paris_marais.DBL.ro.nr.20261120")
        self.assertEqual(night_price(self.hotels["htl_paris_marais"], n.plan, n.night), Money.of(260, "EUR"))

    def test_sold_out_nights_are_listed_out_of_stock(self):
        h = self.hotels["htl_paris_marais"]
        h.booked[("DBL", date(2026, 11, 20))] = h.rooms["DBL"].total_rooms
        rows = {r["id"]: r for r in catalogue(self.hotels, BASE, NOW.date())}
        self.assertEqual(rows[f"{PLAN}.20261120"]["availability"], "out_of_stock")
        self.assertEqual(rows[f"{PLAN}.20261121"]["availability"], "in_stock")

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

    def paid(self, quote: dict, amount: str = "682.00", pid: str = "int_1") -> dict:
        method = quote["fulfillment"]["methods"][0]
        # Airwallex's MCP created and confirmed this intent on our account
        self.api.intents[pid] = {"id": pid, "status": "SUCCEEDED", "amount": float(amount)}
        return {"id": f"evt_{pid}", "name": "payment_intent.succeeded", "data": {"object": {
            "id": pid, "amount": float(amount), "currency": "EUR", "status": "SUCCEEDED",
            "agentic_payment_data": {"fulfillment": {"method_id": method["id"], "line_item_ids": method["line_item_ids"],
                                                     "option_id": method["options"][0]["id"]}}}}}

    def test_unsigned_or_wrongly_signed_calls_are_refused(self):
        self.assertEqual(self.send(cart("20261120"), secret="forged")[0], 401)
        self.assertEqual(MerchantCheckout(self.desk, "").receive({}, b"{}")[0], 404)   # off without a secret

    def test_checkout_quotes_the_stay_in_minor_units_and_books_nothing(self):
        status, out = self.send(cart("20261121", "20261120"))   # any order
        self.assertEqual(status, 200, out)
        self.assertTrue(out["id"].startswith("chk_"))
        self.assertEqual([i["available_quantity"] for i in out["line_items"]], [1, 1])
        self.assertEqual({t["type"]: t["amount"] for t in out["totals"]}, {"subtotal": 68200, "total": 68200})
        [method] = out["fulfillment"]["methods"]
        self.assertEqual((method["type"], method["line_item_ids"]), ("DIGITAL", ["li_0", "li_1"]))
        self.assertIn(out["id"], method["id"])
        self.assertEqual(out["permalink_url"], f"{BASE}/catalogue/{PLAN}.20261120")
        self.assertEqual((self.desk.bookings.all(), self.api.calls), ({}, []))

    def test_one_room_per_checkout(self):
        out = self.send(cart("20261120", qty=2))[1]
        self.assertEqual(out["line_items"][0]["available_quantity"], 1)

    def test_bad_carts_are_refused_with_a_reason(self):
        cases = [cart("20261120", plan="htl_nowhere.DBL.ro.nr"),
                 cart("20261120", plan="htl_paris_opera.DBL.bb3.nr"),   # DBL sleeps 2
                 cart("20261120", "20261122"),                          # a gap
                 cart("20261120", "20261120"),
                 {**cart("20261120"), "line_items": cart("20261120")["line_items"]
                  + cart("20261121", plan="htl_paris_marais.DBL.ro.nr")["line_items"]},
                 cart("20261120", currency="USD"),
                 cart("20261199"),
                 {"line_items": []}]
        for body in cases:
            status, out = self.send(body)
            if body["line_items"]:   # answered as a checkout Airwallex can read: nothing available, and why
                self.assertEqual(status, 200, body)
                self.assertTrue(out["id"].startswith("chk_"))
                self.assertEqual({i["available_quantity"] for i in out["line_items"]}, {0})
                self.assertTrue(out["messages"][0]["content"])
                self.assertNotIn("fulfillment", out)
            else:
                self.assertEqual(status, 400)
        self.assertEqual(len(self.checkout.rejected), len(cases))

    def test_sold_out_or_past_nights_are_quoted_unavailable(self):
        h = self.desk.hotels["htl_paris_marais"]
        self.desk._reserve(h.id, "DBL", "2026-11-20", "2026-11-21", rooms=h.rooms["DBL"].total_rooms)
        for body in (cart("20261120", "20261121"), cart("20261030")):
            out = self.send(body)[1]
            self.assertEqual({i["available_quantity"] for i in out["line_items"]}, {0})
            self.assertNotIn("fulfillment", out)

    def test_payment_books_the_quoted_stay_once(self):
        quote = self.send(cart("20261120", "20261121", customer={"email": "sam@example.org", "first_name": "Sam"}))[1]
        ref = self.checkout.on_payment(self.paid(quote))
        b = self.desk.bookings.get(ref)
        self.assertEqual((b["status"], b["method"], b["total"], b["payment_intent_id"]), ("paid", "agentic", "682.00", "int_1"))
        self.assertEqual((b["check_in"], b["check_out"], b["guests"], b["extras"]), ("2026-11-20", "2026-11-22", 2, ["breakfast"]))
        self.assertEqual(b["guest"], {"first_name": "Sam", "email": "sam@example.org"})
        self.assertIn("split", [c[0] for c in self.api.calls])   # the hotel's share paid out
        self.assertEqual(self.checkout.on_payment(self.paid(quote)), ref)   # redelivered: same booking
        self.assertEqual(len(self.desk.bookings.all()), 1)

    def test_payments_that_cannot_be_booked_are_refunded(self):
        quote = self.send(cart("20261120"))[1]
        self.assertIsNone(self.checkout.on_payment(self.paid(quote, amount="1.00")))
        self.assertEqual(self.api.calls[-1], ("refund", "1.00 EUR"))
        quote = self.send(cart("20261121"))[1]
        h = self.desk.hotels["htl_paris_marais"]
        self.desk._reserve(h.id, "DBL", "2026-11-21", "2026-11-22", rooms=h.rooms["DBL"].total_rooms)   # sold meanwhile
        self.assertIsNone(self.checkout.on_payment(self.paid(quote, amount="341.00", pid="int_2")))
        self.assertEqual(self.api.calls[-1], ("refund", "341.00 EUR"))
        self.assertEqual(self.desk.bookings.all(), {})

    def test_checkout_id_is_found_in_base64_encoded_fulfillment_ids(self):
        # as seen live: Airwallex echoes our option id base64url-encoded, then ".", then the method id
        option = "Y29uZmlybWF0aW9uX2Noa183ZTVkMGFkYjBmMGU0MTY0.c3RheV9jaGtfN2U1ZDBhZGIwZjBlNDE2NA."
        self.assertEqual(checkout_id_in({"option_id": option}), "chk_7e5d0adb0f0e4164")
        self.assertEqual(checkout_id_in({"method_id": "stay_chk_7e5d0adb0f0e4164"}), "chk_7e5d0adb0f0e4164")
        self.assertIsNone(checkout_id_in({"option_id": "c29tZXRoaW5nX2Vsc2VfZW50aXJlbHk"}))

    def test_other_events_are_left_to_the_desk(self):
        self.assertIsNone(self.checkout.on_payment({"id": "evt_x", "name": "payment_intent.succeeded",
                                                    "data": {"object": {"id": "int_card"}}}))

    def test_free_cancellation_refunds_in_full(self):
        quote = self.send(cart("20261120", "20261121"))[1]
        ref = self.checkout.on_payment(self.paid(quote))
        b = self.desk.payments.cancel(ref)
        self.assertEqual(b["status"], "cancelled")
        self.assertIn(("refund", "682.00 EUR"), self.api.calls)


if __name__ == "__main__":
    unittest.main()
