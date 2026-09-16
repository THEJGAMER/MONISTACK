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

2. The events themselves are raised straight into the event store by
   event_detect.SyslogDetector (and by event_reconcile.SshReconciler for
   the SSH fallback) - there is no Alertmanager, no webhook round trip,
   nothing between a line arriving and a phone buzzing but this process.
"""
import json
import logging
import re
import statistics
import threading
import time
from collections import deque
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
        self.last_by_host = {}               # device_host/source_ip -> last received_at (for "syslog silent")
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
                for h in (e.get("device_host"), e.get("source_ip"), e.get("host")):
                    if h:
                        self.last_by_host[str(h)] = received_at
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
