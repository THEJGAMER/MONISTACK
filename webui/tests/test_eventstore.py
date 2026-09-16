"""The event store: one open row per thing, raised once, bumped while it
persists, resolved once - and the hooks fire exactly once per transition
whichever path drives it. Real Postgres: the guarantees are SQL.
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

import eventstore  # noqa: E402
from eventstore import EventStore, signature_for  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")
DDL = """
CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY, signature TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
    device_id TEXT, device TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, detail TEXT,
    labels TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL, signal_at TEXT,
    raised_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), count INTEGER NOT NULL DEFAULT 1,
    resolved_at TIMESTAMPTZ, resolved_by TEXT, resolve_detail TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0, reopened_at TIMESTAMPTZ);
CREATE UNIQUE INDEX idx_events_one_open ON events(signature) WHERE resolved_at IS NULL;
CREATE TABLE event_settings (kind TEXT PRIMARY KEY, severity TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE port_settings (device_id TEXT NOT NULL, port TEXT NOT NULL, severity TEXT NOT NULL, PRIMARY KEY (device_id, port));
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
def db():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=4)
    except Exception:
        pytest.skip("test Postgres not reachable")
    conn.autocommit = True
    schema = f"test_ev_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield _DB(conn)
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


@pytest.fixture
def store(db):
    return EventStore(db)


def _raise(store, kind="port.link_down", subject="Te 1/47", severity="warning", **kw):
    return store.raise_event(kind, severity, "s4048", "S4048", subject, f"Link down: {subject} on S4048", **kw)


def test_raise_once_bump_while_it_persists_resolve_once(store):
    got = []
    store.on_raised = lambda e: got.append(("raised", e["id"]))
    store.on_resolved = lambda e: got.append(("resolved", e["id"]))

    ev, created = _raise(store, detail="first")
    again, created2 = _raise(store, detail="again")
    assert created and not created2 and again["id"] == ev["id"] and again["count"] == 2 and again["detail"] == "again"

    resolved = store.resolve(ev["signature"], by="syslog", detail="up")
    assert resolved["resolved_by"] == "syslog" and store.resolve(ev["signature"]) is None
    assert got == [("raised", ev["id"]), ("resolved", ev["id"])]


# --- de-duplication: one episode, however often it comes and goes ---------

def test_a_fault_that_returns_reopens_its_own_row(store):
    """A port unplugged five times is one event that came back four
    times, not five rows. Confirmed live: one test session of unplugging
    produced a separate row per replug."""
    got = []
    store.on_raised = lambda e: got.append(("raised", e["reopen_count"]))
    store.on_resolved = lambda e: got.append(("resolved", e["reopen_count"]))
    first, created = _raise(store)
    assert created

    for _ in range(4):
        store.resolve(first["signature"], by="syslog")
        again, is_news = _raise(store)
        assert is_news, "a return is news - the phone should hear about it"

    assert again["id"] == first["id"], "same row"
    assert again["reopen_count"] == 4 and again["reopened_at"]
    assert again["raised_at"] == first["raised_at"], "raised_at stays at the first occurrence"
    assert again["count"] == 5 and again["resolved_at"] is None
    assert len(store.list()) == 1
    assert got.count(("raised", 0)) == 1 and [c for k, c in got if k == "raised"] == [0, 1, 2, 3, 4]


def test_a_return_long_after_it_cleared_is_a_new_episode(store):
    a, _ = _raise(store)
    store.resolve(a["signature"])
    store.db.execute("UPDATE events SET resolved_at = now() - interval '2 hours' WHERE id = %s", (a["id"],))

    b, created = _raise(store)

    assert created and b["id"] != a["id"] and b["reopen_count"] == 0
    assert len(store.list()) == 2, "this morning's outage and this afternoon's are two events"


def test_the_reopen_window_is_per_call_overridable(store):
    a, _ = _raise(store)
    store.resolve(a["signature"])
    b, _ = _raise(store, reopen_within=0)
    assert b["id"] != a["id"], "window 0 turns re-opening off"


def test_reopening_refreshes_what_the_new_report_says(store):
    a, _ = _raise(store, severity="warning", detail="first")
    store.resolve(a["signature"], by="ssh", detail="cleared")
    b, _ = _raise(store, severity="critical", detail="worse now")

    assert b["id"] == a["id"]
    assert (b["severity"], b["detail"]) == ("critical", "worse now")
    assert b["resolved_by"] is None and b["resolve_detail"] is None, "the old resolution is not left hanging on an open event"


def test_a_repeat_while_open_is_not_news_and_does_not_count_as_a_return(store):
    got = []
    store.on_raised = lambda e: got.append(e["id"])
    a, _ = _raise(store)
    b, is_news = _raise(store, detail="same thing again")

    assert not is_news and b["id"] == a["id"]
    assert b["count"] == 2 and b["reopen_count"] == 0
    assert got == [a["id"]], "one notification, not one per report"


def test_signature_is_kind_device_subject(store):
    assert signature_for("port.link_down", "s4048", "Te 1/1") != signature_for("port.link_down", "s4048", "Te 1/2")
    assert signature_for("env.psu", "s4048", "PSU 1") == signature_for("env.psu", "s4048", "PSU 1")


def test_resolve_by_id_kind_and_bulk(store):
    a, _ = _raise(store, subject="Te 1/1")
    b, _ = _raise(store, subject="Te 1/2")
    c, _ = _raise(store, kind="syslog.rule", subject="STP", labels={"rule_id": "7"})
    assert store.resolve_id(a["id"], by="jacob")["resolved_by"] == "jacob"
    assert store.resolve_kind("port.link_down", "s4048", "Te 1/2", by="ssh")["id"] == b["id"]
    assert store.resolve_open(rule_id=7, by="switchboard") == 1
    assert store.open_events() == []


def test_expire_resolves_only_stale_open_events(store):
    a, _ = _raise(store, kind="protocol.stp_topology_change", subject="stp")
    store.db.execute("UPDATE events SET last_seen_at = now() - interval '10 minutes' WHERE id = %s", (a["id"],))
    b, _ = _raise(store, kind="protocol.stp_topology_change", subject="stp2")
    assert store.expire("protocol.stp_topology_change", 300) == 1
    assert store.get(a["id"])["resolved_by"] == "timer" and store.get(b["id"])["resolved_at"] is None


def test_list_filters_and_summary(store):
    _raise(store, subject="Te 1/1", severity="critical")
    _raise(store, subject="Te 1/2")
    c, _ = store.raise_event("env.psu", "critical", "ex3300", "EX3300", "PSU 1", "Power supply fault: PSU 1 on EX3300")
    store.resolve(c["signature"])

    assert len(store.list()) == 3
    assert [e["subject"] for e in store.list(open_only=True)] == ["Te 1/2", "Te 1/1"]
    assert [e["kind"] for e in store.list(group="env")] == ["env.psu"]
    assert [e["subject"] for e in store.list(severity=["critical"], open_only=True)] == ["Te 1/1"]
    assert [e["device"] for e in store.list(q="ex33")] == ["EX3300"]
    s = store.summary()
    assert s["open"] == {"info": 0, "warning": 1, "critical": 1} and s["total_open"] == 2 and s["last_24h"]["critical"] == 2


def test_severity_is_validated_and_kind_name_comes_from_the_catalogue(store):
    with pytest.raises(ValueError):
        _raise(store, severity="loud")
    ev, _ = _raise(store)
    assert ev["kind_name"] == "Link down" and ev["group"] == "port"


def test_prune_drops_old_resolved_only(store):
    a, _ = _raise(store, subject="old")
    store.resolve(a["signature"])
    store.db.execute("UPDATE events SET resolved_at = now() - interval '400 days' WHERE id = %s", (a["id"],))
    b, _ = _raise(store, subject="open-forever")
    store.db.execute("UPDATE events SET raised_at = now() - interval '400 days' WHERE id = %s", (b["id"],))
    assert store.prune(180) == 1 and store.get(b["id"]) is not None
