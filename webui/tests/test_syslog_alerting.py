"""Syslog rules: a log line becomes an alarm on arrival, and stops the way
the rule says - a clearing line, a timer, or the rule going away.

Store tests run against a real Postgres (the regex validation and the
seed-once behaviour are what matter); the engine is pure.
"""
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import syslog_alerting as sa  # noqa: E402


# --- matching (pure) ------------------------------------------------------

def _rule(**kw):
    base = {"id": 1, "name": "Routing neighbour lost", "enabled": True, "severity": "critical",
            "facility": "", "mnemonic": "", "pattern": r"(?i)\b(bgp|ospf)\b.*\bdown\b",
            "clear_pattern": r"(?i)\b(bgp|ospf)\b.*\bup\b", "per_interface": False, "auto_resolve_seconds": 0}
    base.update(kw)
    return base


def test_pattern_fires_clear_pattern_clears_and_the_rest_is_ignored():
    r = _rule()
    assert sa.matches(r, {"message": "%BGP-5-ADJCHANGE: neighbor 10.0.0.1 Down - hold timer"}) is True
    assert sa.matches(r, {"message": "%BGP-5-ADJCHANGE: neighbor 10.0.0.1 Up"}) is False
    assert sa.matches(r, {"message": "%SEC-5-LOGIN: user admin"}) is None


def test_facility_and_mnemonic_are_exact_filters():
    r = _rule(facility="STP", pattern=r"(?i)topology change")
    assert sa.matches(r, {"facility": "STP", "message": "%STP-5-TOPOLOGY_CHANGE: Topology change on Vlan 10"}) is True
    assert sa.matches(r, {"facility": "IFM", "message": "topology change"}) is None
    r = _rule(mnemonic="SWITCHBOARD_SELFTEST", pattern="")
    assert sa.matches(r, {"mnemonic": "SWITCHBOARD_SELFTEST", "message": "anything"}) is True
    assert sa.matches(r, {"mnemonic": "OTHER", "message": "anything"}) is None


# --- the timer ---------------------------------------------------------------------

class _ExpiringStore:
    def __init__(self):
        self.calls = []

    def expire(self, kind, ttl, by="timer", rule_id=None):
        self.calls.append((kind, ttl, rule_id))
        return 1


def test_only_rules_with_a_timer_expire_their_events():
    store = _ExpiringStore()
    rules = [_rule(id=1, auto_resolve_seconds=300), _rule(id=2, auto_resolve_seconds=0), _rule(id=3, auto_resolve_seconds=60)]
    assert sa.expire_rules(store, rules) == 2
    assert store.calls == [("syslog.rule", 300, 1), ("syslog.rule", 60, 3)]


# --- the store, against Postgres ----------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")
DDL = """
CREATE TABLE syslog_alert_rules (
    id BIGSERIAL PRIMARY KEY, key TEXT UNIQUE, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    severity TEXT NOT NULL DEFAULT 'warning', facility TEXT NOT NULL DEFAULT '', mnemonic TEXT NOT NULL DEFAULT '',
    pattern TEXT NOT NULL DEFAULT '', clear_pattern TEXT NOT NULL DEFAULT '', per_interface INTEGER NOT NULL DEFAULT 0,
    auto_resolve_seconds INTEGER NOT NULL DEFAULT 0, builtin INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
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
    schema = f"test_sr_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield sa.SyslogRuleStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def test_defaults_are_seeded_once_and_all_off(store):
    assert store.seed_defaults() == len(sa.DEFAULT_RULES)
    assert store.seed_defaults() == 0
    assert store.list(enabled_only=True) == []


def test_a_deleted_default_stays_deleted(store):
    store.seed_defaults()
    stp = next(r for r in store.list() if r["key"] == "stp-topology-change")
    assert store.delete(stp["id"]) is True
    assert store.seed_defaults() == 0 and all(r["key"] != "stp-topology-change" for r in store.list())


def test_rules_are_validated(store):
    for bad in [{"name": "", "pattern": "x"}, {"name": "n"}, {"name": "n", "pattern": "("},
                {"name": "n", "pattern": "x", "severity": "loud"}, {"name": "n", "pattern": "x", "auto_resolve_seconds": -1}]:
        with pytest.raises(ValueError):
            store.create(bad)
    r = store.create({"name": " Fan noise ", "facility": "envmon", "pattern": "(?i)noise", "severity": "info", "auto_resolve_seconds": "30"})
    assert (r["name"], r["facility"], r["auto_resolve_seconds"], r["enabled"]) == ("Fan noise", "ENVMON", 30, True)


def test_update_merges_and_unknown_is_none(store):
    r = store.create({"name": "n", "pattern": "x"})
    u = store.update(r["id"], {"enabled": False, "severity": "critical"})
    assert (u["enabled"], u["severity"], u["pattern"]) == (False, "critical", "x")
    assert store.update(999999, {"name": "z"}) is None
