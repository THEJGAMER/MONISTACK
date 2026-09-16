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
        ev.setdefault("reopened_at", None)
        return ev, created

    def open_kind(self, kind, device_id, subject):
        return self.open.get(signature_for(kind, device_id, subject))


def _status(polled_at, ports=(), env=None, cpu=None, mem=None, error=None, interfaces=None, optics_at=None):
    return {"last_polled": polled_at, "last_error": error, "transceivers_polled": optics_at,
            "interfaces": interfaces if interfaces is not None else [{"port": p, "port_state": s} for p, s in ports],
            "env": env or {}, "cpu": {"overall": {"1min": cpu}} if cpu is not None else {},
            "memory": mem or {}}


# Verbatim from the live S4048 (10GBASE-LR modules). The dark ones are the
# point: eight of its twelve DOM optics read like this on ports nobody is
# using, and treating them as faults would mean eight instant criticals.
DARK = {"present": True, "dom_supported": True, "temperature_c": 24.801, "voltage_v": 3.304, "tx_bias_ma": 0.0,
        "tx_power_dbm": -40.0, "rx_power_dbm": -40.0, "rx_los_state": True, "tx_fault_state": False,
        "temperature_high_alarm_flag": False, "temperature_low_alarm_flag": False,
        "rx_power_high_alarm_flag": False, "rx_power_low_alarm_flag": True,
        "tx_power_high_alarm_flag": False, "tx_power_low_alarm_flag": True, "type": "10GBASE-LR"}
HEALTHY = {"present": True, "dom_supported": True, "temperature_c": 34.242, "voltage_v": 3.339, "tx_bias_ma": 46.646,
           "tx_power_dbm": -0.8108, "rx_power_dbm": -1.6134, "rx_los_state": False, "tx_fault_state": False,
           "temperature_high_alarm_flag": False, "temperature_low_alarm_flag": False,
           "rx_power_high_alarm_flag": False, "rx_power_low_alarm_flag": False,
           "tx_power_high_alarm_flag": False, "tx_power_low_alarm_flag": False, "type": "10GBASE-LR"}


def _iface(port, state="up", optic=None, **counters):
    return {"port": port, "port_state": state, "transceiver": optic, **counters}


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


def test_a_poll_older_than_the_fault_coming_back_does_not_close_it():
    """A re-opened episode keeps its first raise time, so the guard has to
    read reopened_at or a stale snapshot silently un-reports a live fault."""
    store, r = _r()
    ev, _ = store.raise_event("port.link_down", "warning", "s4048", "S4048", "Te 1/47", "t")
    ev["raised_at"] = _t(-600)
    ev["reopened_at"] = _t(0)

    r.reconcile("s4048", "S4048", _status(_t(-30), ports=[("Te 1/47", "up")]))

    assert ("resolve", "port.link_down", "Te 1/47", "ssh") not in store.log


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


# --- optics ------------------------------------------------------------------

def test_a_dark_optic_on_an_unused_port_is_not_a_fault():
    """The measurement that shaped this: the S4048 has eight optics
    reading -40 dBm with Rx-LOS and low-power alarms set, every one on a
    shut or unused port. Alarming on them would be eight instant
    criticals and nobody would look at the list again."""
    store, r = _r()
    r.reconcile("s4048", "S4048", _status(_t(-60), optics_at="p1", interfaces=[
        _iface("Te 1/13", "admin_down", DARK), _iface("Te 1/35", "down", DARK), _iface("Te 1/37", "up", HEALTHY)]))

    assert store.log == []


def test_a_live_link_losing_light_is_a_fault_and_recovers():
    store, r = _r()
    r.reconcile("s4048", "S4048", _status(_t(-60), optics_at="p1", interfaces=[_iface("Te 1/37", "up", HEALTHY)]))
    assert store.log == []

    failing = {**HEALTHY, "rx_power_dbm": -18.4, "rx_power_low_alarm_flag": True}
    r.reconcile("s4048", "S4048", _status(_t(-50), optics_at="p2", interfaces=[_iface("Te 1/37", "up", failing)]))
    assert store.log[-1] == ("raise", "optic.rx_power_low", "Te 1/37", "critical")

    r.reconcile("s4048", "S4048", _status(_t(-40), optics_at="p3", interfaces=[_iface("Te 1/37", "up", HEALTHY)]))
    assert store.log[-1] == ("resolve", "optic.rx_power_low", "Te 1/37", "ssh")


def test_light_fading_short_of_the_modules_own_alarm_still_counts():
    """The module's low alarm is the last word, not the first: a link at
    -14 dBm is failing long before an LR module shouts about it."""
    store, r = _r()
    fading = {**HEALTHY, "rx_power_dbm": -14.2}
    r.reconcile("s4048", "S4048", _status(_t(-60), optics_at="p1", interfaces=[_iface("Te 1/37", "up", fading)]))
    assert store.log[-1] == ("raise", "optic.rx_power_low", "Te 1/37", "critical")


def test_transmit_fault_and_temperature():
    store, r = _r()
    bad = {**HEALTHY, "tx_fault_state": True, "temperature_high_alarm_flag": True}
    r.reconcile("s4048", "S4048", _status(_t(-60), optics_at="p1", interfaces=[_iface("Te 1/37", "up", bad)]))
    kinds = [k for _, k, _, _ in store.log]
    assert "optic.tx_fault" in kinds and "optic.temperature" in kinds


def test_a_transceiver_disappearing_from_the_poll_is_an_event():
    store, r = _r()
    r.reconcile("s4048", "S4048", _status(_t(-60), optics_at="p1", interfaces=[_iface("Te 1/37", "up", HEALTHY)]))
    r.reconcile("s4048", "S4048", _status(_t(-50), optics_at="p2", interfaces=[_iface("Te 1/37", "down", {"present": False})]))
    assert ("raise", "optic.removed", "Te 1/37", "warning") in store.log

    r.reconcile("s4048", "S4048", _status(_t(-40), optics_at="p3", interfaces=[_iface("Te 1/37", "up", HEALTHY)]))
    assert store.log[-1] == ("resolve", "optic.removed", "Te 1/37", "ssh")


def test_optics_are_only_re_read_when_the_slow_poll_actually_ran():
    store, r = _r()
    failing = {**HEALTHY, "rx_power_low_alarm_flag": True}
    for _ in range(3):
        r.reconcile("s4048", "S4048", _status(_t(-60), optics_at="same", interfaces=[_iface("Te 1/37", "up", failing)]))
    assert len([x for x in store.log if x[0] == "raise"]) == 1


# --- error counters ----------------------------------------------------------

def test_rising_input_errors_are_a_fault_and_steady_ones_are_not():
    """Counters are cumulative: a port that logged errors once and stopped
    is healthy, a port whose count climbs is not."""
    store, r = _r()
    def poll(at, errs):
        return _status(at, interfaces=[_iface("Te 1/37", "up", None, input_errors=errs, input_discards=0,
                                              output_errors=0, output_discards=0)])
    r.reconcile("s4048", "S4048", poll(_t(-60), 4000))
    assert store.log == [], "first sight is a baseline, not a fault"

    r.reconcile("s4048", "S4048", poll(_t(-50), 4000))
    assert store.log == [], "unchanged counters are a healthy port"

    r.reconcile("s4048", "S4048", poll(_t(-40), 4085))
    assert store.log[-1] == ("raise", "port.input_errors", "Te 1/37", "warning")

    r.reconcile("s4048", "S4048", poll(_t(-30), 4085))
    assert store.log[-1] == ("resolve", "port.input_errors", "Te 1/37", "ssh")


def test_a_counter_going_backwards_is_a_reboot_not_an_error_rate():
    store, r = _r()
    def poll(at, errs):
        return _status(at, interfaces=[_iface("Te 1/37", "up", None, input_errors=errs, input_discards=0,
                                              output_errors=0, output_discards=0)])
    r.reconcile("s4048", "S4048", poll(_t(-60), 9000))
    r.reconcile("s4048", "S4048", poll(_t(-50), 3))
    assert store.log == []


def test_discards_and_output_errors_have_their_own_kinds_and_thresholds():
    store, r = _r()
    def poll(at, **c):
        return _status(at, interfaces=[_iface("Te 1/37", "up", None, **c)])
    base = dict(input_errors=0, input_discards=0, output_errors=0, output_discards=0)
    r.reconcile("s4048", "S4048", poll(_t(-60), **base))
    r.reconcile("s4048", "S4048", poll(_t(-50), **{**base, "output_errors": 50, "input_discards": 40, "output_discards": 40}))

    kinds = [k for op, k, _, _ in store.log if op == "raise"]
    assert "port.output_errors" in kinds
    assert "port.discards" not in kinds, "80 discards is under the 1000 default - congestion, not a fault"


def test_a_device_without_counters_is_left_alone():
    """The EX3300 reports no error counters at all; that is not zero."""
    store, r = _r()
    r.reconcile("ex3300", "EX3300", _status(_t(-60), interfaces=[_iface("ge-0/0/0", "up", None)]))
    r.reconcile("ex3300", "EX3300", _status(_t(-50), interfaces=[_iface("ge-0/0/0", "up", None)]))
    assert store.log == []


def test_an_ignored_port_is_ignored_for_optics_and_errors_too():
    store, r = _r(_Ports(**{"Te 1/37": "ignore"}))
    failing = {**HEALTHY, "rx_power_low_alarm_flag": True}
    r.reconcile("s4048", "S4048", _status(_t(-60), optics_at="p1",
                                          interfaces=[_iface("Te 1/37", "up", failing, input_errors=0, input_discards=0,
                                                             output_errors=0, output_discards=0)]))
    r.reconcile("s4048", "S4048", _status(_t(-50), optics_at="p2",
                                          interfaces=[_iface("Te 1/37", "up", failing, input_errors=900, input_discards=0,
                                                             output_errors=0, output_discards=0)]))
    assert store.log == []
