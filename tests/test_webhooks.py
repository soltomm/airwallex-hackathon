"""Airwallex webhooks: signature on raw bytes, timestamp window, dedupe, and API-read-as-truth."""
import hashlib
import hmac
import json
import time
import unittest

import test_offer_desk as base

from agentbank.webhooks import WebhookInbox, signature_ok

SECRET = "whsec_test"


def signed(event: dict, ts: int | None = None, secret: str = SECRET) -> tuple[dict, bytes]:
    raw = json.dumps(event, separators=(",", ":")).encode()
    ts = str(ts if ts is not None else int(time.time() * 1000))
    sig = hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest()
    return {"X-Timestamp": ts, "X-Signature": sig}, raw


class WebhookTests(unittest.TestCase):
    setUp = base.OfferDeskTests.setUp
    tearDown = base.OfferDeskTests.tearDown
    negotiate = base.OfferDeskTests.negotiate
    offer = base.OfferDeskTests.offer

    def test_signature_is_checked_on_the_raw_bytes_within_five_minutes(self):
        headers, raw = signed({"id": "evt_1"})
        ts, sig = headers["X-Timestamp"], headers["X-Signature"]
        self.assertTrue(signature_ok(SECRET, ts, raw, sig))
        self.assertFalse(signature_ok(SECRET, ts, raw + b" ", sig))                       # re-serialised body
        self.assertFalse(signature_ok("other", ts, raw, sig))
        self.assertFalse(signature_ok(SECRET, ts, raw, sig, now_ms=int(ts) + 6 * 60 * 1000))   # replayed later
        self.assertFalse(signature_ok("", ts, raw, sig))

    def test_event_refreshes_the_booking_from_the_api_once(self):
        booked = self.desk.book(self.offer(), True, "card")
        ref, pid = booked["booking_ref"], booked["payment_intent_id"]
        handled = []
        inbox = WebhookInbox(SECRET, lambda e: handled.append(self.desk.on_airwallex_event(e)), background=False)
        event = {"id": "evt_9", "name": "payment_intent.requires_capture", "data": {"object": {"id": pid}}}
        # The event says the hold is in place, but Airwallex doesn't yet: nothing moves.
        self.assertEqual(inbox.receive(*signed(event))[0], 200)
        self.assertEqual(self.desk.bookings.get(ref)["status"], "awaiting_payment")
        self.desk.payments.api.intents[pid]["status"] = "REQUIRES_CAPTURE"
        event2 = {**event, "id": "evt_10"}
        self.assertEqual(inbox.receive(*signed(event2)), (200, {"received": True}))
        self.assertEqual(self.desk.bookings.get(ref)["status"], "deposit_held")
        self.assertEqual(inbox.receive(*signed(event2)), (200, {"duplicate": True}))   # at-least-once delivery
        self.assertEqual(handled, [ref, ref])

    def test_bad_signature_or_body_is_refused(self):
        inbox = WebhookInbox(SECRET, lambda e: self.fail("must not run"))
        headers, raw = signed({"id": "evt_1"}, secret="wrong")
        self.assertEqual(inbox.receive(headers, raw)[0], 401)
        headers, raw = signed({"no": "id"})
        self.assertEqual(inbox.receive(headers, raw)[0], 400)


if __name__ == "__main__":
    unittest.main()
