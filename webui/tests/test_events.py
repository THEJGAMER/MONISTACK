"""The event bus: one emit, every subscriber, never on the caller's thread.

The property that matters for the Alertmanager webhook (which is on a
timeout at Alertmanager's end): emit() returns immediately whatever the
subscribers do, and a subscriber that raises costs nobody else their
delivery.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import events  # noqa: E402


def test_emit_reaches_every_subscriber_with_an_envelope():
    bus = events.EventBus()
    got_a, got_b = [], []
    bus.subscribe(lambda ev, env: got_a.append((ev, env)))
    bus.subscribe(lambda ev, env: got_b.append((ev, env)))

    bus.emit("alarm.opened", occurrence={"id": 1})
    bus.drain_now()

    assert got_a[0][0] == "alarm.opened" and got_b[0][0] == "alarm.opened"
    env = got_a[0][1]
    assert env["event"] == "alarm.opened" and env["occurrence"] == {"id": 1} and "at" in env


def test_delivery_is_off_the_callers_thread():
    bus = events.EventBus()
    seen_thread = []
    bus.subscribe(lambda ev, env: seen_thread.append(threading.current_thread().name))

    bus.emit("alarm.opened")
    bus.drain_now()

    assert seen_thread and seen_thread[0] != threading.current_thread().name


def test_a_slow_subscriber_does_not_slow_emit():
    bus = events.EventBus()
    bus.subscribe(lambda ev, env: time.sleep(0.5))

    t = time.time()
    bus.emit("alarm.opened")

    assert time.time() - t < 0.1


def test_a_raising_subscriber_does_not_starve_the_others():
    bus = events.EventBus()
    got = []

    def bad(ev, env):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda ev, env: got.append(ev))

    bus.emit("alarm.opened")
    bus.emit("alarm.resolved")
    bus.drain_now()

    assert got == ["alarm.opened", "alarm.resolved"]


def test_a_non_serialisable_payload_is_dropped_loudly_not_delivered():
    bus = events.EventBus()
    got = []
    bus.subscribe(lambda ev, env: got.append(ev))

    bus.emit("alarm.opened", occurrence=object())
    bus.drain_now()

    assert got == []


def test_unsubscribe_stops_delivery():
    bus = events.EventBus()
    got = []
    fn = bus.subscribe(lambda ev, env: got.append(ev))
    bus.unsubscribe(fn)

    bus.emit("alarm.opened")
    bus.drain_now()

    assert got == []


def test_the_catalogue_names_every_event_the_app_emits():
    """Emitters and the webhook settings page read the same dict; an
    event that is emitted but not catalogued cannot be subscribed to."""
    import re
    src = (Path(__file__).parent.parent / "app.py").read_text()
    emitted = set(re.findall(r'events\.BUS\.emit\("([a-z_.]+)"', src))

    assert emitted, "no emits found - the grep pattern drifted"
    assert emitted <= set(events.EVENTS), emitted - set(events.EVENTS)
