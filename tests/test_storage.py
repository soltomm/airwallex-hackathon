"""Storage backends: files by default, Postgres when DATABASE_URL is set (storage.py).

The Postgres tests run only when TEST_DATABASE_URL points at a database they may write to, e.g.
    docker run -d --rm -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:54329/postgres python -m unittest tests.test_storage
Each test writes under its own namespace."""
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbank import storage

PG = os.environ.get("TEST_DATABASE_URL", "")


class StoreContract:
    """What every store must do on either backend: survive being reopened by a fresh process."""

    def test_documents_survive_a_restart_and_reads_are_copies(self):
        a = storage.doc(self.dir / "state.json")
        self.assertEqual(a.read({"deals": []}), {"deals": []})
        a.write({"deals": [{"ref": "GRP-1", "status": "quoted"}]})
        a.read()["deals"][0]["status"] = "mutated by a caller"   # must not leak into the store
        b = storage.doc(self.dir / "state.json")
        self.assertEqual(b.read()["deals"][0]["status"], "quoted")

    def test_streams_keep_order_across_restarts(self):
        log = storage.stream(self.dir / "events.jsonl")
        for i in range(3):
            log.append({"n": i})
        self.assertEqual([e["n"] for e in storage.stream(self.dir / "events.jsonl").entries()], [0, 1, 2])

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

    def test_namespaces_do_not_see_each_other(self):
        storage.doc(self.dir / "state.json").write({"deals": ["GRP-1"]})
        with mock.patch.dict(os.environ, {"STORAGE_NAMESPACE": "other_" + os.environ["STORAGE_NAMESPACE"]}):
            self.assertIsNone(storage.doc(self.dir / "state.json").read())

    def test_reconnects_after_the_pooler_drops_the_connection(self):
        log = storage.stream(self.dir / "events.jsonl")
        log.append({"n": 0})
        storage.postgres()._conn.close()
        log.append({"n": 1})
        self.assertEqual(len(storage.stream(self.dir / "events.jsonl").entries()), 2)

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
