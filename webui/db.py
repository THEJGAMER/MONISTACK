"""SQLite-backed storage for everything Switchboard persists: devices added
through the UI, and saved command results. Replaces the earlier
JSON-file-per-store / one-.md-file-per-result approach with a single
database file on the same Docker volume, using only the standard library
(`sqlite3`) - no ORM, no new dependency.

One shared connection guarded by a lock: sqlite3 connections aren't safe
to use concurrently from multiple threads, and FastAPI's sync endpoints
(plus status_poller's background threads, which don't touch this at all)
can run on different worker threads. A single lock around every statement
is simple and correct at the request volumes this tool sees - this is an
internal ops console, not a high-throughput service.
"""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
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
    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()
