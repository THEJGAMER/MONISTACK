"""Tests for command history and favourites (ROADMAP Phase 4).

Run against a **real Postgres** in a throwaway schema rather than a fake
DB object, because the behaviour worth pinning here lives in the SQL
itself, not in the Python around it: `DISTINCT ON` for deduplicated
recent commands, and an `ON CONFLICT ... COALESCE(...)` unique index that
exists specifically to stop NULL device_id/params from duplicating
freely (NULL never equals NULL, so a naive unique index silently allows
exactly the duplicates it was added to prevent). A hand-written fake
would happily "pass" both of those while the real database rejected or
duplicated them.

Skipped when no Postgres is reachable - the same skipif discipline
syslog/tests/test_vrl.py already uses for its `vector` binary dependency.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

psycopg2 = pytest.importorskip("psycopg2")

from command_history import (  # noqa: E402
    STATUS_ERROR,
    STATUS_OK,
    CommandFavoritesStore,
    CommandHistoryStore,
)

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")

# The two tables under test, copied from common/db.py's SCHEMA. Kept
# verbatim rather than importing and running the whole SCHEMA so a test
# schema doesn't need every unrelated table (and their FKs) to exist.
DDL = """
CREATE TABLE command_history (
    id BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_name TEXT,
    category_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command TEXT NOT NULL,
    params TEXT,
    status TEXT NOT NULL,
    error TEXT,
    duration_ms INTEGER,
    result_filename TEXT,
    source TEXT NOT NULL DEFAULT 'console'
);
CREATE TABLE command_favorites (
    id BIGSERIAL PRIMARY KEY,
    actor TEXT NOT NULL,
    label TEXT,
    device_id TEXT,
    category_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    params TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_command_favorites_unique
    ON command_favorites(actor, COALESCE(device_id, ''), category_id, command_id, COALESCE(params, ''));
"""


def _reachable():
    try:
        psycopg2.connect(DSN, connect_timeout=4).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no Postgres reachable for integration test")


class _DB:
    """The three methods the stores actually call, over a real connection
    pinned to a private schema."""

    def __init__(self, conn):
        self.conn = conn

    def _cur(self):
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        cur = self._cur()
        cur.execute(sql, params)
        return cur

    def query(self, sql, params=()):
        cur = self._cur()
        cur.execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql, params=()):
        cur = self._cur()
        cur.execute(sql, params)
        return cur.fetchone()


@pytest.fixture
def db():
    import psycopg2.extras  # noqa: F401  (registers RealDictCursor)

    schema = f"test_cmdhist_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(DDL)
    try:
        yield _DB(conn)
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


# --- history ---

def test_record_and_list_round_trip(db):
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "Switch One", "system", "version", "show version",
             duration_ms=42, result_filename="f.md")

    items, total = h.list(actor="alice")

    assert total == 1
    assert items[0]["command"] == "show version"
    assert items[0]["status"] == STATUS_OK
    assert items[0]["duration_ms"] == 42
    assert items[0]["result_filename"] == "f.md"


def test_history_is_scoped_to_its_own_actor(db):
    """Personal history must not leak between users at the *store* level,
    not merely be filtered in the UI."""
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "system", "version", "show version")
    h.record("bob", "sw1", "SW1", "system", "version", "show version")

    alice_items, alice_total = h.list(actor="alice")

    assert alice_total == 1
    assert all(i["actor"] == "alice" for i in alice_items)
    assert h.list(actor=None)[1] == 2  # admin fleet-wide view still sees both


def test_failed_runs_are_recorded_too(db):
    """"I ran it and it broke" is the most useful thing to find again -
    a history that only kept successes would quietly misrepresent what
    was actually attempted."""
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "system", "version", "show version",
             status=STATUS_ERROR, error="SSH timeout")

    items, _ = h.list(actor="alice", status=STATUS_ERROR)

    assert len(items) == 1
    assert items[0]["error"] == "SSH timeout"


def test_record_never_raises_on_a_bad_write(db):
    """A history-write failure must not be able to fail the command that
    was actually run (same contract as audit.py)."""
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "system", "version", "show version",
             duration_ms="not-an-integer")  # wrong type for an INTEGER column

    assert h.list(actor="alice")[1] == 0  # nothing stored, but no exception


def test_params_round_trip_as_structured_data(db):
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "interfaces", "transceiver",
             "show interfaces Te 1/37 transceiver", params={"port": "Te 1/37"})

    items, _ = h.list(actor="alice")

    assert items[0]["params"] == {"port": "Te 1/37"}


def test_recent_commands_deduplicates_repeats(db):
    """Hammering Run on one command must not crowd everything else out of
    a short "recent" list - this is what DISTINCT ON is for."""
    h = CommandHistoryStore(db)
    for _ in range(5):
        h.record("alice", "sw1", "SW1", "system", "version", "show version")
    h.record("alice", "sw1", "SW1", "system", "uptime", "show uptime")

    recent = h.recent_commands("alice", limit=10)

    assert len(recent) == 2
    assert {r["command_id"] for r in recent} == {"version", "uptime"}


def test_recent_commands_treats_different_params_as_different_entries(db):
    """Same command, different port, is genuinely a different thing to
    re-run - collapsing them would make the shortcut list useless for
    parameterised commands."""
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "interfaces", "transceiver", "show ... Te 1/1",
             params={"port": "Te 1/1"})
    h.record("alice", "sw1", "SW1", "interfaces", "transceiver", "show ... Te 1/2",
             params={"port": "Te 1/2"})

    assert len(h.recent_commands("alice")) == 2


def test_search_and_device_filter(db):
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "system", "version", "show version")
    h.record("alice", "sw2", "SW2", "interfaces", "status", "show interfaces status")

    assert h.list(actor="alice", device_id="sw2")[1] == 1
    assert h.list(actor="alice", q="interfaces")[1] == 1
    assert h.list(actor="alice", q="nothing-matches")[1] == 0


def test_clear_only_removes_the_callers_own_rows(db):
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "system", "version", "show version")
    h.record("bob", "sw1", "SW1", "system", "version", "show version")

    h.clear("alice")

    assert h.list(actor="alice")[1] == 0
    assert h.list(actor="bob")[1] == 1


def test_limit_is_bounded(db):
    """A hand-crafted ?limit=1000000 must not turn into a full-table
    materialisation."""
    h = CommandHistoryStore(db)
    h.record("alice", "sw1", "SW1", "system", "version", "show version")

    items, _ = h.list(actor="alice", limit=10_000_000)

    assert len(items) <= 500


# --- favourites ---

def test_add_and_list_favorite(db):
    f = CommandFavoritesStore(db)
    fav = f.add("alice", "system", "version", device_id="sw1", label="Version check")

    assert fav["command_id"] == "version"
    assert fav["label"] == "Version check"
    assert len(f.list("alice")) == 1


def test_favoriting_twice_is_idempotent(db):
    """The UI's star is a toggle a user can double-click; a duplicate must
    be a no-op, not an error or a second row."""
    f = CommandFavoritesStore(db)
    first = f.add("alice", "system", "version", device_id="sw1")
    again = f.add("alice", "system", "version", device_id="sw1")

    assert len(f.list("alice")) == 1
    assert first["id"] == again["id"]


def test_any_device_favorites_do_not_duplicate(db):
    """The COALESCE in the unique index exists exactly for this: with a
    plain unique index, NULL != NULL means every "any device" favourite
    would insert a fresh duplicate row forever."""
    f = CommandFavoritesStore(db)
    f.add("alice", "system", "version")
    f.add("alice", "system", "version")

    assert len(f.list("alice")) == 1


def test_same_command_different_params_are_separate_favorites(db):
    f = CommandFavoritesStore(db)
    f.add("alice", "interfaces", "transceiver", device_id="sw1", params={"port": "Te 1/1"})
    f.add("alice", "interfaces", "transceiver", device_id="sw1", params={"port": "Te 1/2"})

    assert len(f.list("alice")) == 2


def test_favorites_are_per_user(db):
    f = CommandFavoritesStore(db)
    f.add("alice", "system", "version")
    f.add("bob", "system", "version")

    assert len(f.list("alice")) == 1
    assert len(f.list("bob")) == 1


def test_delete_cannot_remove_another_users_favorite(db):
    """Scoping the DELETE by actor in SQL, not just checking it in the
    route - otherwise any authenticated user could unpin someone else's
    favourite by guessing an id."""
    f = CommandFavoritesStore(db)
    bobs = f.add("bob", "system", "version", device_id="sw1")

    f.delete("alice", bobs["id"])

    assert len(f.list("bob")) == 1, "alice must not be able to delete bob's favourite"

    f.delete("bob", bobs["id"])
    assert len(f.list("bob")) == 0
