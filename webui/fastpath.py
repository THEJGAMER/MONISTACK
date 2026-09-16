"""The syslog fast path: events evaluated on arrival, alarms raised here first.

What PROXMON does with its agent - watch cheap fault signals, report the
instant one changes, evaluate on arrival, page within a second - a switch
already does for us: it logs a link change, a PSU fault or a protocol
event to syslog the moment it happens. Until now that line took the long
way round: Vector -> Loki -> a 3-second poll -> Alertmanager -> its
dispatch -> our webhook -> an occurrence -> a page. Several seconds on a
good day, and a Loki hiccup made it minutes.

Two pieces here shorten that to well under a second:

1. `parse_events` / `FastPathStats` - Vector's `http` sink POSTs every
   interpreted event to /api/ingest/syslog as it arrives (batch timeout
   50 ms), carrying the same structured fields the Loki archive gets.
   The app evaluates each one on arrival with the very same checkers the
   Loki poll feeds; the poll stays as the safety net and dedups against
   the fast path through the checkers' timestamp cursor, so exactness of
   `timestamp_ns` matters (see it).

2. `LocalFirstAlertmanager` - every alert this app raises itself (interface
   down, fan/PSU, syslog rules, manual resolve) goes through
   `post_alerts`. Wrapping the client makes the occurrence - and the page
   that follows from it - happen *here, now*, and only then forwards the
   alert to Alertmanager for its other receivers. Alertmanager's own
   round trip (dispatch, webhook) still happens; it just no longer sits
   between a fault and a phone. If Alertmanager is down, the page still
   goes out.

`signal` / `attributed` carry *how* an alarm was detected into the
occurrence row (`detected_via`, `signal_at`) so the alarm can say
"detected via syslog 0.4 s after the switch logged it" - the number that
proves the path is short, rather than a claim that it is.
"""
import contextvars
import json
import logging
import re
import statistics
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger("webui.fastpath")

SELFTEST_MNEMONIC = "SWITCHBOARD_SELFTEST"

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|z|[+-]\d{2}:?\d{2})?$")


def timestamp_ns(value):
    """Exact nanoseconds for an RFC 3339 timestamp, or 0.

    Exact, not "close": Loki stores Vector's `timestamp` to the nanosecond
    and the checkers dedup the fast path against the Loki poll by
    comparing the two. Rounding to microseconds here would make every
    fast-path event look new to the poll and fire twice."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10**15 else v * 1_000_000_000
    m = _TS_RE.match(str(value).strip())
    if not m:
        return 0
    date, clock, frac, tz = m.groups()
    try:
        if tz in (None, "Z", "z"):
            dt = datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=timezone.utc)
        else:
            tz = tz if ":" in tz else f"{tz[:3]}:{tz[3:]}"
            dt = datetime.fromisoformat(f"{date}T{clock}{tz}")
    except ValueError:
        return 0
    frac_ns = int((frac or "0")[:9].ljust(9, "0"))
    return int(dt.timestamp()) * 1_000_000_000 + frac_ns


def parse_events(raw, content_type=""):
    """Vector's http sink sends a JSON array per batch (codec json); a
    single object and newline-delimited JSON are accepted too so a curl
    from the installer's connection test, or another shipper, works the
    same way. Anything that is not a list of objects is a ValueError."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = text.strip()
    if not text:
        return []
    events = None
    if "ndjson" in (content_type or "") or (text[0] == "{" and "\n" in text):
        try:
            events = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            events = None
    if events is None:
        parsed = json.loads(text)
        events = parsed if isinstance(parsed, list) else [parsed]
    if not all(isinstance(e, dict) for e in events):
        raise ValueError("events must be JSON objects")
    for e in events:
        e["_timestamp_ns"] = timestamp_ns(e.get("timestamp")) or time.time_ns()
    return events


class FastPathStats:
    """What the health panel and the Fast path card show: is anything
    arriving, how much, and how long Vector -> Switchboard takes. Kept in
    memory; a restart simply starts counting again."""

    def __init__(self, window=200):
        self._lock = threading.Lock()
        self.total = 0
        self.last_received_at = None
        self.last_host = None
        self._recent = deque()               # receive times, for events-per-minute
        self._transport_ms = deque(maxlen=window)
        self._selftests = {}                 # nonce -> received_at (monotonic + wall)
        self.last_test = None

    def record(self, events, received_at=None):
        received_at = received_at or datetime.now(timezone.utc)
        now_ns = int(received_at.timestamp() * 1e9)
        with self._lock:
            self.total += len(events)
            self.last_received_at = received_at
            for e in events:
                self.last_host = e.get("device_host") or e.get("host") or self.last_host
                ts = int(e.get("_timestamp_ns") or 0)
                if ts:
                    ms = (now_ns - ts) / 1e6
                    if -1000 <= ms <= 60_000:
                        self._transport_ms.append(ms)
                # Wherever the parser put it: the mnemonic may land in the
                # message, the detail, or (a BSD-shaped line) the app name.
                msg = " ".join(str(e.get(k) or "") for k in ("message", "detail", "appname", "mnemonic"))
                if SELFTEST_MNEMONIC in msg:
                    m = re.search(r"nonce=([A-Za-z0-9]+)", msg)
                    if m:
                        self._selftests[m.group(1)] = received_at
                        if len(self._selftests) > 50:
                            self._selftests.pop(next(iter(self._selftests)))
            self._recent.append(time.monotonic())
            cutoff = time.monotonic() - 60
            while self._recent and self._recent[0] < cutoff:
                self._recent.popleft()

    def selftest_received_at(self, nonce):
        with self._lock:
            return self._selftests.get(nonce)

    def snapshot(self):
        with self._lock:
            ms = list(self._transport_ms)
            return {
                "total": self.total,
                "last_received_at": self.last_received_at.isoformat() if self.last_received_at else None,
                "last_host": self.last_host,
                "events_last_minute": len(self._recent),
                "transport_ms_median": round(statistics.median(ms), 1) if ms else None,
                "transport_ms_p95": round(sorted(ms)[int(len(ms) * 0.95) - 1 if len(ms) > 1 else 0], 1) if ms else None,
                "last_test": self.last_test,
            }


# --- how was this alarm detected? ---------------------------------------

CURRENT_SIGNAL = contextvars.ContextVar("fastpath_signal", default=None)


@contextmanager
def signal(event, via="syslog"):
    """Run a checker for one syslog event with the event's timing in scope,
    so an occurrence opened as a result records when the device logged the
    signal and by which path it was detected."""
    token = CURRENT_SIGNAL.set({
        "via": via,
        "signal_at": event.get("device_timestamp") or event.get("timestamp"),
    })
    try:
        yield
    finally:
        CURRENT_SIGNAL.reset(token)


@contextmanager
def attributed(via):
    """Same, for paths that have no single event: the Loki poll ("loki"),
    an SSH poll ("poll"), or a person resolving by hand (their name)."""
    token = CURRENT_SIGNAL.set({"via": via, "signal_at": None})
    try:
        yield
    finally:
        CURRENT_SIGNAL.reset(token)


def _iso_to_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class LocalFirstAlertmanager:
    """Alertmanager client wrapper: an alert we raise becomes an occurrence
    here first (and pages), then goes to Alertmanager for its receivers.

    `post_alerts` is the one method every checker uses to fire, heartbeat
    and resolve, so this is the single place the local-first behaviour
    lives; everything else is passed straight through. A heartbeat
    (re-posting an alert that is already open) is harmless: open() is
    idempotent and mark_paged fires exactly once. An alert with an
    `endsAt` in the past is a resolve and closes the occurrence at once -
    a link that came back up stops paging the moment the switch says so,
    not when Alertmanager's resolved notification eventually lands.

    Paging holds are respected: an occurrence someone delayed (page_at
    set) or turned off (NARG) is not marked paged here."""

    def __init__(self, inner, occurrences, fingerprint_for):
        self.inner = inner
        self._occurrences = occurrences          # callable -> OccurrenceStore or None
        self._fingerprint_for = fingerprint_for
        self.local_opens = 0
        self.local_closes = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def post_alerts(self, alerts):
        for alert in alerts or []:
            try:
                self._apply_locally(alert)
            except Exception:
                log.exception("local-first alarm handling failed for %s", (alert.get("labels") or {}).get("alertname"))
        return self.inner.post_alerts(alerts)

    def _apply_locally(self, alert):
        store = self._occurrences() if callable(self._occurrences) else self._occurrences
        if store is None:
            return
        labels = alert.get("labels") or {}
        signature = self._fingerprint_for(labels)
        ctx = CURRENT_SIGNAL.get() or {}
        ends = _iso_to_dt(alert.get("endsAt"))
        if ends is not None and ends <= datetime.now(timezone.utc):
            if store.close(signature, by=ctx.get("via") or "switchboard"):
                self.local_closes += 1
            return
        annotations = alert.get("annotations") or {}
        occ = store.open(
            signature, labels.get("alertname", "unknown"), labels.get("severity"), annotations.get("summary"),
            labels, started_at=alert.get("startsAt"),
            detected_via=ctx.get("via") or "poll", signal_at=ctx.get("signal_at"),
        )
        if occ is None:
            return
        store.touch(signature)
        if occ.get("paged_at") is None and not occ.get("paging_disabled") and occ.get("page_at") is None:
            store.mark_paged(occ["id"])
            self.local_opens += 1


# --- the self-test line -------------------------------------------------

def selftest_line(nonce, sender="switchboard", severity_digit=4, when=None):
    """An RFC 5424 syslog line whose MSG is Dell OS9-shaped, so Vector's
    interpreter parses it like a switch message (facility SWB, mnemonic
    SWITCHBOARD_SELFTEST). RFC 5424 rather than BSD on purpose: with the
    BSD form Vector's parser takes the leading `%SWB-4-...:` token as the
    app name and leaves it out of the message, so nothing downstream
    could see the mnemonic (confirmed live on the first test). PRI 190 =
    local7.info."""
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    return (f"<190>1 {stamp} {sender} {sender} - - - "
            f"%SWB-{severity_digit}-{SELFTEST_MNEMONIC}: Fast-path self-test nonce={nonce}")
