"""Tests for loki_client.query_range's retry policy.

The one property that matters: **a timeout is never retried**. A timeout
means Loki is still working on the request - its own log shows "context
canceled" for exactly these - and sending it again half a second later
doubles the load at the one moment Loki is already behind. With two
pollers on a 3-second loop, that is how a brief queue overflow became a
sustained one (2026-09-15: one open Console tab, 45 errors/min).

A dropped connection is different: nothing is in flight, the query is
read-only and idempotent, and one quick retry absorbs a transient blip
without surfacing "Loki unreachable" for a single lost packet.
"""
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import loki_client  # noqa: E402


class _Resp:
    def __init__(self, body=b'{"data":{"result":[]}}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_sequence(monkeypatch, outcomes):
    """Each call to urlopen pops the next outcome: an Exception instance
    to raise, or a bytes body to return."""
    calls = []

    def fake(url, timeout=None):
        calls.append(url)
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return _Resp(out)

    monkeypatch.setattr(loki_client.urllib.request, "urlopen", fake)
    monkeypatch.setattr(loki_client.time, "sleep", lambda s: None)
    return calls


def test_a_timeout_is_not_retried(monkeypatch):
    calls = _urlopen_sequence(monkeypatch, [TimeoutError("timed out"), b'{"data":{"result":[]}}'])

    with pytest.raises(loki_client.LokiError):
        loki_client.LokiClient("http://loki:3100").query_range(since_seconds=20)

    assert len(calls) == 1, "retrying a timeout is the amplifier this exists to remove"


def test_a_urllib_wrapped_timeout_is_not_retried_either(monkeypatch):
    """urllib reports socket timeouts as URLError(reason=timeout) on some
    code paths and as a bare TimeoutError on others."""
    wrapped = urllib.error.URLError(socket.timeout("timed out"))
    calls = _urlopen_sequence(monkeypatch, [wrapped, b'{"data":{"result":[]}}'])

    with pytest.raises(loki_client.LokiError):
        loki_client.LokiClient("http://loki:3100").query_range(since_seconds=20)

    assert len(calls) == 1


def test_a_dropped_connection_is_retried_once(monkeypatch):
    calls = _urlopen_sequence(monkeypatch, [urllib.error.URLError(ConnectionResetError("reset")),
                                            b'{"data":{"result":[]}}'])

    assert loki_client.LokiClient("http://loki:3100").query_range(since_seconds=20) == []
    assert len(calls) == 2


def test_two_dropped_connections_fail_rather_than_retrying_forever(monkeypatch):
    calls = _urlopen_sequence(monkeypatch, [urllib.error.URLError(ConnectionResetError("a")),
                                            urllib.error.URLError(ConnectionResetError("b"))])

    with pytest.raises(loki_client.LokiError):
        loki_client.LokiClient("http://loki:3100").query_range(since_seconds=20)

    assert len(calls) == 2


def test_a_success_needs_no_retry(monkeypatch):
    calls = _urlopen_sequence(monkeypatch, [b'{"data":{"result":[]}}'])

    loki_client.LokiClient("http://loki:3100").query_range(since_seconds=20)

    assert len(calls) == 1


def test_failures_are_counted(monkeypatch):
    _urlopen_sequence(monkeypatch, [TimeoutError("timed out")])
    before = loki_client.metrics.loki_query_failure_total._value.get()

    with pytest.raises(loki_client.LokiError):
        loki_client.LokiClient("http://loki:3100").query_range(since_seconds=20)

    assert loki_client.metrics.loki_query_failure_total._value.get() == before + 1
