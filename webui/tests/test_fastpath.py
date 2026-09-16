"""The syslog fast path: what arrives is parsed exactly and counted honestly.
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
    assert st.last_by_host["S4048"] == now


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
