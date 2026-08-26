"""Tests for the sFlow API's time window.

One control on the page drives every panel, so the request layer has two
jobs beyond passing a number through: reject a range that cannot mean
anything, and report back the window it actually queried. The second
matters because the two differ - a span wider than the query cap is
served narrower, and a page that cannot tell will label its charts with a
range it is not showing.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module


class _FakeSflow:
    """Only resolve_window is reached by the code under test - the views
    are exercised against real Postgres in test_sflow_store.py."""

    def resolve_window(self, since_minutes=None, start=None, end=None):
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        if start is not None and end is not None:
            return start, end
        end = end or now
        if start is not None:
            return start, end
        return end - timedelta(minutes=int(since_minutes or 60)), end


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(app_module, "SFLOW", _FakeSflow())
    return app_module._sflow_window


def test_minutes_resolve_to_a_concrete_span(win):
    start, end, clamped = win(180, None, None)

    assert (end - start) == timedelta(minutes=180)
    assert clamped is False


def test_an_absolute_range_is_used_verbatim(win):
    start, end, clamped = win(60, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")

    assert start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 2, tzinfo=timezone.utc)
    assert clamped is False


def test_an_absolute_range_beats_minutes_when_both_are_sent(win):
    """The UI always sends minutes as a fallback, so the specific request
    has to win or absolute ranges would never take effect."""
    start, end, _ = win(60, "2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z")

    assert (end - start) == timedelta(days=2)


def test_a_trailing_z_is_accepted(win):
    """What browsers actually send. fromisoformat only learned to parse it
    in 3.11, so rejecting it would break the UI on an older runtime."""
    start, _, _ = win(60, "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z")

    assert start.tzinfo is not None


def test_a_naive_timestamp_is_read_as_utc_not_guessed(win):
    """Guessing local time would shift the whole window by the host's
    offset without anything on the page saying so."""
    start, _, _ = win(60, "2026-08-01T00:00:00", "2026-08-01T01:00:00")

    assert start == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_an_unparseable_timestamp_is_rejected_rather_than_ignored(win):
    """Silently falling back to the relative window would show a range
    nobody asked for and look like it worked."""
    with pytest.raises(HTTPException) as e:
        win(60, "last tuesday", "2026-08-02T00:00:00Z")

    assert e.value.status_code == 400


def test_a_backwards_range_is_rejected(win):
    with pytest.raises(HTTPException) as e:
        win(60, "2026-08-02T00:00:00Z", "2026-08-01T00:00:00Z")

    assert e.value.status_code == 400


def test_an_equal_start_and_end_is_rejected(win):
    with pytest.raises(HTTPException) as e:
        win(60, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z")

    assert e.value.status_code == 400


def test_an_oversized_span_is_clamped_and_says_so(win, monkeypatch):
    monkeypatch.setattr(app_module, "SFLOW_MAX_SPAN_DAYS", 92)

    start, end, clamped = win(60, "2020-01-01T00:00:00Z", "2026-08-01T00:00:00Z")

    assert clamped is True
    assert (end - start) == timedelta(days=92)
    # The recent end is kept: someone asking for years of history is
    # almost always looking at the near end of it.
    assert end == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_minutes_are_capped_at_seven_days(win):
    start, end, _ = win(999999, None, None)

    assert (end - start) == timedelta(days=7)
