"""Tests for paging.py - the investigation hold that sits between an alarm
firing and a human's pager.

Previously untested. The module's own docstring states the safety
principle it is built around, and that principle is what these tests
mostly pin: **"pages sooner than you wanted" is a safe failure for a
pager; "silently never pages" is not.** Every error path here must fail
open (no hold -> the alarm pages normally through Alertmanager), never
closed (a hold that outlives its purpose, or an exception that stops the
alarm being processed at all).

The hold is an Alertmanager silence, so these tests use a fake
Alertmanager that records silence calls rather than asserting against a
live one - the live round trip is covered by the end-to-end validation.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from paging import PagingController


class _FakeAlertmanager:
    def __init__(self, silence_id="sil-1"):
        self.created = []
        self.deleted = []
        self.silence_id = silence_id

    def create_silence(self, matchers, starts_at, ends_at, created_by, comment):
        self.created.append({"matchers": matchers, "startsAt": starts_at, "endsAt": ends_at,
                             "createdBy": created_by, "comment": comment})
        return {"silenceID": self.silence_id}

    def delete_silence(self, silence_id):
        self.deleted.append(silence_id)


class _BrokenAlertmanager:
    def create_silence(self, *a, **kw):
        raise RuntimeError("alertmanager unreachable")

    def delete_silence(self, *a, **kw):
        raise RuntimeError("alertmanager unreachable")


LABELS = {"alertname": "S4048PSUDown", "device_id": "s4048", "unit": "1", "bay": "2"}


# --- matchers --------------------------------------------------------

def test_matchers_cover_the_full_label_set_exactly():
    """A silence built from a subset would suppress far more than the one
    alarm it's meant to hold - silencing {alertname=InterfaceDown} alone
    would hold back every port on every device."""
    matchers = PagingController.matchers_for(LABELS)

    assert {m["name"] for m in matchers} == set(LABELS)
    assert all(m["isEqual"] and not m["isRegex"] for m in matchers)
    by_name = {m["name"]: m["value"] for m in matchers}
    assert by_name["alertname"] == "S4048PSUDown"
    assert by_name["bay"] == "2"


def test_matcher_values_are_stringified():
    """Alertmanager's matcher values are strings; an int label would
    otherwise produce a silence that matches nothing."""
    matchers = PagingController.matchers_for({"unit": 1, "bay": 2})

    assert all(isinstance(m["value"], str) for m in matchers)


def test_matchers_handle_no_labels():
    assert PagingController.matchers_for(None) == []
    assert PagingController.matchers_for({}) == []


# --- holds -----------------------------------------------------------

def test_hold_for_duration_creates_a_silence_and_reports_when_it_lifts():
    am = _FakeAlertmanager()
    controller = PagingController(am)

    silence_id, page_at = controller.hold_for_duration(LABELS, 120)

    assert silence_id == "sil-1"
    assert len(am.created) == 1
    # page_at is what the UI counts down to, so it must be in the future.
    assert page_at > datetime.now(timezone.utc)


def test_a_zero_or_negative_delay_means_do_not_hold_at_all():
    """Not "hold for no time" - no silence may be created, so the alarm
    pages immediately through the normal receivers."""
    am = _FakeAlertmanager()
    controller = PagingController(am)

    assert controller.hold_for_duration(LABELS, 0) == (None, None)
    assert controller.hold_for_duration(LABELS, -30) == (None, None)
    assert am.created == [], "no silence may be created when there's no hold"


def test_hold_until_in_the_past_creates_nothing():
    """A hold that has already expired would be a silence that suppresses
    nothing while looking like protection."""
    am = _FakeAlertmanager()
    controller = PagingController(am)

    result = controller.hold_until(LABELS, datetime.now(timezone.utc) - timedelta(seconds=1), "late")

    assert result is None
    assert am.created == []


def test_hold_for_default_uses_the_configured_delay():
    am = _FakeAlertmanager()
    controller = PagingController(am, default_delay_seconds=300)

    _, page_at = controller.hold_for_default(LABELS)

    remaining = (page_at - datetime.now(timezone.utc)).total_seconds()
    assert 290 < remaining <= 300


def test_a_failed_hold_fails_open_rather_than_raising():
    """The module's core safety property: if the hold can't be placed, the
    alarm must page normally. Raising here would propagate into the
    caller's alarm-processing loop, which is the "silently never pages"
    direction."""
    controller = PagingController(_BrokenAlertmanager())

    assert controller.hold_until(LABELS, datetime.now(timezone.utc) + timedelta(seconds=60), "x") is None


def test_hold_for_duration_fails_open_too():
    controller = PagingController(_BrokenAlertmanager())

    silence_id, page_at = controller.hold_for_duration(LABELS, 120)

    assert silence_id is None
    # page_at is still returned so the caller's bookkeeping stays coherent;
    # what matters is that no silence exists, so paging is not suppressed.
    assert page_at is not None


# --- release ---------------------------------------------------------

def test_release_deletes_the_silence():
    am = _FakeAlertmanager()
    controller = PagingController(am)

    assert controller.release("sil-1") is True
    assert am.deleted == ["sil-1"]


def test_releasing_nothing_is_a_success():
    """"Page now" on an alarm that was never held is not an error - the
    end state (not suppressed) is what was wanted either way."""
    am = _FakeAlertmanager()
    controller = PagingController(am)

    assert controller.release(None) is True
    assert am.deleted == []


def test_a_failed_release_reports_failure_without_raising():
    """Reported, not raised: the caller decides what to do, and the alarm
    loop keeps running."""
    controller = PagingController(_BrokenAlertmanager())

    assert controller.release("sil-1") is False


# --- paging disabled (NARG) ------------------------------------------

def test_paging_disabled_is_a_long_but_finite_hold():
    """Deliberately not open-ended - a silence with no end date is how an
    alarm gets lost permanently by accident. The point is to stop the
    pager tonight, not delete the alarm."""
    am = _FakeAlertmanager()
    controller = PagingController(am)

    controller.hold_indefinitely(LABELS, "alice", "known issue, fixing tomorrow")

    assert len(am.created) == 1
    ends_at = datetime.fromisoformat(am.created[0]["endsAt"])
    hours = (ends_at - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23 < hours <= 24, "must lapse and page again rather than suppress forever"


def test_paging_disabled_records_who_and_why():
    """This is an auditable action, not an anonymous mute."""
    am = _FakeAlertmanager()
    controller = PagingController(am)

    controller.hold_indefinitely(LABELS, "alice", "known issue")

    assert am.created[0]["createdBy"] == "alice"
    assert "known issue" in am.created[0]["comment"]


def test_hold_comment_defaults_to_something_self_explanatory():
    """Whoever finds this silence in Alertmanager's own UI needs to know
    what created it and why."""
    am = _FakeAlertmanager()
    controller = PagingController(am)

    controller.hold_for_duration(LABELS, 120)

    assert "Switchboard" in am.created[0]["comment"]
    assert am.created[0]["createdBy"] == "switchboard"
