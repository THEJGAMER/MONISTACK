"""Tests for retention.py.

Run against a real Postgres in a throwaway schema, because what matters
here is the SQL: a `LIKE` predicate that has to partition a table exactly,
and an `auto_saved` flag that separates deliberate keeps from automatic ones. A fake DB would
happily "pass" all three while the real database did something else.

Events: only resolved ones age out - an open event is live state
however old.
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

import retention  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")

DDL = """
CREATE TABLE metric_samples (
    id BIGSERIAL PRIMARY KEY, device_id TEXT, metric TEXT, port TEXT,
    value DOUBLE PRECISION, recorded_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY, signature TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
    device TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, source TEXT NOT NULL,
    raised_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ
);
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, action TEXT,
    target TEXT, detail TEXT, fingerprint TEXT, occurrence_id BIGINT
);
CREATE TABLE results (
    filename TEXT PRIMARY KEY, device_id TEXT, command TEXT,
    auto_saved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE command_history (
    id BIGSERIAL PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, device_id TEXT,
    category_id TEXT, command_id TEXT, command TEXT, status TEXT
);
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
        cur = self._cur(); cur.execute(sql, params); return cur

    def query(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchall()

    def query_one(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchone()


@pytest.fixture
def db():
    schema = f"test_ret_{uuid.uuid4().hex[:12]}"
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


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _count(db, table):
    return db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]


# --- the cascade hazard ----------------------------------------------

def _event(db, resolved_days_ago=None, raised_days_ago=400):
    db.execute("INSERT INTO events (signature, kind, severity, device, title, source, raised_at, resolved_at) "
               "VALUES ('sig', 'port.link_down', 'warning', 'S4048', 't', 'syslog', %s, %s)",
               (_iso(raised_days_ago), _iso(resolved_days_ago) if resolved_days_ago is not None else None))


def test_an_old_resolved_event_is_pruned(db):
    _event(db, resolved_days_ago=399)
    retention.prune_all(db)
    assert _count(db, "events") == 0


def test_a_recently_resolved_event_stays(db):
    _event(db, resolved_days_ago=1)
    retention.prune_all(db)
    assert _count(db, "events") == 1


def test_a_still_open_event_is_never_pruned_however_old(db):
    _event(db, resolved_days_ago=None, raised_days_ago=900)
    retention.prune_all(db)
    assert _count(db, "events") == 1


def test_only_auto_saved_results_age_out(db):
    """A result someone clicked Save on is a deliberate keep and must
    outlive the auto-saved copy of every command ever run."""
    db.execute("INSERT INTO results (filename, device_id, command, auto_saved, created_at) "
               "VALUES ('auto.md','d','show version',1,%s)", (_iso(400),))
    db.execute("INSERT INTO results (filename, device_id, command, auto_saved, created_at) "
               "VALUES ('kept.md','d','show version',0,%s)", (_iso(400),))

    retention.prune_all(db)

    remaining = [r["filename"] for r in db.query("SELECT filename FROM results")]
    assert remaining == ["kept.md"]


# --- the metric_samples split ----------------------------------------

def test_interface_and_other_samples_are_partitioned_exactly(db):
    """The two policies must cover every row exactly once - a gap leaves
    rows nothing ever deletes, an overlap double-counts the dry run."""
    for metric in ("iface_input_mbps", "iface_output_errors", "optic_temp_c", "psu_power_watts"):
        db.execute("INSERT INTO metric_samples (device_id, metric, port, value, recorded_at) "
                   "VALUES ('d',%s,'Te 1/1',1.0, now())", (metric,))

    total = _count(db, "metric_samples")
    counted = 0
    for p in retention.POLICIES:
        if not p.name.startswith("metric_samples"):
            continue
        sql = p.sql.replace("DELETE FROM", "SELECT COUNT(*) AS n FROM", 1)
        counted += db.query_one(sql, (datetime.now(timezone.utc) + timedelta(days=1),))["n"]

    assert counted == total == 4


def test_interface_samples_age_out_before_optic_samples(db):
    """The point of splitting: the 94%-of-rows series gets a shorter
    window than the rare, diagnostically valuable one."""
    old = datetime.now(timezone.utc) - timedelta(days=60)
    db.execute("INSERT INTO metric_samples (device_id, metric, port, value, recorded_at) "
               "VALUES ('d','iface_input_mbps','Te 1/1',1.0,%s)", (old,))
    db.execute("INSERT INTO metric_samples (device_id, metric, port, value, recorded_at) "
               "VALUES ('d','optic_temp_c','Te 1/1',30.0,%s)", (old,))

    retention.prune_all(db)  # defaults: iface 30d, other 180d

    rows = [r["metric"] for r in db.query("SELECT metric FROM metric_samples")]
    assert rows == ["optic_temp_c"]


# --- policy mechanics -------------------------------------------------

def test_zero_days_disables_a_policy(db, monkeypatch):
    """"Keep forever" must be expressible honestly, rather than by setting
    an absurd number."""
    monkeypatch.setenv("RETAIN_AUDIT_LOG_DAYS", "0")
    db.execute("INSERT INTO audit_log (ts, actor, action) VALUES (%s,'a','x')", (_iso(9999),))

    results = retention.prune_all(db)

    assert _count(db, "audit_log") == 1
    audit = next(r for r in results if r["table"] == "audit_log")
    assert audit["skipped"] == "retention disabled"


def test_dry_run_deletes_nothing_but_reports_what_would_go(db):
    db.execute("INSERT INTO audit_log (ts, actor, action) VALUES (%s,'a','x')", (_iso(400),))

    results = retention.prune_all(db, dry_run=True)

    assert _count(db, "audit_log") == 1, "dry run must not delete"
    assert next(r for r in results if r["table"] == "audit_log")["deleted"] == 1


def test_recent_rows_are_untouched(db):
    for table, col in (("audit_log", "ts"), ("command_history", "ts")):
        db.execute(f"INSERT INTO {table} ({col}) VALUES (%s)", (_iso(1),))

    retention.prune_all(db)

    assert _count(db, "audit_log") == 1
    assert _count(db, "command_history") == 1


def test_one_failing_policy_does_not_stop_the_others(db):
    """This runs on a background loop - a single bad policy must not stop
    every other table being pruned, nor kill the thread."""
    broken = retention.Policy("nope", "RETAIN_NOPE_DAYS", 1, "DELETE FROM table_that_does_not_exist WHERE x < %s")
    db.execute("INSERT INTO audit_log (ts, actor, action) VALUES (%s,'a','x')", (_iso(400),))
    original = retention.POLICIES[:]
    retention.POLICIES.insert(0, broken)
    try:
        results = retention.prune_all(db)
    finally:
        retention.POLICIES[:] = original

    assert next(r for r in results if r["table"] == "nope")["skipped"] is not None
    assert _count(db, "audit_log") == 0, "a later policy still ran"


def test_prune_all_is_a_no_op_without_a_database():
    assert retention.prune_all(None) == []
