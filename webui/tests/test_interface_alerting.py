"""Tests for interface_alerting.py - the interface-down detection and
paging path. Previously the largest untested module in the app (470 lines,
zero tests) despite carrying every InterfaceDown page.

Almost every case here pins a behaviour whose docstring records a real,
live-diagnosed bug. Those are called out individually, because the reason
each rule exists is the thing most likely to be "simplified" away later:

  - immediate mode must never resolve from the SSH-poll path (a stale
    mid-transition read falsely un-alerted a real outage)
  - the syslog path must fire unconditionally, not gated on "already
    alerting" (a false fire from the poll path swallowed the real event)
  - reconcile_via_poll must only ever act on a *fresh* poll snapshot
  - ...and must re-arm regardless of in-memory state, because that state
    is wiped by a restart and a restart mid-outage orphaned a live page

The checker's timing is `time.monotonic`-based, so tests that care about
elapsed time drive a fake clock rather than sleeping.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import interface_alerting
from interface_alerting import InterfaceAlertChecker


class _FakeAlertmanager:
    def __init__(self):
        self.posted = []

    def post_alerts(self, alerts):
        self.posted.extend(alerts)

    def list_alerts(self):
        return []

    @property
    def fires(self):
        return [a for a in self.posted if "endsAt" not in a]

    @property
    def resolves(self):
        return [a for a in self.posted if "endsAt" in a]


class _Device:
    def __init__(self, id_, host):
        self.id = id_
        self.host = host


@pytest.fixture
def clock(monkeypatch):
    """Drives interface_alerting's monotonic clock so delay/heartbeat
    windows can be crossed deterministically."""
    class _Clock:
        def __init__(self):
            self.t = 1000.0

        def advance(self, seconds):
            self.t += seconds

    c = _Clock()
    monkeypatch.setattr(interface_alerting.time, "monotonic", lambda: c.t)
    return c


@pytest.fixture
def am():
    return _FakeAlertmanager()


def _cfg(mode="immediate", enabled=True, delay=60, device="dev1", port="Te 1/1"):
    return {"device_id": device, "port": port, "enabled": enabled,
            "mode": mode, "delay_seconds": delay, "severity": "warning"}


def _name(device_id):
    return device_id


# --- check_once: delayed mode ---------------------------------------

def test_delayed_mode_does_not_fire_before_the_delay(clock, am):
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="delayed", delay=60)]

    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    clock.advance(59)
    checker.check_once(cfgs, lambda d, p: "down", _name, am)

    assert am.fires == []


def test_delayed_mode_fires_once_the_delay_elapses(clock, am):
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="delayed", delay=60)]

    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    clock.advance(61)
    checker.check_once(cfgs, lambda d, p: "down", _name, am)

    assert len(am.fires) == 1
    assert am.fires[0]["labels"]["alertname"] == "InterfaceDown"


def test_delayed_mode_recovering_before_the_delay_never_fires(clock, am):
    """The whole point of delayed mode - a short flap must not page."""
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="delayed", delay=60)]

    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    clock.advance(30)
    checker.check_once(cfgs, lambda d, p: "up", _name, am)
    clock.advance(60)
    checker.check_once(cfgs, lambda d, p: "down", _name, am)

    assert am.fires == [], "the down_since clock must restart after a recovery"


def test_delayed_mode_resolves_when_the_port_comes_back(clock, am):
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="delayed", delay=60)]

    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    clock.advance(61)
    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    checker.check_once(cfgs, lambda d, p: "up", _name, am)

    assert len(am.resolves) == 1


# --- check_once: immediate mode -------------------------------------

def test_immediate_mode_fires_on_the_first_down_seen(clock, am):
    checker = InterfaceAlertChecker()

    checker.check_once([_cfg(mode="immediate")], lambda d, p: "down", _name, am)

    assert len(am.fires) == 1


def test_immediate_mode_never_resolves_from_the_poll_path(clock, am):
    """The real race this split exists for: check_via_syslog correctly
    fired 3s after a genuine down event, then this loop's next SSH poll
    read a stale "up" from mid-transition and incorrectly resolved it.
    Resolving here is the hazard - a stale read silently un-alerts a real
    ongoing outage - so immediate mode is fire-only here, and resolve
    belongs to the syslog and fresh-poll paths alone."""
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="immediate")]

    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    checker.check_once(cfgs, lambda d, p: "up", _name, am)  # stale read

    assert am.resolves == [], "immediate mode must not resolve from check_once"


def test_admin_down_is_never_alerted(clock, am):
    """A deliberately shut port is not a fault."""
    checker = InterfaceAlertChecker()

    checker.check_once([_cfg(mode="immediate")], lambda d, p: "admin_down", _name, am)
    checker.check_once([_cfg(mode="delayed", delay=0)], lambda d, p: "admin_down", _name, am)

    assert am.posted == []


def test_disabling_a_port_mid_alert_resolves_it_immediately(clock, am):
    """Disabling must resolve regardless of mode - the fast path only
    resolves on a real "up" event, which disabling never produces."""
    checker = InterfaceAlertChecker()

    checker.check_once([_cfg(mode="immediate")], lambda d, p: "down", _name, am)
    assert len(am.fires) == 1

    checker.check_once([_cfg(mode="immediate", enabled=False)], lambda d, p: "down", _name, am)

    assert len(am.resolves) == 1


# --- heartbeat -------------------------------------------------------

def test_an_ongoing_alert_is_reposted_only_on_the_heartbeat_interval(clock, am):
    """Alertmanager's resolve_timeout expires a directly-posted alert that
    is never refreshed, so an ongoing alert must be re-POSTed - but on the
    HEARTBEAT_SECONDS cadence, not on every tick."""
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="immediate")]

    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    for _ in range(10):
        clock.advance(5)
        checker.check_once(cfgs, lambda d, p: "down", _name, am)
    assert len(am.fires) == 1, "50s of ticks is inside the 120s heartbeat window"

    clock.advance(interface_alerting.InterfaceAlertChecker.HEARTBEAT_SECONDS + 1)
    checker.check_once(cfgs, lambda d, p: "down", _name, am)

    assert len(am.fires) == 2


# --- check_via_syslog ------------------------------------------------

def _event(ts_ns, iface, state, host="10.0.0.1"):
    return {"_timestamp_ns": ts_ns, "interface": iface, "link_state": state, "source_ip": host}


class _FakeLoki:
    def __init__(self, events):
        self.events = events

    def query_range(self, filters, limit, since_seconds):
        return self.events


def test_syslog_down_event_fires(clock, am):
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}

    checker.check_via_syslog([_cfg()], _FakeLoki([_event(100, "Te 1/1", "down")]), devices, am, _name)

    assert len(am.fires) == 1


def test_syslog_up_event_resolves(clock, am):
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}

    checker.check_via_syslog([_cfg()], _FakeLoki([_event(100, "Te 1/1", "down")]), devices, am, _name)
    checker.check_via_syslog([_cfg()], _FakeLoki([_event(200, "Te 1/1", "up")]), devices, am, _name)

    assert len(am.resolves) == 1


def test_syslog_events_are_not_reprocessed(clock, am):
    """Log lines don't leave the query window between ticks, so the
    timestamp cursor is what stops one event firing repeatedly."""
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}
    loki = _FakeLoki([_event(100, "Te 1/1", "down")])

    checker.check_via_syslog([_cfg()], loki, devices, am, _name)
    checker.check_via_syslog([_cfg()], loki, devices, am, _name)
    checker.check_via_syslog([_cfg()], loki, devices, am, _name)

    assert len(am.fires) == 1


def test_syslog_fires_even_when_already_alerting(clock, am):
    """Deliberately not gated on self._alerting. During rapid flapping the
    poll path can set _alerting from a stale sample with nothing real
    behind it; a genuine new down event arriving here gated on "not
    already alerting" would be silently swallowed - a real outage with no
    alert at all."""
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}

    checker.check_once([_cfg()], lambda d, p: "down", _name, am)  # sets _alerting
    assert len(am.fires) == 1

    checker.check_via_syslog([_cfg()], _FakeLoki([_event(100, "Te 1/1", "down")]), devices, am, _name)

    assert len(am.fires) == 2, "a real syslog down event must always re-post"


def test_syslog_ignores_delayed_mode_configs(clock, am):
    """Delayed mode's whole meaning is sustained-down tracking, which only
    check_once does."""
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}

    checker.check_via_syslog([_cfg(mode="delayed")], _FakeLoki([_event(100, "Te 1/1", "down")]),
                             devices, am, _name)

    assert am.posted == []


def test_syslog_ignores_events_from_unknown_hosts_and_ports(clock, am):
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}
    events = [
        _event(100, "Te 1/1", "down", host="10.9.9.9"),  # unknown device
        _event(101, "Te 9/9", "down"),                    # port not configured
        _event(102, "Te 1/1", "flapping"),                # not a link state
    ]

    checker.check_via_syslog([_cfg()], _FakeLoki(events), devices, am, _name)

    assert am.posted == []


def test_syslog_survives_loki_being_unreachable(clock, am):
    """Loki down must degrade to "no fast detection", not raise into the
    caller's loop."""
    class _Broken:
        def query_range(self, **kw):
            raise RuntimeError("loki down")

    checker = InterfaceAlertChecker()
    checker.check_via_syslog([_cfg()], _Broken(), {}, am, _name)

    assert am.posted == []


# --- reconcile_via_poll ---------------------------------------------

def test_reconcile_resolves_a_missed_syslog_up(clock, am):
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="immediate")]
    checker.check_once(cfgs, lambda d, p: "down", _name, am)

    checker.reconcile_via_poll(cfgs, lambda d, p: ("up", "t1"), _name, am)

    assert len(am.resolves) == 1


def test_reconcile_ignores_a_stale_unchanged_snapshot(clock, am):
    """The stale-read hazard this method was carefully designed around: it
    may only ever act on a poll timestamp that has actually advanced."""
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="immediate")]
    checker.check_once(cfgs, lambda d, p: "down", _name, am)

    for _ in range(5):
        checker.reconcile_via_poll(cfgs, lambda d, p: ("up", "same-timestamp"), _name, am)

    assert len(am.resolves) == 1, "only the first genuinely-new snapshot may resolve"


def test_reconcile_does_not_refire_on_an_unchanged_down_snapshot(clock, am):
    """The freshness check's load-bearing direction, and the one the
    resolve case above cannot prove: after re-arming from a poll, the
    *same* snapshot must not keep re-firing every ~5s tick.

    Written after mutation-testing showed the resolve-only version of this
    test still passed with the freshness check deleted entirely - resolve
    is naturally idempotent (it discards the key), so it can't detect a
    missing guard. Re-arm can, because nothing else stops it repeating."""
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="immediate")]

    for _ in range(5):
        checker.reconcile_via_poll(cfgs, lambda d, p: ("down", "same-timestamp"), _name, am)

    assert len(am.fires) == 1, "an unchanged snapshot must not re-fire on every tick"


def test_reconcile_acts_again_once_the_snapshot_genuinely_advances(clock, am):
    """The other half - the guard must not freeze the port permanently:
    a genuinely new poll result is still acted on."""
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="immediate")]

    checker.reconcile_via_poll(cfgs, lambda d, p: ("down", "t1"), _name, am)
    assert len(am.fires) == 1

    checker.reconcile_via_poll(cfgs, lambda d, p: ("up", "t2"), _name, am)

    assert len(am.resolves) == 1


def test_a_poll_older_than_the_alert_cannot_resolve_it(clock, am):
    """The bug this file found. reconcile_via_poll's docstring promises "a
    snapshot from before the alert even started can never trigger this" -
    it didn't hold. Clearing `_last_seen_poll_at` on fire left the next
    tick with no baseline, so it accepted the first snapshot it saw
    whatever its age.

    The realistic case is the dangerous one: a syslog "down" fires within
    ~3s, while the SSH poller's last successful cycle can be ~30s old and
    still read "up" - so reconcile resolved a live outage seconds after it
    was correctly detected, exactly the stale-read hazard the whole
    fire/resolve split exists to prevent."""
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}
    stale_poll = "2020-01-01T00:00:00+00:00"  # long before the alert

    checker.check_via_syslog([_cfg()], _FakeLoki([_event(100, "Te 1/1", "down")]), devices, am, _name)
    assert ("dev1", "Te 1/1") in checker._alerting

    checker.reconcile_via_poll([_cfg()], lambda d, p: ("up", stale_poll), _name, am)

    assert am.resolves == [], "a pre-alert snapshot must not resolve a live alert"
    assert ("dev1", "Te 1/1") in checker._alerting


def test_a_poll_newer_than_the_alert_still_resolves_it(clock, am):
    """The guard must not make alerts unresolvable - a genuinely newer
    poll showing recovery is still the missed-syslog safety net."""
    from datetime import datetime, timedelta, timezone

    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}
    checker.check_via_syslog([_cfg()], _FakeLoki([_event(100, "Te 1/1", "down")]), devices, am, _name)

    fresh = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    checker.reconcile_via_poll([_cfg()], lambda d, p: ("up", fresh), _name, am)

    assert len(am.resolves) == 1


def test_an_unreadable_poll_timestamp_does_not_wedge_the_alert(clock, am):
    """A format this can't parse must mean "no opinion", not "block
    forever" - an unresolvable alert pages until resolve_timeout."""
    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}
    checker.check_via_syslog([_cfg()], _FakeLoki([_event(100, "Te 1/1", "down")]), devices, am, _name)

    checker.reconcile_via_poll([_cfg()], lambda d, p: ("up", "not-a-timestamp"), _name, am)

    assert len(am.resolves) == 1


def test_the_episode_stamp_is_cleared_so_it_cannot_block_the_next_episode(clock, am):
    """A stale stamp left over from a resolved episode would make the
    *next* outage's recovery unresolvable."""
    from datetime import datetime, timedelta, timezone

    checker = InterfaceAlertChecker()
    devices = {"dev1": _Device("dev1", "10.0.0.1")}

    checker.check_via_syslog([_cfg()], _FakeLoki([_event(100, "Te 1/1", "down")]), devices, am, _name)
    checker.check_via_syslog([_cfg()], _FakeLoki([_event(200, "Te 1/1", "up")]), devices, am, _name)
    assert ("dev1", "Te 1/1") not in checker._alert_started_at

    # A second, later outage - resolved by a poll newer than *it*.
    checker.check_via_syslog([_cfg()], _FakeLoki([_event(300, "Te 1/1", "down")]), devices, am, _name)
    fresh = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    checker.reconcile_via_poll([_cfg()], lambda d, p: ("up", fresh), _name, am)

    assert len(am.resolves) == 2


def test_reseeded_alerts_can_still_be_resolved_by_a_current_poll(clock, am):
    """A reseeded episode began before this process did, so any poll the
    poller has since taken genuinely postdates it and must be allowed to
    resolve - otherwise a restart makes live alerts unresolvable."""
    class _AM(_FakeAlertmanager):
        def list_alerts(self):
            return [{"labels": {"alertname": "InterfaceDown", "device_id": "dev1", "port": "Te 1/1"},
                     "status": {"state": "active"}}]

    checker = InterfaceAlertChecker()
    am2 = _AM()
    checker.reseed_from_alertmanager(am2)
    assert ("dev1", "Te 1/1") in checker._alerting

    checker.reconcile_via_poll([_cfg()], lambda d, p: ("up", "2026-01-01T00:00:00+00:00"), _name, am2)

    assert len(am2.resolves) == 1


def test_reconcile_re_arms_after_state_loss(clock, am):
    """A restart mid-outage wipes the in-memory tracking and orphaned a
    real, still-firing Alertmanager alert - nothing heartbeated it, so it
    paged until resolve_timeout silently expired it. A fresh poll showing
    "down" must re-establish the alert regardless of bookkeeping."""
    checker = InterfaceAlertChecker()  # fresh process: no memory of anything

    checker.reconcile_via_poll([_cfg(mode="immediate")], lambda d, p: ("down", "t1"), _name, am)

    assert len(am.fires) == 1
    assert ("dev1", "Te 1/1") in checker._alerting


def test_reconcile_skips_delayed_and_disabled_configs(clock, am):
    checker = InterfaceAlertChecker()

    checker.reconcile_via_poll([_cfg(mode="delayed")], lambda d, p: ("down", "t1"), _name, am)
    checker.reconcile_via_poll([_cfg(enabled=False)], lambda d, p: ("down", "t1"), _name, am)

    assert am.posted == []


def test_reconcile_ignores_a_device_that_has_never_polled(clock, am):
    checker = InterfaceAlertChecker()

    checker.reconcile_via_poll([_cfg()], lambda d, p: (None, None), _name, am)

    assert am.posted == []


# --- forget ----------------------------------------------------------

def test_forget_clears_tracking_but_does_not_suppress(clock, am):
    """A manual resolve is meant to be temporary: if the fault is still
    real, the next fresh poll re-arms it. The tool for "stop telling me"
    is a time-boxed silence, not a permanent hide."""
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="immediate")]
    checker.check_once(cfgs, lambda d, p: "down", _name, am)

    checker.forget("dev1", "Te 1/1")
    assert ("dev1", "Te 1/1") not in checker._alerting

    checker.reconcile_via_poll(cfgs, lambda d, p: ("down", "t-new"), _name, am)

    assert len(am.fires) == 2, "a still-real fault must re-fire after forget"


# --- pending_entries -------------------------------------------------

def test_pending_entries_reports_a_delayed_port_mid_countdown(clock, am):
    checker = InterfaceAlertChecker()
    cfgs = [_cfg(mode="delayed", delay=60)]
    checker.check_once(cfgs, lambda d, p: "down", _name, am)
    clock.advance(30)

    pending = checker.pending_entries(cfgs, _name)

    assert len(pending) == 1
    assert pending[0]["status"]["state"] == "pending"
    assert pending[0]["labels"]["port"] == "Te 1/1"


def test_pending_entries_excludes_immediate_mode_and_already_fired(clock, am):
    checker = InterfaceAlertChecker()

    immediate = [_cfg(mode="immediate")]
    checker.check_once(immediate, lambda d, p: "down", _name, am)
    assert checker.pending_entries(immediate, _name) == [], "immediate mode has no pending phase"

    delayed = [_cfg(mode="delayed", delay=60, port="Te 1/2")]
    checker.check_once(delayed, lambda d, p: "down", _name, am)
    clock.advance(61)
    checker.check_once(delayed, lambda d, p: "down", _name, am)
    assert checker.pending_entries(delayed, _name) == [], "already fired is not pending"


# --- reseed_from_alertmanager ----------------------------------------

def test_reseed_restores_tracking_for_still_active_alerts(clock, am):
    class _AM(_FakeAlertmanager):
        def list_alerts(self):
            return [
                {"labels": {"alertname": "InterfaceDown", "device_id": "dev1", "port": "Te 1/1"},
                 "status": {"state": "active"}},
                {"labels": {"alertname": "InterfaceDown", "device_id": "dev1", "port": "Te 1/2"},
                 "status": {"state": "suppressed"}},          # not active
                {"labels": {"alertname": "HardwareAlarm", "device_id": "dev1"},
                 "status": {"state": "active"}},              # not ours
            ]

    checker = InterfaceAlertChecker()
    checker.reseed_from_alertmanager(_AM())

    assert checker._alerting == {("dev1", "Te 1/1")}


def test_reseed_survives_alertmanager_being_down(clock, am):
    class _Broken:
        def list_alerts(self):
            raise RuntimeError("unreachable")

    checker = InterfaceAlertChecker()
    checker.reseed_from_alertmanager(_Broken())  # must not raise

    assert checker._alerting == set()


# --- posting failures must not break the loop ------------------------

def test_a_failing_alertmanager_post_does_not_raise(clock):
    """The checker runs inside a background loop; an Alertmanager blip
    must not kill the thread that would otherwise recover on the next
    tick."""
    class _Broken:
        def post_alerts(self, alerts):
            raise RuntimeError("alertmanager down")

    checker = InterfaceAlertChecker()
    checker.check_once([_cfg(mode="immediate")], lambda d, p: "down", _name, _Broken())
