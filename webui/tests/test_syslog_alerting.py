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


# --- the engine ---------------------------------------------------------------

class _AM:
    def __init__(self):
        self.posted = []

    def post_alerts(self, alerts):
        self.posted.extend(alerts)

    def list_alerts(self):
        return []


def _device_for(event):
    return ("dev-1", "S4048") if event.get("device_host") == "S4048" else ("", event.get("device_host") or "unknown")


def _ev(msg, host="S4048", **kw):
    return {"message": msg, "detail": msg, "device_host": host, **kw}


def test_a_matching_line_fires_once_and_a_clearing_line_resolves():
    am, eng = _AM(), sa.SyslogRuleEngine()
    rules = [_rule()]

    assert eng.evaluate([_ev("%BGP-5-ADJCHANGE: neighbor 10.0.0.1 Down")], rules, _device_for, am) == 1
    assert eng.evaluate([_ev("%BGP-5-ADJCHANGE: neighbor 10.0.0.1 Down")], rules, _device_for, am) == 0, "already firing"
    fired = am.posted[0]
    assert fired["labels"] == {"alertname": "Routing neighbour lost", "source": "syslog-rule", "rule_id": "1",
                               "device": "S4048", "severity": "critical", "device_id": "dev-1"}
    assert "endsAt" not in fired and fired["annotations"]["summary"].startswith("Routing neighbour lost on S4048")

    assert eng.evaluate([_ev("%BGP-5-ADJCHANGE: neighbor 10.0.0.1 Up")], rules, _device_for, am) == 1
    assert "endsAt" in am.posted[-1] and eng.active() == []


def test_one_alarm_per_device_or_per_interface_as_the_rule_says():
    am, eng = _AM(), sa.SyslogRuleEngine()
    per_dev = [_rule(id=1, pattern="(?i)flap", clear_pattern="")]
    per_if = [_rule(id=2, pattern="(?i)flap", clear_pattern="", per_interface=True)]

    eng.evaluate([_ev("flap", interface="Te 1/1"), _ev("flap", interface="Te 1/2")], per_dev, _device_for, am)
    eng.evaluate([_ev("flap", interface="Te 1/1"), _ev("flap", interface="Te 1/2")], per_if, _device_for, am)

    active = eng.active()
    assert len(active) == 3
    assert sorted(a["labels"].get("interface", "") for a in active) == ["", "Te 1/1", "Te 1/2"]


def test_auto_resolve_and_heartbeat_on_tick(monkeypatch):
    am, eng = _AM(), sa.SyslogRuleEngine()
    clock = [1000.0]
    monkeypatch.setattr(sa.time, "monotonic", lambda: clock[0])
    eng.evaluate([_ev("%STP-5-TOPO: Topology change")], [_rule(id=3, pattern="(?i)topology", clear_pattern="", auto_resolve_seconds=300)],
                 _device_for, am)
    assert eng.tick(am) == 0 and len(am.posted) == 1

    clock[0] += sa.HEARTBEAT_SECONDS
    eng.tick(am)
    assert len(am.posted) == 2 and "endsAt" not in am.posted[-1], "heartbeat keeps Alertmanager from timing it out"

    clock[0] += 300
    assert eng.tick(am) == 1
    assert "endsAt" in am.posted[-1] and eng.active() == []


def test_the_same_event_from_two_paths_is_evaluated_once():
    """The fast path and the Loki poll carry the same Vector timestamp."""
    am, eng = _AM(), sa.SyslogRuleEngine()
    rules = [_rule(pattern="(?i)down", clear_pattern="(?i)up")]
    ev_down = _ev("bgp down", _timestamp_ns=1000)
    ev_up = _ev("bgp up", _timestamp_ns=2000)

    assert eng.evaluate_new([ev_down], rules, _device_for, am) == 1        # fast path
    assert eng.evaluate_new([ev_down], rules, _device_for, am) == 0        # loki poll, same event: skipped
    assert eng.evaluate_new([ev_up], rules, _device_for, am) == 1
    assert eng.evaluate_new([ev_down, ev_up], rules, _device_for, am) == 0, "a re-read of both changes nothing"
    assert eng.cursor_ns == 2000


def test_a_disabled_rule_is_ignored_and_a_removed_rule_takes_its_alarms():
    am, eng = _AM(), sa.SyslogRuleEngine()
    assert eng.evaluate([_ev("bgp down")], [_rule(enabled=False)], _device_for, am) == 0
    eng.evaluate([_ev("bgp down")], [_rule()], _device_for, am)
    assert eng.forget_rule(1, am) == 1 and eng.active() == []


def test_an_unknown_sender_still_alarms_under_its_own_name():
    am, eng = _AM(), sa.SyslogRuleEngine()
    eng.evaluate([_ev("%SWB-4-SWITCHBOARD_SELFTEST: nonce=1", host="switchboard", mnemonic="SWITCHBOARD_SELFTEST")],
                 [_rule(id=9, mnemonic="SWITCHBOARD_SELFTEST", pattern="", clear_pattern="")], _device_for, am)
    labels = am.posted[0]["labels"]
    assert labels["device"] == "switchboard" and "device_id" not in labels


def test_reseed_adopts_alertmanagers_active_rule_alarms():
    class _AMActive(_AM):
        def list_alerts(self):
            return [{"labels": {"alertname": "X", "source": "syslog-rule", "rule_id": "1", "device": "S4048", "device_id": "dev-1", "severity": "critical"},
                     "annotations": {"summary": "s"}, "status": {"state": "active"}},
                    {"labels": {"alertname": "Y", "source": "other"}, "status": {"state": "active"}}]
    eng = sa.SyslogRuleEngine()
    assert eng.reseed_from_alertmanager(_AMActive(), {"1": _rule()}) == 1
    assert eng.active()[0]["labels"]["alertname"] == "X"


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


def test_defaults_are_seeded_once_and_only_the_selftest_is_on(store):
    assert store.seed_defaults() == len(sa.DEFAULT_RULES)
    assert store.seed_defaults() == 0
    on = [r["name"] for r in store.list(enabled_only=True)]
    assert on == ["Switchboard fast-path self-test"]


def test_a_deleted_default_stays_deleted_but_the_selftest_comes_back(store):
    store.seed_defaults()
    stp = next(r for r in store.list() if r["key"] == "stp-topology-change")
    assert store.delete(stp["id"]) is True
    assert store.seed_defaults() == 0 and all(r["key"] != "stp-topology-change" for r in store.list())
    selftest = store.get_by_key("selftest")
    assert store.delete(selftest["id"]) is False, "builtin: cannot be deleted"
    assert store.ensure_selftest("critical")["severity"] == "critical"


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
