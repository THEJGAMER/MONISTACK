"""The SSH poll must not re-raise what syslog has already fixed.

The poll reads a cache that can be most of a cycle old; syslog is
immediate. Seen in production: a port went down and came back inside one
poll interval, syslog raised and resolved it, and then the poll raised it
again from the snapshot taken while it was still down - 8.7 seconds after
the resolve, for a port that was up. Because a returning fault re-opens
its own event, that also dragged the episode out rather than making an
obvious duplicate row.

The mirror of the guard that stops a stale poll *resolving* something
raised after its snapshot.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

from eventstore import signature_for  # noqa: E402
from test_event_detect import _Settings  # noqa: E402
from test_event_reconcile import _Ports, _StoreR, _iface, _status, _t  # noqa: E402

import event_reconcile as rec  # noqa: E402


class _StoreWithHistory(_StoreR):
    """Adds the one read the guard needs: the last episode for a signature."""
    def __init__(self):
        super().__init__()
        self.history = {}

    def remember(self, kind, device_id, subject, resolved_at):
        self.history[signature_for(kind, device_id, subject)] = {"resolved_at": resolved_at}

    def latest_for(self, signature):
        return self.history.get(signature)


def _reconciler(ports=None):
    store = _StoreWithHistory()
    return store, rec.SshReconciler(store, _Settings(), ports or _Ports())


def _poll(at, port="Te 1/47", state="down"):
    return _status(at, ports=[(port, state)])


def test_the_poll_does_not_re_raise_what_syslog_already_fixed():
    store, r = _reconciler()
    # the poller saw it down a moment ago...
    snapshot = _t(-20)
    # ...but syslog said it was back up after that snapshot was taken
    store.remember("port.link_down", "s4048", "Te 1/47", _t(-5))

    r.reconcile("s4048", "S4048", _poll(_t(-60), state="up"))   # baseline: up
    r.reconcile("s4048", "S4048", _poll(snapshot))              # now reads down, but staler than the fix

    assert store.log == [], "the port is up; this snapshot is simply out of date"


def test_a_fault_that_is_still_down_is_raised_normally():
    store, r = _reconciler()
    store.remember("port.link_down", "s4048", "Te 1/47", _t(-90))   # an older, unrelated episode

    r.reconcile("s4048", "S4048", _poll(_t(-60), state="up"))
    r.reconcile("s4048", "S4048", _poll(_t(-10)))                   # snapshot newer than that resolve

    assert store.log == [("raise", "port.link_down", "Te 1/47", "warning")]


def test_the_first_ever_fault_is_never_suppressed():
    store, r = _reconciler()
    r.reconcile("s4048", "S4048", _poll(_t(-60), state="up"))
    r.reconcile("s4048", "S4048", _poll(_t(-10)))

    assert store.log == [("raise", "port.link_down", "Te 1/47", "warning")]


def test_an_episode_still_open_does_not_suppress_anything():
    """Only a *resolution* newer than the snapshot means the poll is out
    of date. An open event is the same fault, and raising bumps it."""
    store, r = _reconciler()
    store.history[signature_for("port.link_down", "s4048", "Te 1/47")] = {"resolved_at": None}

    r.reconcile("s4048", "S4048", _poll(_t(-60), state="up"))
    r.reconcile("s4048", "S4048", _poll(_t(-10)))

    assert store.log == [("raise", "port.link_down", "Te 1/47", "warning")]


def test_environment_and_compute_get_the_same_protection():
    store, r = _reconciler()
    store.remember("env.psu", "s4048", "PSU 2 (unit 1)", _t(-5))
    env = {"psus": [{"unit": 1, "bay": 2, "status": "down"}], "fans": []}

    r.reconcile("s4048", "S4048", _status(_t(-20), env=env))

    assert store.log == [], "syslog already reported the PSU back; the poll is behind"
