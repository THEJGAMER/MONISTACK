"""Postgres-backed storage for everything Switchboard persists: devices added
through the UI, and saved command results. Was SQLite on a Docker volume
until this app outgrew "one file, one process" (see ROADMAP.md Phase 2) -
this is the same storage layer, same interface, pointed at a real database.

One shared connection guarded by a lock: this is still an internal ops
console at low request volume, not a case for a connection pool. What's
different from the SQLite version is that a network database connection
can actually drop (a local file never does), so every method retries once
after reconnecting if it hits a dead connection.
"""
import threading

import psycopg2
import psycopg2.extras

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS results (
    filename TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    device_name TEXT NOT NULL,
    host TEXT NOT NULL,
    category_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command TEXT NOT NULL,
    summary TEXT,
    output TEXT NOT NULL,
    markdown TEXT NOT NULL,
    auto_saved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_device ON results(device_id);
CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at);
"""


class Database:
    def __init__(self, dsn):
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn = None
        self._connect()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(SCHEMA)
            self._conn.commit()

    def _connect(self):
        self._conn = psycopg2.connect(self._dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        self._conn.autocommit = False

    def _with_reconnect(self, fn):
        """Run `fn(cursor)` under the lock, retrying once after a fresh
        reconnect if the connection turned out to be dead - a network
        Postgres box can drop an idle connection in ways a local SQLite
        file never could."""
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    result = fn(cur)
                self._conn.commit()
                return result
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._connect()
                with self._conn.cursor() as cur:
                    result = fn(cur)
                self._conn.commit()
                return result

    def execute(self, sql, params=()):
        def run(cur):
            cur.execute(sql, params)
            return cur.rowcount

        rowcount = self._with_reconnect(run)
        return _ExecResult(rowcount)

    def query(self, sql, params=()):
        def run(cur):
            cur.execute(sql, params)
            return cur.fetchall()

        return self._with_reconnect(run)

    def query_one(self, sql, params=()):
        def run(cur):
            cur.execute(sql, params)
            return cur.fetchone()

        return self._with_reconnect(run)


class _ExecResult:
    """Mirrors the bit of sqlite3's cursor that callers actually use
    (`cur.rowcount`) without exposing a psycopg2 cursor that's already
    been closed by the time `execute()` returns."""

    def __init__(self, rowcount):
        self.rowcount = rowcount
