"""Tests for OccurrenceStore, focused on the paging bookkeeping in close() -
the fix for a real bug reported live: a resolved alarm kept showing "paging
now..." in the UI because closing an occurrence never cleared its stale,
already-past page_at.

Against a real Postgres in a throwaway schema (as test_occurrence_events.py
and test_occurrence_close_grace.py are): the store's transitions are now
exactly-once *in SQL* - INSERT ... RETURNING under the partial unique
index, UPDATE ... FROM ... FOR UPDATE, UPDATE ... WHERE resolved_at IS NULL -
and a string-matching fake of the Database would only pin the fake.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from occurrences import OccurrenceStore  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")
DDL = """
CREATE TABLE alert_occurrences (
    id BIGSERIAL PRIMARY KEY, signature TEXT NOT NULL, alertname TEXT NOT NULL, severity TEXT, summary TEXT,
    labels TEXT NOT NULL, started_at TEXT NOT NULL, resolved_at TEXT, page_at TEXT, paged_at TEXT,
    paging_disabled INTEGER NOT NULL DEFAULT 0, silence_id TEXT, last_seen_at TEXT,
    detected_via TEXT, signal_at TEXT);
CREATE UNIQUE INDEX idx_occurrences_one_open ON alert_occurrences(signature) WHERE resolved_at IS NULL;
"""


class _DB:
    def __init__(self, conn):
        self.conn = conn

    def _cur(self):
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur

    def query(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchall()

    def query_one(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchone()


@pytest.fixture
def store():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=4)
    except Exception:
        pytest.skip("test Postgres not reachable")
    conn.autocommit = True
    schema = f"test_occ_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield OccurrenceStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _open(store, signature="sig1", started_at="2026-08-01T10:00:00+00:00"):
    return store.open(signature, "TestAlarm", "critical", "test summary", {"alertname": "TestAlarm"}, started_at)


def test_close_clears_a_lapsed_page_at_and_backfills_paged_at(store):
    """The exact bug: an occurrence whose hold had already lapsed (page_at
    in the past) by the time it resolved must end up looking "paged", not
    stuck showing a live countdown to a moment that's already gone."""
    occurrence = _open(store)
    store.set_paging(occurrence["id"], "2026-08-01T10:02:00+00:00", "silence-abc")

    closed = store.close("sig1", resolved_at="2026-08-01T10:05:00+00:00")  # after page_at

    assert closed["page_at"] is None
    assert closed["paged_at"] == "2026-08-01T10:02:00+00:00"


def test_close_inside_the_hold_never_marks_paged(store):
    """Recovered *before* page_at - the hold did its job, this alarm never
    paged, and the record must say so cleanly rather than showing a
    countdown to a page that will now never happen."""
    occurrence = _open(store)
    store.set_paging(occurrence["id"], "2026-08-01T10:10:00+00:00", "silence-abc")

    closed = store.close("sig1", resolved_at="2026-08-01T10:05:00+00:00")  # before page_at

    assert closed["page_at"] is None
    assert closed["paged_at"] is None


def test_close_with_no_hold_ever_placed_stays_unpaged(store):
    """A rule with page delay 0 (or paging disabled) has no page_at at
    all - closing it must not invent a paged_at out of nothing."""
    _open(store)
    closed = store.close("sig1")
    assert closed["page_at"] is None
    assert closed["paged_at"] is None


def test_close_does_not_overwrite_an_already_recorded_page(store):
    """If the due-to-page loop already marked this paged before it
    resolved, closing it must not clobber that with a recomputed value."""
    occurrence = _open(store)
    store.set_paging(occurrence["id"], "2026-08-01T10:02:00+00:00", "silence-abc")
    store.mark_paged(occurrence["id"], when="2026-08-01T10:02:03+00:00")

    closed = store.close("sig1", resolved_at="2026-08-01T10:05:00+00:00")

    assert closed["paged_at"] == "2026-08-01T10:02:03+00:00"


def test_close_on_nothing_open_is_a_noop(store):
    assert store.close("no-such-signature") is None


def _make_broken_row(store, page_at, resolved_at, paging_disabled=0):
    """Simulates data written before close() cleared page_at on resolve -
    exactly the shape reported live (ALM-108/ALM-59: resolved, but still
    showing a live "paging now..." countdown). Pokes the row directly
    rather than going through close(), since close() no longer produces
    this broken shape - that's the whole point of the fix."""
    occurrence = _open(store, signature=f"broken-{page_at}")
    store.db.execute("UPDATE alert_occurrences SET page_at = %s, resolved_at = %s, paging_disabled = %s WHERE id = %s",
                     (page_at, resolved_at, paging_disabled, occurrence["id"]))
    return occurrence["id"]


def test_repair_backfills_paged_at_for_a_lapsed_stale_page_at(store):
    row_id = _make_broken_row(store, "2026-08-01T10:02:00+00:00", "2026-08-01T10:05:00+00:00")

    repaired = store.repair_stale_paging_on_resolved()

    assert repaired == 1
    fixed = store.get(row_id)
    assert fixed["page_at"] is None
    assert fixed["paged_at"] == "2026-08-01T10:02:00+00:00"


def test_repair_leaves_never_paged_alone_if_hold_had_not_lapsed(store):
    row_id = _make_broken_row(store, "2026-08-01T10:10:00+00:00", "2026-08-01T10:05:00+00:00")

    store.repair_stale_paging_on_resolved()

    fixed = store.get(row_id)
    assert fixed["page_at"] is None
    assert fixed["paged_at"] is None


def test_repair_is_a_noop_on_clean_data(store):
    _open(store)  # open, unresolved, no page_at - nothing to repair
    assert store.repair_stale_paging_on_resolved() == 0
