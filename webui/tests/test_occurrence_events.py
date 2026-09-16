"""Alarm lifecycle events come from the occurrence store's transitions.

Confirmed live (occurrence 52623, and by reading the paths): alarm.opened
was emitted only by the Alertmanager webhook, but the 3s sync tick opens
nearly every real occurrence first - so real alarms produced no event, no
page and no webhook call; and the stale sweep closed occurrences without
ever emitting alarm.resolved, so the pager ledger was left behind. The
store now fires one hook per transition - opened, paged, closed - whatever
path drove it, and exactly once even when two paths race.

Real Postgres per test: the exactly-once guarantees are SQL (INSERT ...
RETURNING under the partial unique index, UPDATE ... FROM ... FOR UPDATE
returning the previous paged_at, UPDATE ... WHERE resolved_at IS NULL).
"""
import os
import sys
import uuid
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
    schema = f"test_oe_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield OccurrenceStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


class _Recorder:
    def __init__(self, store):
        self.events = []
        store.on_opened = lambda occ: self.events.append(("opened", occ["id"]))
        store.on_paged = lambda occ: self.events.append(("paged", occ["id"]))
        store.on_closed = lambda occ, by: self.events.append(("closed", occ["id"], by))


def _open(store, sig="sig-1"):
    return store.open(sig, "FanFailure", "critical", "Fan tray 2 down", {"alertname": "FanFailure", "device": "s4048"})


# --- opened -----------------------------------------------------------

def test_opening_fires_once_however_many_paths_report_the_same_alarm(store):
    r = _Recorder(store)
    a = _open(store)          # the sync tick sees it
    b = _open(store)          # then the webhook arrives for the same episode

    assert a["id"] == b["id"]
    assert r.events == [("opened", a["id"])]


def test_a_new_episode_after_a_close_is_a_new_opened(store):
    r = _Recorder(store)
    a = _open(store)
    store.close("sig-1")
    b = _open(store)

    assert b["id"] != a["id"]
    assert [e for e in r.events if e[0] == "opened"] == [("opened", a["id"]), ("opened", b["id"])]


# --- paged ------------------------------------------------------------

def test_paging_fires_once_even_when_two_paths_mark_it(store):
    """The sync tick and the webhook both call mark_paged for a fresh,
    unheld alarm within the same instant."""
    r = _Recorder(store)
    occ = _open(store)
    store.mark_paged(occ["id"])
    store.mark_paged(occ["id"])

    assert r.events.count(("paged", occ["id"])) == 1
    assert store.get(occ["id"])["paged_at"] is not None


def test_a_hold_does_not_page_until_it_lapses(store):
    r = _Recorder(store)
    occ = _open(store)
    store.set_paging(occ["id"], "2999-01-01T00:00:00+00:00", "silence-1")

    assert ("paged", occ["id"]) not in r.events
    store.mark_paged(occ["id"])          # the scheduler, once page_at passes
    assert ("paged", occ["id"]) in r.events


# --- closed -----------------------------------------------------------

def test_closing_fires_once_and_says_who(store):
    """The webhook and the stale sweep can both try to close the same
    occurrence; only the UPDATE that lands reports it."""
    r = _Recorder(store)
    occ = _open(store)
    store.mark_paged(occ["id"])
    first = store.close("sig-1", by="alertmanager")
    second = store.close("sig-1", by="sync")

    assert first["resolved_at"] and second is None
    assert [e for e in r.events if e[0] == "closed"] == [("closed", occ["id"], "alertmanager")]


def test_the_closed_row_says_whether_it_ever_paged(store):
    got = {}
    store.on_closed = lambda occ, by: got.update(occ)
    _open(store)
    store.close("sig-1", by="sync")

    assert got["paged_at"] is None, "cleared while pending/held: consumers can tell nobody was paged"


def test_a_failing_hook_never_breaks_the_transition(store):
    def boom(*a):
        raise RuntimeError("bus is on fire")
    store.on_opened = store.on_paged = store.on_closed = boom

    occ = _open(store)
    store.mark_paged(occ["id"])
    closed = store.close("sig-1")

    assert occ and closed["resolved_at"] and closed["paged_at"]


def test_open_ids_is_every_open_occurrence(store):
    a = _open(store, "a"); b = _open(store, "b"); c = _open(store, "c")
    store.close("b")

    assert store.open_ids() == {a["id"], c["id"]}
