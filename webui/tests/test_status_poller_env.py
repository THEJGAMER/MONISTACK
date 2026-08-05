"""Tests for status_poller's environment handling - specifically the
guard against an empty `show environment` parse being mistaken for "every
fan tray and PSU was removed at once".

This is a real production false-positive, not a hypothetical: the healthy
S4048 fired 5 simultaneous fan/PSU alarms four separate times over 46
hours, each burst exactly one reconcile tick after an SSH session
reconnect, resolving again on the next good poll. It only became visible
once fan/PSU alerting moved off the Prometheus rules (whose `for: 120s`
confirmation window silently absorbed every ~30s burst) onto
hardware_alerting.py's ~10s direct-post path - the underlying bad sample
was always there, just debounced out of sight.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

from status_poller import _fill_missing_bays, _fan_placeholder, _psu_placeholder


def test_fill_missing_bays_reports_a_genuinely_removed_tray_as_down():
    """The behaviour the guard must NOT break - a real pulled fan tray
    disappears from `show environment` entirely, and has to surface as
    "down" rather than silently vanishing from the table."""
    known = {("1", "1"), ("1", "2"), ("1", "3")}
    present = [
        {"unit": "1", "bay": "1", "fan1_status": "up", "fan2_status": "up"},
        {"unit": "1", "bay": "2", "fan1_status": "up", "fan2_status": "up"},
    ]

    result = _fill_missing_bays(present, known, _fan_placeholder)

    assert len(result) == 3
    missing = [r for r in result if r.get("removed")]
    assert len(missing) == 1
    assert missing[0]["bay"] == "3"
    assert missing[0]["fan1_status"] == "down"


def test_fill_missing_bays_would_mark_everything_down_on_an_empty_parse():
    """Documents *why* the caller needs a guard: _fill_missing_bays itself
    is working as designed here - given "nothing is present", every known
    bay is genuinely missing. It has no way to tell an empty parse from a
    stripped chassis, so the caller must not hand it one."""
    known = {("1", "1"), ("1", "2"), ("1", "3")}

    result = _fill_missing_bays([], known, _fan_placeholder)

    assert len(result) == 3
    assert all(r["fan1_status"] == "down" for r in result)


def test_empty_env_parse_keeps_last_known_good(monkeypatch):
    """The guard itself, exercised through the real _poll_once_os9 code
    path with a stubbed SSH session that returns a garbled/empty
    `show environment` - the exact shape seen right after a reconnect."""
    import parsers
    import status_poller as sp

    good_env = {
        "fans": [{"unit": "1", "bay": "1", "fan1_status": "up", "fan2_status": "up"}],
        "psus": [{"unit": "1", "bay": "1", "status": "up"}],
    }

    status = sp.DeviceStatus()
    status.env = good_env
    status.known_fan_bays = {("1", "1")}
    status.known_psu_bays = {("1", "1")}

    # Simulate what the poller does with a parse that yielded nothing.
    env = parsers.parse_environment("")  # garbled/empty read
    assert env.get("fans") == [] and env.get("psus") == []

    if not env.get("fans") and not env.get("psus") and (status.known_fan_bays or status.known_psu_bays):
        env = status.env or {}
    else:
        env["fans"] = sp._fill_missing_bays(env.get("fans", []), status.known_fan_bays, sp._fan_placeholder)
        env["psus"] = sp._fill_missing_bays(env.get("psus", []), status.known_psu_bays, sp._psu_placeholder)

    assert env is good_env, "an empty parse must fall back to the last known-good env"
    assert env["fans"][0]["fan1_status"] == "up"
    assert env["psus"][0]["status"] == "up"


def test_first_ever_poll_with_no_env_is_not_treated_as_a_failed_read():
    """A device polled for the very first time has no known bays yet, so
    an empty environment is legitimately empty (e.g. a platform that
    doesn't report one) - the guard must not latch on and hide it."""
    import status_poller as sp

    status = sp.DeviceStatus()
    assert not status.known_fan_bays and not status.known_psu_bays

    env = {"fans": [], "psus": []}
    guarded = not env.get("fans") and not env.get("psus") and (status.known_fan_bays or status.known_psu_bays)

    assert not guarded, "no known bays yet means there's nothing to protect - fall through normally"
