"""Tests for evidence-based occurrence closing (touch / stale_open).

Regression cover for a real production bug: occurrence closing was
absence-based - "anything open that I can't currently see firing is over".
That is only correct when this process has a complete view of what's
active, and it silently doesn't in two cases:

1. Two Switchboard instances sharing one database. Found live via
   pg_stat_activity (two distinct client_addr, the second connected
   continuously since 2026-08-02). Each reconciles against its *own*
   Alertmanager, so each kept closing occurrences the other had just
   opened, and the other immediately reopened them. One continuously-down
   device produced ~19,800 occurrence rows instead of one.
2. A single instance where Alertmanager or Prometheus blips for one tick -
   the local view goes empty and every open alarm closes spuriously.

Run against a real Postgres in a throwaway schema, because what's being
pinned is the SQL: the `last_seen_at IS NULL OR last_seen_at < cutoff`
predicate and the partial unique index that keeps one open row per
signature. Skipped when no database is reachable.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from occurrences import OccurrenceStore  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")

DDL = """
CREATE TABLE alert_occurrences (
    id BIGSERIAL PRIMARY KEY,
    signature TEXT NOT NULL,
    alertname TEXT NOT NULL,
    severity TEXT,
    summary TEXT,
    labels TEXT NOT NULL,
    started_at TEXT NOT NULL,
    resolved_at TEXT,
    page_at TEXT,
    paged_at TEXT,
    paging_disabled INTEGER NOT NULL DEFAULT 0,
    silence_id TEXT,
    last_seen_at TEXT
);
CREATE UNIQUE INDEX idx_occurrences_one_open
    ON alert_occurrences(signature) WHERE resolved_at IS NULL;
"""


def _reachable():
    try:
        psycopg2.connect(DSN, connect_timeout=4).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no Postgres reachable for integration test")


class _DB:
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
def store():
    schema = f"test_occ_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(DDL)
    try:
        yield OccurrenceStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _open(store, signature="sig1"):
    return store.open(signature, "TestAlarm", "critical", "summary", {"alertname": "TestAlarm"})


def test_a_freshly_touched_occurrence_is_not_stale(store):
    """The core of the fix: an alarm somebody has just seen active must
    not be closable, no matter whose view saw it."""
    _open(store)
    store.touch("sig1")

    assert store.stale_open(grace_seconds=90) == []


def test_an_untouched_occurrence_becomes_stale_after_the_grace_period(store):
    """The other half - a genuinely resolved alarm must still close, or
    the fix would trade spurious closes for alarms that never end."""
    _open(store)
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    store.touch("sig1", seen_at=old)

    stale = store.stale_open(grace_seconds=90)

    assert [s["signature"] for s in stale] == ["sig1"]


def test_never_touched_occurrence_counts_as_stale(store):
    """Rows predating last_seen_at, or opened by an instance that then
    died, must remain closable rather than becoming immortal."""
    _open(store)

    assert [s["signature"] for s in store.stale_open(grace_seconds=90)] == ["sig1"]


def test_touch_does_not_resurrect_a_closed_occurrence(store):
    """touch() is scoped to open rows - otherwise a late notification for
    an already-resolved alarm would silently reopen a closed record."""
    _open(store)
    store.close("sig1")
    store.touch("sig1")

    assert store.open_for("sig1") is None


def test_the_two_instance_ping_pong_no_longer_closes_a_live_alarm(store):
    """End-to-end reproduction of the production bug, as two instances
    against one database: instance A can see the alarm firing, instance B
    cannot (different Alertmanager). B must not close what A is still
    reporting active - which is exactly what produced ~19,800 rows."""
    a = store
    b = OccurrenceStore(store.db)  # same database, independent instance

    occurrence = _open(a)
    a.touch("sig1")  # A sees it firing this tick

    # B's own reconcile pass: it sees nothing firing, so it closes whatever
    # it considers stale. With evidence-based closing, that's nothing.
    for stale in b.stale_open(grace_seconds=90):
        b.close(stale["signature"])

    still_open = a.open_for("sig1")
    assert still_open is not None, "a live alarm was closed by an instance that couldn't see it"
    assert still_open["id"] == occurrence["id"], "the occurrence identity must survive the other instance's pass"


def test_a_momentary_blank_view_does_not_close_a_live_alarm(store):
    """Same failure mode without a second instance: Alertmanager returns
    nothing for one tick. The alarm is still real and must survive until
    the grace period genuinely lapses."""
    _open(store)
    store.touch("sig1")

    for _ in range(5):  # five consecutive empty ticks, well inside the grace window
        for stale in store.stale_open(grace_seconds=90):
            store.close(stale["signature"])

    assert store.open_for("sig1") is not None


def test_reopening_after_a_real_close_creates_a_distinct_occurrence(store):
    """The episode model must still hold: a genuine resolve followed by a
    genuine re-fire is two occurrences, not one reopened row."""
    first = _open(store)
    store.close("sig1")
    second = _open(store)

    assert second["id"] != first["id"]
    assert store.open_for("sig1")["id"] == second["id"]
