"""Card deposits in the guest's currency (Airwallex Multi-Currency Pricing), offline."""
import unittest
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from test_offer_desk import FakeAirwallex, OfferDeskTests

from agentbank.airwallex import AirwallexError
from agentbank.guest_currency import convert
from agentbank.money import Money

RATES = {"GBP": "0.8676901117", "USD": "1.160309925"}


class FakeAirwallexFX(FakeAirwallex):
    """Adds conversion quotes; applying one re-denominates the intent as Airwallex does."""

    quote_fails = False

    def create_conversion_quote(self, request_id, merchant_currency, shopper_currency, validity_period="HR_24"):
        if self.quote_fails:
            raise AirwallexError(400, "validation_error", "currency not supported", {})
        self.calls.append(("quote", f"{merchant_currency}->{shopper_currency}"))
        return {"id": f"cvq_{shopper_currency}", "conversion_rate": float(RATES[shopper_currency]),
                "expires_at": "2026-11-02T00:00:00Z", "status": "CREATED"}

    def apply_conversion_quote(self, request_id, pid, quote_id):
        cur = quote_id.removeprefix("cvq_")
        pi = self.intents[pid]
        if pi.get("connected_account_id"):   # as the sandbox answers
            raise AirwallexError(400, "validation_error",
                                 "Conversion quote is not allowed to be updated for platform payments", {})
        amount = (Decimal(str(pi["amount"])) * Decimal(RATES[cur])).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
        pi.update(original_amount=pi["amount"], original_currency="EUR", amount=float(amount), currency=cur,
                  conversion_quote_id=quote_id)
        self.calls.append(("apply", quote_id))
        return pi


class GuestCurrencyTests(unittest.TestCase):
    setUp = OfferDeskTests.setUp
    tearDown = OfferDeskTests.tearDown
    negotiate = OfferDeskTests.negotiate
    offer = OfferDeskTests.offer

    def book(self, currency="GBP", fails=False):
        self.desk.payments.api = api = FakeAirwallexFX()
        api.quote_fails = fails
        return self.desk.book(self.offer(), True, "card", currency=currency)

    def held(self, booked):
        intent = self.desk.payments.api.intents[booked["payment_intent_id"]]
        intent["status"] = "REQUIRES_CAPTURE"
        return self.desk.booking_status(booked["booking_ref"])

    def test_convert_rounds_half_to_even(self):
        self.assertEqual(convert(Money.of("0.25", "EUR"), "0.5", "GBP"), Money.of("0.12", "GBP"))   # 0.125
        self.assertEqual(convert(Money.of("0.75", "EUR"), "0.5", "GBP"), Money.of("0.38", "GBP"))   # 0.375
        self.assertEqual(convert(Money.of("500", "EUR"), RATES["GBP"], "GBP"), Money.of("433.85", "GBP"))

    def test_deposit_held_in_guest_currency_at_locked_rate(self):
        booked = self.book("gbp")
        self.assertEqual(booked["result"], "booked")
        self.assertEqual((booked["deposit"], booked["total"], booked["currency"]), ("300.00", "500.00", "EUR"))
        fx = booked["fx"]
        self.assertEqual((fx["currency"], fx["rate"], fx["deposit"], fx["total"]),
                         ("GBP", "0.8676901117", "260.31", "433.85"))
        self.assertIn("260.31 GBP (300.00 EUR)", booked["next_step"])
        self.assertEqual([c[0] for c in self.desk.payments.api.calls], ["create", "quote", "apply"])
        self.assertEqual(self.held(booked)["fx"], fx)   # status re-reads do not re-quote
        self.assertEqual(self.desk.bookings.get(booked["booking_ref"])["history"][-1]["event"],
                         "deposit 260.31 GBP (300.00 EUR) held on card")
        self.assertEqual(len(self.desk.payments.api.calls), 3)

    def test_capture_and_increment_use_the_guest_currency(self):
        booked = self.book("USD")
        ref = booked["booking_ref"]
        self.assertEqual(self.held(booked)["status"], "deposit_held")
        out = self.desk.payments.check_out(ref)
        api = self.desk.payments.api
        self.assertIsNone(api.intents[booked["payment_intent_id"]]["connected_account_id"])   # platform payments can't convert
        self.assertEqual(api.calls[-4:-2], [("increment", "580.15 USD"), ("capture", "580.15 USD")])
        # The hotel's 88%, in the card's currency: 80% of it now, 20% held while it is a new hotel.
        self.assertEqual([c[:2] for c in api.calls[-2:]], [("split", "408.42 USD"), ("reserve", "102.11 USD")])
        self.assertEqual(out["history"][-2]["event"], "hold raised to 580.15 USD (500.00 EUR) and captured")
        s = out["settlement"]
        self.assertEqual((s["captured"], s["commission"], s["hotel_payout"]), ("500.00", "60.00", "440.00"))
        self.assertEqual(s["charged"], {"currency": "USD", "captured": "580.15", "commission": "69.62",
                                        "hotel_payout": "510.53"})

    def test_late_cancellation_captures_guest_currency_deposit(self):
        booked = self.book("USD")
        self.held(booked)
        self.desk.payments.now = lambda: datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.assertEqual(self.desk.cancel_booking(booked["booking_ref"])["status"], "cancelled_fee_charged")
        self.assertEqual(self.desk.payments.api.calls[-3], ("capture", "348.09 USD"))

    def test_eur_or_no_currency_skips_conversion(self):
        for cur in (None, "EUR"):
            self.tearDown()
            self.setUp()
            booked = self.book(cur)
            self.assertNotIn("fx", booked)
            self.assertEqual(self.desk.payments.api.calls, [("create", "300.00 EUR")])

    def test_quote_failure_keeps_eur_deposit(self):
        booked = self.book("GBP", fails=True)
        self.assertEqual(booked["result"], "booked")
        self.assertIsNone(booked["fx"]["currency"])
        self.assertIn("300.00 EUR", booked["next_step"])
        self.held(booked)
        self.desk.payments.now = lambda: datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.desk.cancel_booking(booked["booking_ref"])
        self.assertEqual(self.desk.payments.api.calls[-3], ("capture", "300.00 EUR"))

    def test_invalid_currency_or_metal_is_refused(self):
        self.assertEqual(self.book("JPY")["result"], "not_booked")
        self.desk.payments.api = FakeAirwallexFX()
        self.assertIn("card deposits only", self.desk.book(self.offer(), True, "metal", currency="USD")["reason"])


if __name__ == "__main__":
    unittest.main()
