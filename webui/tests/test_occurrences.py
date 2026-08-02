"""Tests for OccurrenceStore, focused on the paging bookkeeping in close() -
the fix for a real bug reported live: a resolved alarm kept showing "paging
now..." in the UI because closing an occurrence never cleared its stale,
already-past page_at.

Uses a minimal in-memory fake of the Database interface, same approach as
test_alert_rules.py - OccurrenceStore's SQL is simple enough that faking
the three-method Database contract is more honest than mocking the store.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from occurrences import OccurrenceStore


class _FakeDB:
    def __init__(self):
        self.rows = {}
        self._next_id = 1

    def query_one(self, sql, params=()):
        if "SELECT * FROM alert_occurrences WHERE signature = %s AND resolved_at IS NULL" in sql:
            for r in self.rows.values():
                if r["signature"] == params[0] and r["resolved_at"] is None:
                    return dict(r)
            return None
        if "SELECT * FROM alert_occurrences WHERE id = %s" in sql:
            r = self.rows.get(params[0])
            return dict(r) if r else None
        raise AssertionError(f"unexpected query_one: {sql}")

    def query(self, sql, params=()):
        if "WHERE resolved_at IS NOT NULL AND page_at IS NOT NULL" in sql:
            return [dict(r) for r in self.rows.values() if r["resolved_at"] and r["page_at"]]
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO alert_occurrences"):
            signature, alertname, severity, summary, labels, started_at = params
            row_id = self._next_id
            self._next_id += 1
            self.rows[row_id] = {
                "id": row_id, "signature": signature, "alertname": alertname,
                "severity": severity, "summary": summary, "labels": labels,
                "started_at": started_at, "resolved_at": None,
                "page_at": None, "paged_at": None, "paging_disabled": 0, "silence_id": None,
            }
            return _Result(1)
        if sql.startswith("UPDATE alert_occurrences SET resolved_at = %s, page_at = NULL, paged_at = %s"):
            resolved_at, paged_at, row_id = params
            self.rows[row_id].update(resolved_at=resolved_at, page_at=None, paged_at=paged_at)
            return _Result(1)
        if sql.startswith("UPDATE alert_occurrences SET page_at = %s, silence_id = %s"):
            page_at, silence_id, row_id = params
            self.rows[row_id].update(page_at=page_at, silence_id=silence_id)
            return _Result(1)
        if sql.startswith("UPDATE alert_occurrences SET paged_at = %s, page_at = NULL, silence_id = NULL"):
            paged_at, row_id = params
            self.rows[row_id].update(paged_at=paged_at, page_at=None, silence_id=None)
            return _Result(1)
        if sql.startswith("UPDATE alert_occurrences SET page_at = NULL, paged_at = %s WHERE id"):
            paged_at, row_id = params
            self.rows[row_id].update(page_at=None, paged_at=paged_at)
            return _Result(1)
        raise AssertionError(f"unexpected execute: {sql}")


class _Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


@pytest.fixture
def store():
    return OccurrenceStore(_FakeDB())


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
    row = store.db.rows[occurrence["id"]]
    row.update(page_at=page_at, resolved_at=resolved_at, paging_disabled=paging_disabled)
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
