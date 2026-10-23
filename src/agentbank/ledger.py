"""Duplicate protection for money movement.

Each business operation ("pay invoice INV-001", "convert for payroll week 43") gets one
stable key. The ledger hands out one request_id per key and records what happened, so:
- a retry of the same operation reuses its request_id (Airwallex dedupes it), and
- an operation already marked done, or whose outcome is unknown, is never re-sent blindly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

from .airwallex.errors import AirwallexError, AmbiguousResult


class OpStatus(str, Enum):
    STARTED = "started"      # request_id issued, call in progress or crashed mid-call
    SUCCEEDED = "succeeded"
    FAILED = "failed"        # Airwallex definitely rejected it; a new attempt needs a new key
    UNKNOWN = "unknown"      # timeout / 5xx: reconcile before doing anything else


@dataclass(frozen=True)
class Operation:
    key: str
    request_id: str
    status: OpStatus
    result: dict | None


class DuplicateOperation(Exception):
    def __init__(self, op: Operation):
        super().__init__(f"operation {op.key!r} is already {op.status.value}; refusing to resend")
        self.op = op


class OperationLedger:
    def __init__(self, path: str | Path = "var/ledger.sqlite3"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Tool calls arrive on worker threads (the MCP SDK runs sync tools off the event loop).
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._lock = threading.RLock()
        self._inflight: set[str] = set()   # keys being executed right now, in this process
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS ops (key TEXT PRIMARY KEY, request_id TEXT NOT NULL,"
            " status TEXT NOT NULL, result TEXT, updated REAL NOT NULL)"
        )

    def get(self, key: str) -> Operation | None:
        row = self.db.execute("SELECT key, request_id, status, result FROM ops WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return Operation(row[0], row[1], OpStatus(row[2]), json.loads(row[3]) if row[3] else None)

    def _set(self, key: str, status: OpStatus, result: dict | None = None) -> None:
        self.db.execute("UPDATE ops SET status=?, result=?, updated=? WHERE key=?",
                        (status.value, json.dumps(result) if result is not None else None, time.time(), key))

    def begin(self, key: str) -> Operation:
        """Claim a key. New keys get a fresh UUID; STARTED keys (a crashed earlier attempt)
        keep their request_id so the retry is deduped by Airwallex."""
        with self._lock:
            op = self.get(key)
            if op is not None and (op.status is not OpStatus.STARTED or key in self._inflight):
                raise DuplicateOperation(op)
            if op is None:
                op = Operation(key, str(uuid.uuid4()), OpStatus.STARTED, None)
                self.db.execute("INSERT INTO ops VALUES (?, ?, ?, NULL, ?)",
                                (key, op.request_id, OpStatus.STARTED.value, time.time()))
            self._inflight.add(key)
            return op

    def run(self, key: str, call: Callable[[str], dict]) -> dict:
        """Execute `call(request_id)` at most once per key."""
        op = self.begin(key)
        try:
            result = call(op.request_id)
        except AmbiguousResult:
            self._set(key, OpStatus.UNKNOWN)
            raise
        except AirwallexError as e:
            self._set(key, OpStatus.FAILED, {"status": e.status, "code": e.code, "message": e.message})
            raise
        else:
            self._set(key, OpStatus.SUCCEEDED, result)
        finally:
            with self._lock:
                self._inflight.discard(key)
        return result

    def resolve_unknown(self, key: str, found: dict | None) -> None:
        """After looking the object up by request_id: record it if it exists, otherwise
        reopen the key so the same request_id can be retried safely."""
        if found is not None:
            self._set(key, OpStatus.SUCCEEDED, found)
        else:
            self._set(key, OpStatus.STARTED)

    def unresolved(self) -> list[Operation]:
        rows = self.db.execute("SELECT key FROM ops WHERE status IN (?, ?)",
                               (OpStatus.STARTED.value, OpStatus.UNKNOWN.value)).fetchall()
        return [self.get(r[0]) for r in rows]
