"""Storage backends: files by default, Postgres when DATABASE_URL is set (storage.py).

The Postgres tests run only when TEST_DATABASE_URL points at a database they may write to, e.g.
    docker run -d --rm -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:54329/postgres python -m unittest tests.test_storage
Each test writes under its own namespace. Run: python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank import storage
from agentbank.audit import AuditLog
from agentbank.bookings import BookingStore
from agentbank.hotel_onboarding import HotelOnboarding
from agentbank.insights import DemandLog
from agentbank.ledger import DuplicateOperation, OperationLedger, OpStatus
from agentbank.offers import load_hotels
from agentbank.webhooks import WebhookInbox

PG = os.environ.get("TEST_DATABASE_URL", "")
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


class StoreContract:
    """What every store must do on either backend: survive being reopened by a fresh process."""

    def test_documents_survive_a_restart_and_reads_are_copies(self):
        a = BookingStore(self.dir / "bookings.json")
        a.put({"booking_ref": "BKG-1", "status": "awaiting_payment"})
        a.update("BKG-1", "paid", status="paid")
        a.get("BKG-1")["status"] = "mutated by a caller"   # must not leak into the store
        b = BookingStore(self.dir / "bookings.json")
        self.assertEqual(b.get("BKG-1")["status"], "paid")
        self.assertEqual(b.get("BKG-1")["history"][-1]["event"], "paid")

    def test_streams_keep_order_across_restarts(self):
        log = DemandLog(self.dir / "demand.jsonl")
        for i in range(3):
            log.record("search", NOW, n=i)
        AuditLog(self.dir / "audit.jsonl").record("x", client_secret="s3cret")
        self.assertEqual([e["n"] for e in DemandLog(self.dir / "demand.jsonl").entries()], [0, 1, 2])
        self.assertEqual(AuditLog(self.dir / "audit.jsonl").entries()[0]["client_secret"], "***")

    def test_ledger_never_resends_after_a_restart(self):
        led = OperationLedger(self.dir / "ledger.sqlite3")
        self.assertEqual(led.run("booking:ofr_1", lambda rid: {"ref": rid})["ref"], led.get("booking:ofr_1").request_id)
        started = led.begin("payout:BKG-1")   # crashes mid-call
        again = OperationLedger(self.dir / "ledger.sqlite3")
        self.assertIs(again.get("booking:ofr_1").status, OpStatus.SUCCEEDED)
        with self.assertRaises(DuplicateOperation):
            again.run("booking:ofr_1", lambda rid: {})
        self.assertEqual(again.begin("payout:BKG-1").request_id, started.request_id)   # same id: Airwallex dedupes
        self.assertEqual([o.key for o in again.unresolved()], ["payout:BKG-1"])

    def test_sign_ups_and_webhook_dedupe_survive_a_restart(self):
        hotels = load_hotels(ROOT / "data/hotels.json")
        HotelOnboarding(hotels, self.dir / "hotels_onboarded.json", now=lambda: NOW).join({
            "name": "Casa Lumbre", "city": "Barcelona", "email": "a@b.example", "style": "budget",
            "breakfast_price": "12", "late_checkout_price": "10",
            "rooms": [{"name": "Double", "max_guests": "2", "total_rooms": "5", "published_rate": "100",
                       "floor_rate": "80"}]})
        fresh = load_hotels(ROOT / "data/hotels.json")
        HotelOnboarding(fresh, self.dir / "hotels_onboarded.json")
        self.assertEqual(fresh["htl_barcelona_casa_lumbre"].city, "Barcelona")
        inbox = WebhookInbox("k", lambda e: None, self.dir / "webhooks.jsonl", background=False)
        inbox.stream.append({"id": "evt_1", "name": "payment_intent.succeeded", "at": 0})
        self.assertIn("evt_1", WebhookInbox("k", lambda e: None, self.dir / "webhooks.jsonl").seen)


class FileStorageTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"DATABASE_URL": ""})
        patcher.start()
        self.addCleanup(patcher.stop)


@unittest.skipUnless(PG, "set TEST_DATABASE_URL to run the Postgres backend tests")
class PostgresStorageTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())   # must stay empty: nothing may be written to files
        ns = "t" + uuid.uuid4().hex[:12]
        patcher = mock.patch.dict(os.environ, {"DATABASE_URL": PG, "STORAGE_NAMESPACE": ns})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._drop, ns)

    def tearDown(self):
        self.assertEqual(list(self.dir.iterdir()), [], "a store wrote to disk instead of Postgres")

    def _drop(self, ns):
        pg = storage.postgres()
        pg.run(f"DELETE FROM {storage.SCHEMA}.docs WHERE ns=%s", (ns,))
        pg.run(f"DELETE FROM {storage.SCHEMA}.events WHERE ns=%s", (ns,))
        pg.run(f"DROP TABLE IF EXISTS {storage.SCHEMA}.ops_{ns}")

    def test_namespaces_do_not_see_each_other(self):
        BookingStore(self.dir / "bookings.json").put({"booking_ref": "BKG-1"})
        with mock.patch.dict(os.environ, {"STORAGE_NAMESPACE": "other_" + os.environ["STORAGE_NAMESPACE"]}):
            self.assertEqual(BookingStore(self.dir / "bookings.json").all(), {})

    def test_reconnects_after_the_pooler_drops_the_connection(self):
        log = DemandLog(self.dir / "demand.jsonl")
        log.record("search", NOW)
        storage.postgres()._conn.close()
        log.record("search", NOW)
        self.assertEqual(len(DemandLog(self.dir / "demand.jsonl").entries()), 2)

    def test_tables_stay_out_of_the_public_schema(self):
        rows = storage.postgres().run("SELECT table_schema FROM information_schema.tables WHERE table_name IN "
                                      "('docs', 'events')", fetch=True)
        self.assertEqual({r[0] for r in rows}, {storage.SCHEMA})


class NamespaceTests(unittest.TestCase):
    def test_hosted_and_local_are_separate_and_names_are_checked(self):
        with mock.patch.dict(os.environ, {"STORAGE_NAMESPACE": "", "RENDER": "true"}):
            self.assertEqual(storage.namespace(), "hosted")
        with mock.patch.dict(os.environ, {"STORAGE_NAMESPACE": "", "RENDER": ""}):
            self.assertEqual(storage.namespace(), "local")
        with mock.patch.dict(os.environ, {"STORAGE_NAMESPACE": "x; DROP TABLE"}):
            with self.assertRaises(ValueError):
                storage.namespace()


if __name__ == "__main__":
    unittest.main()
