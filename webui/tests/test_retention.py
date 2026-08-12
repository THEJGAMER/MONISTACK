"""Tests for retention.py.

Run against a real Postgres in a throwaway schema, because what matters
here is the SQL: an `ON DELETE CASCADE` that must not fire, a `LIKE`
predicate that has to partition a table exactly, and an `auto_saved` flag
that separates deliberate keeps from automatic ones. A fake DB would
happily "pass" all three while the real database did something else.

The cascade case is the one worth being careful about: `alarm_acks` and
`alarm_comments` cascade from `alert_occurrences`, so a naive age-based
delete silently destroys acknowledgements and incident discussion. That
isn't hypothetical - a manual cleanup of ~20,800 junk occurrences had to
exclude those rows explicitly or it would have taken 8 comments and 2 acks
with it.
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
CREATE TABLE alert_history (
    id BIGSERIAL PRIMARY KEY, alertname TEXT, status TEXT, severity TEXT,
    summary TEXT, labels TEXT, received_at TEXT NOT NULL
);
CREATE TABLE alert_occurrences (
    id BIGSERIAL PRIMARY KEY, signature TEXT NOT NULL, alertname TEXT NOT NULL,
    severity TEXT, summary TEXT, labels TEXT NOT NULL,
    started_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE alarm_comments (
    id BIGSERIAL PRIMARY KEY,
    occurrence_id BIGINT REFERENCES alert_occurrences(id) ON DELETE CASCADE,
    author TEXT, body TEXT, created_at TEXT
);
CREATE TABLE alarm_acks (
    id BIGSERIAL PRIMARY KEY,
    occurrence_id BIGINT REFERENCES alert_occurrences(id) ON DELETE CASCADE,
    actor TEXT, created_at TEXT
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

def test_an_old_occurrence_carrying_a_comment_is_never_deleted(db):
    """The rule that protects human records. Deleting this row would
    cascade the comment away with it."""
    db.execute("INSERT INTO alert_occurrences (id, signature, alertname, labels, started_at, resolved_at) "
               "VALUES (1,'sig','A','{}',%s,%s)", (_iso(400), _iso(399)))
    db.execute("INSERT INTO alarm_comments (occurrence_id, author, body, created_at) "
               "VALUES (1,'alice','worth keeping',%s)", (_iso(399),))

    retention.prune_all(db)

    assert _count(db, "alert_occurrences") == 1
    assert _count(db, "alarm_comments") == 1


def test_an_old_occurrence_carrying_an_ack_is_never_deleted(db):
    db.execute("INSERT INTO alert_occurrences (id, signature, alertname, labels, started_at, resolved_at) "
               "VALUES (2,'sig2','A','{}',%s,%s)", (_iso(400), _iso(399)))
    db.execute("INSERT INTO alarm_acks (occurrence_id, actor, created_at) VALUES (2,'bob',%s)", (_iso(399),))

    retention.prune_all(db)

    assert _count(db, "alert_occurrences") == 1
    assert _count(db, "alarm_acks") == 1


def test_an_old_plain_occurrence_is_pruned(db):
    """The other half - without this the policy would never delete
    anything and the table still grows forever."""
    db.execute("INSERT INTO alert_occurrences (signature, alertname, labels, started_at, resolved_at) "
               "VALUES ('sig3','A','{}',%s,%s)", (_iso(400), _iso(399)))

    retention.prune_all(db)

    assert _count(db, "alert_occurrences") == 0


def test_a_still_open_occurrence_is_never_pruned_however_old(db):
    """An unresolved alarm is live, not history - age is irrelevant."""
    db.execute("INSERT INTO alert_occurrences (signature, alertname, labels, started_at, resolved_at) "
               "VALUES ('sig4','A','{}',%s,NULL)", (_iso(999),))

    retention.prune_all(db)

    assert _count(db, "alert_occurrences") == 1


# --- deliberate keeps outlive automatic ones -------------------------

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
    for table, col in (("audit_log", "ts"), ("command_history", "ts"), ("alert_history", "received_at")):
        db.execute(f"INSERT INTO {table} ({col}) VALUES (%s)", (_iso(1),))

    retention.prune_all(db)

    assert _count(db, "audit_log") == 1
    assert _count(db, "command_history") == 1
    assert _count(db, "alert_history") == 1


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
