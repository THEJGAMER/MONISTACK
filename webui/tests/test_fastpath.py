"""The syslog fast path: what arrives is parsed exactly, counted honestly,
and an alert we raise becomes an occurrence - and a page - here, before
Alertmanager ever hears of it.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import fastpath  # noqa: E402


# --- timestamps -----------------------------------------------------

def test_nanoseconds_are_exact_not_rounded():
    """Loki keeps Vector's timestamp to the nanosecond and the checkers'
    dedup cursor compares the two paths for equality."""
    whole = int(datetime(2026, 9, 16, 8, 22, 30, tzinfo=timezone.utc).timestamp())
    assert fastpath.timestamp_ns("2026-09-16T08:22:30.123456789Z") == whole * 1_000_000_000 + 123456789


def test_timestamp_forms():
    base = fastpath.timestamp_ns("2026-09-16T08:22:30Z")
    assert base % 1_000_000_000 == 0
    assert fastpath.timestamp_ns("2026-09-16T08:22:30.5Z") == base + 500_000_000
    assert fastpath.timestamp_ns("2026-09-16T10:22:30+02:00") == base, "offsets are honoured"
    assert fastpath.timestamp_ns("2026-09-16T08:22:30.000001+0000") == base + 1000
    assert fastpath.timestamp_ns("garbage") == 0 and fastpath.timestamp_ns(None) == 0
    assert fastpath.timestamp_ns(1789719750) == 1789719750 * 1_000_000_000


# --- parsing what Vector sends ----------------------------------------

def test_a_vector_batch_is_a_json_array():
    events = fastpath.parse_events(b'[{"message": "a", "timestamp": "2026-09-16T08:22:30.5Z"}, {"message": "b"}]')
    assert [e["message"] for e in events] == ["a", "b"]
    assert events[0]["_timestamp_ns"] % 1_000_000_000 == 500_000_000
    assert events[1]["_timestamp_ns"] > 0, "no timestamp: now, so the cursor still advances"


def test_single_object_and_ndjson_are_accepted_too():
    assert len(fastpath.parse_events(b'{"message": "one"}')) == 1
    assert len(fastpath.parse_events(b'{"message": "one"}\n{"message": "two"}\n', "application/x-ndjson")) == 2
    assert fastpath.parse_events(b"   ") == []


def test_non_objects_are_refused():
    with pytest.raises(ValueError):
        fastpath.parse_events(b'[1, 2]')
    with pytest.raises(ValueError):
        fastpath.parse_events(b'not json')


# --- stats ----------------------------------------------------------

def test_stats_count_rate_transport_latency_and_selftests():
    st = fastpath.FastPathStats()
    now = datetime(2026, 9, 16, 8, 22, 31, tzinfo=timezone.utc)
    ts = (now - timedelta(milliseconds=40)).isoformat()
    st.record(fastpath.parse_events(f'[{{"message": "x", "device_host": "S4048", "timestamp": "{ts}"}}, '
                                    f'{{"message": "%SWB-4-SWITCHBOARD_SELFTEST: nonce=abc123", "timestamp": "{ts}"}}]'.encode()),
              received_at=now)
    snap = st.snapshot()
    assert snap["total"] == 2 and snap["events_last_minute"] == 1 and snap["last_host"] == "S4048"
    assert 39 <= snap["transport_ms_median"] <= 41
    assert st.selftest_received_at("abc123") == now
    assert st.selftest_received_at("nope") is None


# --- local-first alarms --------------------------------------------------

class _Store:
    """Just enough of OccurrenceStore to see what the wrapper does."""
    def __init__(self):
        self.rows = {}
        self.calls = []

    def open(self, signature, alertname, severity, summary, labels, started_at=None, detected_via=None, signal_at=None):
        self.calls.append(("open", signature, detected_via, signal_at))
        row = self.rows.get(signature)
        if row is None or row["resolved_at"]:
            row = {"id": len(self.rows) + 1, "signature": signature, "paged_at": None, "paging_disabled": 0,
                   "page_at": None, "resolved_at": None}
            self.rows[signature] = row
        return dict(row)

    def touch(self, signature):
        self.calls.append(("touch", signature))

    def mark_paged(self, occ_id):
        self.calls.append(("paged", occ_id))
        for r in self.rows.values():
            if r["id"] == occ_id:
                r["paged_at"] = "now"

    def close(self, signature, resolved_at=None, by=None):
        self.calls.append(("close", signature, by))
        row = self.rows.get(signature)
        if row and not row["resolved_at"]:
            row["resolved_at"] = "now"
            return dict(row)
        return None


class _AM:
    def __init__(self, fail=False):
        self.posted = []
        self.fail = fail

    def post_alerts(self, alerts):
        if self.fail:
            raise RuntimeError("alertmanager down")
        self.posted.extend(alerts)

    def list_alerts(self):
        return ["passthrough"]


def _fp(labels):
    return "sig-" + labels["alertname"]


def _fire(name="InterfaceDown", **extra):
    return {"labels": {"alertname": name, "severity": "critical", **extra},
            "annotations": {"summary": "Te 1/1 is down"}, "startsAt": "2026-09-16T08:22:30+00:00"}


def _resolve(name="InterfaceDown"):
    now = datetime.now(timezone.utc)
    return {"labels": {"alertname": name, "severity": "critical"}, "annotations": {},
            "startsAt": (now - timedelta(minutes=1)).isoformat(), "endsAt": now.isoformat()}


def test_a_fired_alert_opens_and_pages_locally_before_alertmanager():
    store, am = _Store(), _AM()
    w = fastpath.LocalFirstAlertmanager(am, lambda: store, _fp)

    w.post_alerts([_fire()])

    kinds = [c[0] for c in store.calls]
    assert kinds == ["open", "touch", "paged"], kinds
    assert am.posted and am.posted[0]["labels"]["alertname"] == "InterfaceDown", "and still forwarded"
    assert w.local_opens == 1


def test_a_heartbeat_does_not_page_again():
    store, am = _Store(), _AM()
    w = fastpath.LocalFirstAlertmanager(am, lambda: store, _fp)
    w.post_alerts([_fire()])
    w.post_alerts([_fire()])

    assert [c[0] for c in store.calls].count("paged") == 1
    assert len(am.posted) == 2, "Alertmanager still gets the heartbeat"


def test_a_resolve_closes_locally_at_once():
    store, am = _Store(), _AM()
    w = fastpath.LocalFirstAlertmanager(am, lambda: store, _fp)
    w.post_alerts([_fire()])
    w.post_alerts([_resolve()])

    assert store.calls[-1][0] == "close" and store.rows["sig-InterfaceDown"]["resolved_at"]
    assert w.local_closes == 1


def test_alertmanager_being_down_does_not_stop_the_page():
    store, am = _Store(), _AM(fail=True)
    w = fastpath.LocalFirstAlertmanager(am, lambda: store, _fp)

    with pytest.raises(RuntimeError):
        w.post_alerts([_fire()])

    assert ("paged", 1) in store.calls, "paged first; the forward failing is the caller's problem to log"


def test_a_held_or_narged_occurrence_is_not_paged_by_the_wrapper():
    store, am = _Store(), _AM()
    w = fastpath.LocalFirstAlertmanager(am, lambda: store, _fp)
    store.rows["sig-Held"] = {"id": 7, "signature": "sig-Held", "paged_at": None, "paging_disabled": 0,
                              "page_at": "2999-01-01T00:00:00+00:00", "resolved_at": None}
    store.rows["sig-Narg"] = {"id": 8, "signature": "sig-Narg", "paged_at": None, "paging_disabled": 1,
                              "page_at": None, "resolved_at": None}

    w.post_alerts([_fire("Held"), _fire("Narg")])

    assert not any(c[0] == "paged" for c in store.calls)


def test_the_detection_path_and_signal_time_are_recorded():
    store, am = _Store(), _AM()
    w = fastpath.LocalFirstAlertmanager(am, lambda: store, _fp)
    event = {"device_timestamp": "2026-09-16T08:22:30+00:00", "timestamp": "2026-09-16T08:22:30.4Z"}

    with fastpath.signal(event):
        w.post_alerts([_fire("FromSyslog")])
    with fastpath.attributed("loki"):
        w.post_alerts([_fire("FromLoki")])
    w.post_alerts([_fire("FromPoll")])
    with fastpath.attributed("jacob"):
        w.post_alerts([_resolve("FromPoll")])

    opens = {c[1]: (c[2], c[3]) for c in store.calls if c[0] == "open"}
    assert opens["sig-FromSyslog"] == ("syslog", "2026-09-16T08:22:30+00:00")
    assert opens["sig-FromLoki"] == ("loki", None)
    assert opens["sig-FromPoll"] == ("poll", None)
    assert ("close", "sig-FromPoll", "jacob") in store.calls


def test_no_store_yet_means_plain_forwarding():
    am = _AM()
    w = fastpath.LocalFirstAlertmanager(am, lambda: None, _fp)
    w.post_alerts([_fire()])
    assert len(am.posted) == 1
    assert w.list_alerts() == ["passthrough"], "everything else passes straight through"


# --- the self-test line ---------------------------------------------------

def test_the_selftest_line_is_rfc5424_with_a_dell_shaped_message():
    """RFC 5424, not BSD: Vector's BSD parsing took the %SWB-4-... token as
    the app name and the mnemonic never reached the message (seen live)."""
    line = fastpath.selftest_line("abc123", when=datetime(2026, 9, 6, 8, 5, 9, 120000, tzinfo=timezone.utc))
    assert line == ("<190>1 2026-09-06T08:05:09.120Z switchboard switchboard - - - "
                    "%SWB-4-SWITCHBOARD_SELFTEST: Fast-path self-test nonce=abc123")


def test_the_nonce_is_found_wherever_the_parser_put_it():
    st = fastpath.FastPathStats()
    now = datetime.now(timezone.utc)
    st.record([{"appname": "%SWB-4-SWITCHBOARD_SELFTEST", "message": "Fast-path self-test nonce=deadbeef"}], received_at=now)
    assert st.selftest_received_at("deadbeef") == now
