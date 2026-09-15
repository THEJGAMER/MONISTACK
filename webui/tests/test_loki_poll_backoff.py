"""The three pieces that stop the Loki pollers from amplifying an overload.

Found 2026-09-15: one open Console tab fired a 7-day Loki query every 20s,
which with Loki's defaults was 337 sub-queries into a scheduler queue of
100. The overflow cancelled the two 3-second syslog pollers behind it,
which retried and re-queued, and a brief spike became 45 errors/min for
as long as the tab stayed open.

Loki's own limits were the main fix (loki/loki-config.yaml). These are the
webui's share: pollers that back off when their query fails, checkers
that tell the poller whether it failed, and a ceiling on how wide a
Loki-backed request may be.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import app as app_module  # noqa: E402
import hardware_alerting  # noqa: E402
import interface_alerting  # noqa: E402


# --- PollBackoff -----------------------------------------------------

def test_healthy_polling_keeps_the_fast_cadence():
    b = app_module.PollBackoff(base=3, cap=60)

    for _ in range(5):
        b.ok()

    assert b.delay == 3


def test_each_consecutive_failure_doubles_the_wait():
    b = app_module.PollBackoff(base=3, cap=60)

    b.failed(); assert b.delay == 6
    b.failed(); assert b.delay == 12
    b.failed(); assert b.delay == 24


def test_the_wait_is_capped():
    """Backing off is for letting a queue drain, not for giving up: a fan
    fault still has to page within a minute even after a long outage."""
    b = app_module.PollBackoff(base=3, cap=60)

    for _ in range(20):
        b.failed()

    assert b.delay == 60


def test_one_success_snaps_straight_back():
    """Not a gradual recovery. Once Loki answers, the fast path is correct
    again immediately; easing back in would just delay alerts for nothing."""
    b = app_module.PollBackoff(base=3, cap=60)
    for _ in range(6):
        b.failed()

    b.ok()

    assert b.delay == 3


# --- the checkers report whether their query worked -------------------

class _Dev:
    def __init__(self, id, host):
        self.id, self.host = id, host


class _RaisingLoki:
    def query_range(self, **kw):
        raise RuntimeError("timed out")


class _EmptyLoki:
    def query_range(self, **kw):
        return []


class _NullAlertmanager:
    def fire(self, *a, **k): pass
    def resolve(self, *a, **k): pass
    def post_alert(self, *a, **k): pass


def test_hardware_checker_returns_false_when_loki_fails():
    checker = hardware_alerting.HardwareAlertChecker()

    ok = checker.check_via_syslog(_RaisingLoki(), {"d": _Dev("d", "10.0.0.1")}, _NullAlertmanager(), lambda i: i)

    assert ok is False


def test_hardware_checker_returns_true_on_a_quiet_but_working_query():
    """"Nothing new" is a healthy poll and must not slow the cadence."""
    checker = hardware_alerting.HardwareAlertChecker()

    assert checker.check_via_syslog(_EmptyLoki(), {}, _NullAlertmanager(), lambda i: i) is True


def test_interface_checker_returns_false_when_loki_fails():
    checker = interface_alerting.InterfaceAlertChecker()
    cfg = {"device_id": "d", "port": "Te 1/1", "enabled": True, "mode": "immediate"}

    ok = checker.check_via_syslog([cfg], _RaisingLoki(), {"d": _Dev("d", "10.0.0.1")}, _NullAlertmanager(), lambda i: i)

    assert ok is False


def test_interface_checker_with_nothing_to_watch_is_not_a_failure():
    checker = interface_alerting.InterfaceAlertChecker()

    assert checker.check_via_syslog([], _RaisingLoki(), {}, _NullAlertmanager(), lambda i: i) is True


# --- window ceiling on Loki-backed requests ---------------------------

def test_a_reasonable_window_passes_through():
    assert app_module._clamp_window(3600) == 3600
    assert app_module._clamp_window(604800) == 604800


def test_an_absurd_window_is_capped_not_rejected():
    """A 30-day query at a 24h split is 30 pieces - fine. Unbounded, a typo
    or a hostile value is thousands, which is the overflow all over again."""
    assert app_module._clamp_window(10**9) == app_module.LOKI_MAX_WINDOW_SECONDS


def test_a_tiny_or_garbage_window_gets_a_sane_floor():
    assert app_module._clamp_window(0) == 60
    assert app_module._clamp_window(-5) == 60
    assert app_module._clamp_window("nonsense") == 3600
    assert app_module._clamp_window(None) == 3600
