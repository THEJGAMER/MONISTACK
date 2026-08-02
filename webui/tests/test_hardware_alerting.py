"""Tests for hardware_alerting.py - fan/PSU syslog-primary alerting with
an SSH-poll fallback, mirroring interface_alerting.py's design.

_classify_alarm is a second independent implementation of syslog/
vector.yaml's own VRL alarm-normalization block (see its docstring) - the
"cleared" vs "major alarm" substring-ordering bug that shipped once in the
VRL version is exactly the kind of thing worth pinning down here too,
since a fix in one implementation doesn't touch the other.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hardware_alerting import HardwareAlertChecker, _classify_alarm, _parse_component


# --- _classify_alarm ---
# Real captured text (see syslog/tests/test_vrl.py) - not guessed.

def test_classify_psu_down():
    result = _classify_alarm("Major alarm: Power supply 2 in unit 1 is down")
    assert result["alarm_severity"] == "critical"
    assert result["alarm_active"] is True
    assert result["alarm_component"] == "Power supply 2 in unit 1"


def test_classify_psu_up():
    result = _classify_alarm("Power supply 2 in unit 1 is up")
    assert result["alarm_active"] is False


def test_classify_psu_cleared():
    """The bug that shipped once in the VRL version: "Major alarm cleared:
    ..." contains the literal substring "major alarm", so "cleared" must
    be checked first or this misreads as a new fault instead of a
    recovery."""
    result = _classify_alarm("Major alarm cleared: Power supply 2 in unit 1")
    assert result["alarm_active"] is False
    assert result["alarm_severity"] is None


def test_classify_minor_alarm():
    result = _classify_alarm("Minor alarm: Fan 1 in unit 1 is down")
    assert result["alarm_severity"] == "minor"
    assert result["alarm_active"] is True


def test_classify_irrelevant_telemetry_is_not_an_alarm():
    """Routine fan-speed-percentage telemetry lines share the ENVMON/
    CHMGR facilities but aren't alarms at all - must not be misread as
    either a fault or a recovery."""
    result = _classify_alarm("Fan speed at 45 percent")
    assert result["alarm_severity"] is None
    assert result["alarm_active"] is None


# --- _parse_component ---

def test_parse_component_psu():
    assert _parse_component("Power supply 2 in unit 1") == ("psu", "1", "2")


def test_parse_component_fan():
    assert _parse_component("Fan 1 in unit 1") == ("fan", "1", "1")


def test_parse_component_unmatched_text_returns_none():
    """A component string that doesn't match the known shape must return
    None, not raise or guess - the syslog path skips it and
    reconcile_via_poll (which doesn't depend on this parsing at all)
    remains the fallback."""
    assert _parse_component("some unexpected wording") is None
    assert _parse_component(None) is None


# --- HardwareAlertChecker.reconcile_via_poll ---

class _FakeAlertmanager:
    def __init__(self):
        self.posted = []

    def post_alerts(self, alerts):
        self.posted.extend(alerts)

    def list_alerts(self):
        return []


def _device_name_for(device_id):
    return device_id


def test_reconcile_fires_for_a_faulted_psu_with_no_prior_state():
    """The exact scenario this exists for: a device with no syslog
    configured at all (or a missed syslog event) - the poll path alone
    must still detect and fire a real fault."""
    checker = HardwareAlertChecker()
    am = _FakeAlertmanager()
    env = {"fans": [], "psus": [{"unit": 1, "bay": 2, "status": "down"}]}

    def get_env_and_polled_at(device_id):
        return env, "2026-01-01T00:00:00Z"

    checker.reconcile_via_poll(["dev1"], get_env_and_polled_at, _device_name_for, am)

    assert ("dev1", "psu", "1", "2") in checker._alerting
    assert len(am.posted) == 1
    assert am.posted[0]["labels"]["alertname"] == "HardwareAlarm"
    assert "endsAt" not in am.posted[0]


def test_reconcile_resolves_when_fault_clears():
    checker = HardwareAlertChecker()
    am = _FakeAlertmanager()
    faulted_env = {"fans": [], "psus": [{"unit": 1, "bay": 2, "status": "down"}]}
    polls = iter([("t1", faulted_env), ("t2", {"fans": [], "psus": [{"unit": 1, "bay": 2, "status": "up"}]})])

    def get_env_and_polled_at(device_id):
        polled_at, env = next(polls)
        return env, polled_at

    checker.reconcile_via_poll(["dev1"], get_env_and_polled_at, _device_name_for, am)
    assert ("dev1", "psu", "1", "2") in checker._alerting

    checker.reconcile_via_poll(["dev1"], get_env_and_polled_at, _device_name_for, am)
    assert ("dev1", "psu", "1", "2") not in checker._alerting
    assert am.posted[-1]["annotations"]["summary"].endswith("recovered on dev1")
    assert "endsAt" in am.posted[-1]


def test_reconcile_ignores_a_stale_unchanged_snapshot():
    """Same last_polled timestamp twice in a row must not re-fire/re-post
    - this is what lets reconcile_via_poll run on a tight loop without
    hammering Alertmanager every tick when nothing new was learned."""
    checker = HardwareAlertChecker()
    am = _FakeAlertmanager()
    env = {"fans": [], "psus": [{"unit": 1, "bay": 2, "status": "down"}]}

    def get_env_and_polled_at(device_id):
        return env, "same-timestamp"

    checker.reconcile_via_poll(["dev1"], get_env_and_polled_at, _device_name_for, am)
    checker.reconcile_via_poll(["dev1"], get_env_and_polled_at, _device_name_for, am)

    assert len(am.posted) == 1  # only the first tick actually posted


def test_forget_drops_tracking_but_a_still_real_fault_can_refire():
    """Mirrors InterfaceAlertChecker.forget's non-suppression contract - a
    manual resolve clears bookkeeping, but the next poll tick that still
    sees the real fault re-arms it."""
    checker = HardwareAlertChecker()
    am = _FakeAlertmanager()
    env = {"fans": [], "psus": [{"unit": 1, "bay": 2, "status": "down"}]}

    def get_env_and_polled_at(device_id):
        return env, "t1"

    checker.reconcile_via_poll(["dev1"], get_env_and_polled_at, _device_name_for, am)
    assert ("dev1", "psu", "1", "2") in checker._alerting

    checker.forget("dev1", "psu", "1", "2")
    assert ("dev1", "psu", "1", "2") not in checker._alerting

    # A later tick with a genuinely new snapshot and the fault still real re-arms it.
    def get_env_and_polled_at_2(device_id):
        return env, "t2"

    checker.reconcile_via_poll(["dev1"], get_env_and_polled_at_2, _device_name_for, am)
    assert ("dev1", "psu", "1", "2") in checker._alerting
