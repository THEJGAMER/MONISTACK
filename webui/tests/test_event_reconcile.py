"""The SSH fallback: transitions the poll observes become events; state at
first sight does not (except for ports someone classified); a stale poll
never closes a newer event; reachability and thresholds hold for N polls.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import event_reconcile as rec  # noqa: E402
from eventstore import signature_for  # noqa: E402
from test_event_detect import _Settings, _Store  # noqa: E402


class _Ports:
    def __init__(self, **over):
        self.over = over

    def severity_for(self, device_id, port, fallback):
        return self.over.get(port, fallback)

    def has_override(self, device_id, port):
        return port in self.over


class _StoreR(_Store):
    """The reconciler also needs open_kind() with raised_at."""
    def raise_event(self, *a, **kw):
        ev, created = super().raise_event(*a, **kw)
        ev.setdefault("raised_at", datetime.now(timezone.utc).isoformat())
        return ev, created

    def open_kind(self, kind, device_id, subject):
        return self.open.get(signature_for(kind, device_id, subject))


def _status(polled_at, ports=(), env=None, cpu=None, mem=None, error=None):
    return {"last_polled": polled_at, "last_error": error,
            "interfaces": [{"port": p, "port_state": s} for p, s in ports],
            "env": env or {}, "cpu": {"overall": {"1min": cpu}} if cpu is not None else {},
            "memory": mem or {}}


def _r(ports=None):
    store = _StoreR()
    return store, rec.SshReconciler(store, _Settings(), ports or _Ports())


def _t(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def test_a_port_raises_on_an_observed_transition_not_on_state_at_first_sight():
    store, r = _r()
    r.reconcile("s4048", "S4048", _status(_t(-60), ports=[("Te 1/1", "down"), ("Te 1/47", "up")]))
    assert store.log == [], "Te 1/1 was already down when we first looked: not an event"
    r.reconcile("s4048", "S4048", _status(_t(-30), ports=[("Te 1/1", "down"), ("Te 1/47", "down")]))
    assert store.log == [("raise", "port.link_down", "Te 1/47", "warning")]
    assert store.open[signature_for("port.link_down", "s4048", "Te 1/47")]["source"] == "ssh"


def test_a_classified_port_down_at_first_sight_is_an_event():
    store, r = _r(_Ports(**{"Te 1/47": "critical"}))
    r.reconcile("s4048", "S4048", _status(_t(-60), ports=[("Te 1/47", "down"), ("Te 1/1", "down")]))
    assert store.log == [("raise", "port.link_down", "Te 1/47", "critical")]


def test_the_poll_resolves_only_when_newer_than_the_event():
    store, r = _r()
    ev, _ = store.raise_event("port.link_down", "warning", "s4048", "S4048", "Te 1/47", "t")
    ev["raised_at"] = _t(0)
    r.reconcile("s4048", "S4048", _status(_t(-30), ports=[("Te 1/47", "up")]))
    assert ("resolve", "port.link_down", "Te 1/47", "ssh") not in store.log, "a stale snapshot must not close a fresh event"
    r.reconcile("s4048", "S4048", _status(_t(+5), ports=[("Te 1/47", "up")]))
    assert store.log[-1] == ("resolve", "port.link_down", "Te 1/47", "ssh")


def test_the_same_snapshot_is_not_reconciled_twice():
    store, r = _r()
    s = _status(_t(-60), ports=[("Te 1/47", "up")])
    r.reconcile("s4048", "S4048", s)
    r.reconcile("s4048", "S4048", _status(s["last_polled"], ports=[("Te 1/47", "down")]))
    assert store.log == []


def test_environment_state_is_reconciled_directly():
    store, r = _r()
    env = {"fans": [{"unit": 1, "bay": 1, "fan1_status": "up", "fan2_status": None}, {"unit": 1, "bay": 2, "fan1_status": "down", "fan2_status": "up"}],
           "psus": [{"unit": 1, "bay": 2, "status": "down", "removed": True}]}
    r.reconcile("s4048", "S4048", _status(_t(-60), env=env))
    assert sorted(store.log) == [("raise", "env.fan", "Fan tray 2 (unit 1)", "critical"), ("raise", "env.psu", "PSU 2 (unit 1)", "critical")]
    env["psus"][0]["status"] = "up"; env["fans"][1]["fan1_status"] = "up"
    r.reconcile("s4048", "S4048", _status(_t(+5), env=env))
    assert ("resolve", "env.psu", "PSU 2 (unit 1)", "ssh") in store.log and ("resolve", "env.fan", "Fan tray 2 (unit 1)", "ssh") in store.log


def test_unreachable_after_consecutive_failed_passes_then_resolved_by_a_good_poll():
    store, r = _r()
    for _ in range(2):
        r.reconcile("s4048", "S4048", _status(None, error="timed out"))
    assert store.log == []
    r.reconcile("s4048", "S4048", _status(None, error="timed out"))
    assert store.log == [("raise", "device.unreachable", "ssh", "critical")]
    r.reconcile("s4048", "S4048", _status(_t(0)))
    assert store.log[-1] == ("resolve", "device.unreachable", "ssh", "ssh")


def test_cpu_and_memory_thresholds_hold_for_n_polls():
    store, r = _r()
    for i in range(2):
        r.reconcile("s4048", "S4048", _status(_t(-60 + i), cpu=95))
    assert store.log == []
    r.reconcile("s4048", "S4048", _status(_t(-50), cpu=95, mem={"total": 100, "used": 92}))
    assert ("raise", "compute.cpu_high", "cpu", "warning") in store.log and ("raise", "compute.memory_high", "memory", "warning") not in store.log
    r.reconcile("s4048", "S4048", _status(_t(-40), cpu=50, mem={"total": 100, "used": 93}))
    assert ("resolve", "compute.cpu_high", "cpu", "ssh") in store.log and ("raise", "compute.memory_high", "memory", "warning") in store.log
