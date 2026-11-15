"""Where state is kept: files under var/ (the default) or Postgres when DATABASE_URL is set.

Render's free instances lose their disk on every deploy and every idle spin-down, so the hosted demo
keeps its state in Postgres (Supabase's transaction pooler works: no session state, no prepared
statements). Tables live in the `agentbank` schema, which Supabase's REST API does not expose, and
every row carries a namespace (STORAGE_NAMESPACE; "hosted" on Render, "local" elsewhere) so a laptop
never writes the hosted data.

Two shapes:
  doc(path)     one JSON value (the group-sales state, rule overrides), read whole and written whole
  stream(path)  an append-only list of JSON entries
Both are loaded once and written through, so reads stay local: this assumes one server instance,
which is how the demo runs.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

SCHEMA = "agentbank"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def namespace() -> str:
    ns = os.environ.get("STORAGE_NAMESPACE") or ("hosted" if os.environ.get("RENDER") else "local")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", ns):
        raise ValueError(f"STORAGE_NAMESPACE must be lowercase letters, digits and _: {ns!r}")
    return ns


class Postgres:
    """One shared autocommit connection, reconnected once if the pooler dropped it."""

    def __init__(self, url: str):
        self.url = url
        self._lock = threading.RLock()
        self._conn = None
        self.run(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        self.run(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.docs (ns TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,"
                 " updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (ns, name))")
        self.run(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.events (id BIGSERIAL PRIMARY KEY, ns TEXT NOT NULL,"
                 " stream TEXT NOT NULL, body TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now())")
        self.run(f"CREATE INDEX IF NOT EXISTS events_stream ON {SCHEMA}.events (ns, stream, id)")

    def _connect(self):
        import psycopg   # only needed when DATABASE_URL is set (pip install -e ".[postgres]")
        # prepare_threshold=None: transaction poolers (Supabase, PgBouncer) can't keep prepared statements.
        return psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=10)

    def run(self, sql: str, params=(), fetch: bool = False):
        import psycopg
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = self._connect()
                    cur = self._conn.execute(sql, params)
                    return cur.fetchall() if fetch else None
                except psycopg.OperationalError:
                    self._conn = None
                    if attempt == 2:
                        raise


_pg: Postgres | None = None
_pg_lock = threading.Lock()


def postgres() -> Postgres:
    global _pg
    with _pg_lock:
        if _pg is None or _pg.url != database_url():
            _pg = Postgres(database_url())
        return _pg


# ---- documents ------------------------------------------------------------------
class FileDoc:
    def __init__(self, path: Path):
        self.path = path

    def read(self, default=None):
        return json.loads(self.path.read_text()) if self.path.exists() else default

    def write(self, value) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
        tmp.replace(self.path)


class PgDoc:
    def __init__(self, pg: Postgres, name: str):
        self.pg, self.name, self.ns = pg, name, namespace()
        rows = pg.run(f"SELECT body FROM {SCHEMA}.docs WHERE ns=%s AND name=%s", (self.ns, name), fetch=True)
        self._text = rows[0][0] if rows else None

    def read(self, default=None):
        return json.loads(self._text) if self._text is not None else default   # a fresh copy every read

    def write(self, value) -> None:
        text = json.dumps(value, ensure_ascii=False)
        self.pg.run(f"INSERT INTO {SCHEMA}.docs (ns, name, body) VALUES (%s, %s, %s) ON CONFLICT (ns, name)"
                    " DO UPDATE SET body = EXCLUDED.body, updated_at = now()", (self.ns, self.name, text))
        self._text = text   # only once the database has it


def doc(path: str | Path):
    """A JSON document named after the file it would be (var/groupsales/state.json -> "state.json")."""
    path = Path(path)
    return PgDoc(postgres(), path.name) if database_url() else FileDoc(path)


# ---- append-only streams ----------------------------------------------------------
class FileStream:
    def __init__(self, path: Path):
        self.path = path

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]


class PgStream:
    def __init__(self, pg: Postgres, name: str):
        self.pg, self.name, self.ns = pg, name, namespace()
        self._lock = threading.Lock()
        self._lines = [r[0] for r in pg.run(f"SELECT body FROM {SCHEMA}.events WHERE ns=%s AND stream=%s ORDER BY id",
                                            (self.ns, name), fetch=True)]

    def append(self, entry: dict) -> None:
        line = json.dumps(entry, default=str)
        with self._lock:   # keeps the local copy in the database's order
            self.pg.run(f"INSERT INTO {SCHEMA}.events (ns, stream, body) VALUES (%s, %s, %s)", (self.ns, self.name, line))
            self._lines.append(line)

    def entries(self) -> list[dict]:
        with self._lock:
            lines = list(self._lines)
        return [json.loads(line) for line in lines]


def stream(path: str | Path):
    """An append-only stream named after its file (var/events.jsonl -> "events.jsonl")."""
    path = Path(path)
    return PgStream(postgres(), path.name) if database_url() else FileStream(path)
