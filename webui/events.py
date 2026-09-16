"""In-process event bus: the one place the app says "something happened".

Webhooks and Web Push both need to know when an alarm opens, is
acknowledged, resolves, and so on. Without this each of them would need
its own hook at every one of those sites in app.py, and the third
integration would need a third. So the sites call `emit()` once, and
anything that wants events subscribes here.

Delivery is off the request thread. A webhook receiver that takes ten
seconds to answer, or a push service that is down, must not make the
Alertmanager webhook - which is on a timeout at Alertmanager's end - slow
or fail. Emitting enqueues; a single worker thread drains the queue and
calls every subscriber, catching and logging anything they raise so one
broken subscriber cannot starve the others.

Event names are dotted, lowercase, past tense: `alarm.opened`,
`alarm.acknowledged`, `alarm.resolved`, `command.ran`. The payload is a
plain dict that is safe to serialise as JSON and send outside the app - no
credentials, no session state.
"""
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("webui.events")

# The catalogue. Kept here so the webhook settings page and the docs list
# the same names the emitters actually use.
EVENTS = {
    "alarm.opened": "An occurrence was opened: the alarm is pending or firing (it may still be inside its paging hold)",
    "alarm.acknowledged": "Someone acknowledged an alarm",
    "alarm.unacknowledged": "An acknowledgement was withdrawn",
    "alarm.commented": "A comment was added to an alarm",
    "alarm.resolved": "An alarm stopped firing, or was resolved by hand",
    "alarm.paged": "The alarm went to the pager: it fired and any paging hold lapsed - this is what pages phones",
    "command.ran": "A command was run against a device and its result saved",
    "device.created": "A device was added",
    "device.updated": "A device was edited",
    "device.deleted": "A device was removed",
    "topology.drift": "Live topology differs from the saved baseline",
}


class EventBus:
    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=10000)
        self._worker = None
        self.dropped = 0
        self.delivered = 0

    def subscribe(self, fn):
        """fn(event: str, payload: dict). Called on the worker thread."""
        with self._lock:
            self._subscribers.append(fn)
        return fn

    def unsubscribe(self, fn):
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not fn]

    def emit(self, event, **payload):
        """Fire-and-forget. Never raises, never blocks the caller for more
        than the queue put; if the queue is full (a subscriber has wedged
        for a very long time) the event is dropped and counted rather than
        holding up the request that produced it."""
        if event not in EVENTS:
            log.warning("emit of unknown event %r - add it to events.EVENTS", event)
        envelope = {
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        try:
            json.dumps(envelope)          # fail here, loudly, not in a subscriber
        except (TypeError, ValueError):
            log.error("event %s payload is not JSON-serialisable: %r", event, payload)
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            self.dropped += 1
            log.error("event queue full - dropped %s (total dropped %d)", event, self.dropped)

    def _ensure_worker(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._drain, daemon=True, name="event-bus")
        self._worker.start()

    def _drain(self):
        while True:
            envelope = self._queue.get()
            with self._lock:
                subs = list(self._subscribers)
            for fn in subs:
                try:
                    fn(envelope["event"], envelope)
                except Exception:
                    log.exception("event subscriber %s failed on %s", getattr(fn, "__name__", fn), envelope["event"])
            self.delivered += 1

    def drain_now(self, timeout=5.0):
        """Test helper: wait until the queue is empty."""
        end = time.time() + timeout
        while not self._queue.empty() and time.time() < end:
            time.sleep(0.01)
        time.sleep(0.02)


BUS = EventBus()
