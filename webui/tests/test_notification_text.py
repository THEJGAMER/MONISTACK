"""What a phone actually shows.

Before this, a notification put the raw log line in the body:

    CRITICAL: Link down: xe-0/1/3 on EX3300 (Juniper - edited)
    mib2d[1344]: SNMP_TRAP_LINK_DOWN: ifIndex 603, ifAdminStatus up(1),
    ifOperStatus down(2), ifName xe-0/1/3

You cannot read that on a lock screen, and by the time you have you have
opened the app - where the raw line is, and belongs. These pin the
wording, using events exactly as the store hands them over.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import push  # noqa: E402

RAISED = "2026-09-16T09:37:28+00:00"


def _ev(**over):
    ev = {"id": 7, "kind": "port.link_down", "kind_name": "Link down", "severity": "critical",
          "device": "EX3300 (Juniper - edited)", "subject": "xe-0/1/3",
          "title": "Link down: xe-0/1/3 on EX3300 (Juniper - edited)",
          "detail": "mib2d[1344]: SNMP_TRAP_LINK_DOWN: ifIndex 603, ifAdminStatus up(1), ifOperStatus down(2), ifName xe-0/1/3",
          "source": "syslog", "raised_at": RAISED, "resolved_at": None, "resolved_by": None,
          "resolve_detail": None, "count": 1, "reopen_count": 0}
    ev.update(over)
    return ev


def _raised(**over):
    return push.payload_for("event.raised", {"event_data": _ev(**over)})


def _resolved(**over):
    over.setdefault("resolved_at", (datetime.fromisoformat(RAISED) + timedelta(minutes=3)).isoformat())
    over.setdefault("resolved_by", "syslog")
    return push.payload_for("event.resolved", {"event_data": _ev(**over)})


# --- what is raised -----------------------------------------------------

def test_the_raw_log_line_never_reaches_the_lock_screen():
    p = _raised()
    assert "SNMP_TRAP_LINK_DOWN" not in p["body"] and "ifIndex" not in p["body"]
    assert p["body"] == "Critical. The device reported it."


def test_the_title_is_the_event_not_a_shouted_prefix():
    assert _raised()["title"] == "Link down: xe-0/1/3 on EX3300 (Juniper - edited)"
    assert not _raised()["title"].startswith("CRITICAL")


def test_a_reading_switchboard_worked_out_is_worth_saying():
    """The syslog paths carry the device's raw line; everything else
    carries a sentence the detectors wrote, and that one is useful."""
    p = _raised(source="ssh", severity="warning", kind="compute.cpu_high",
                title="High CPU on S4048: 95%",
                detail="At 95% for 3 consecutive polls, against a threshold of 90%")
    assert p["body"] == "Warning. Found by the SSH poll. At 95% for 3 consecutive polls, against a threshold of 90%."


def test_a_detail_already_ending_in_a_full_stop_is_not_given_another():
    p = _raised(source="ssh", detail="The SSH poll found it down.")
    assert p["body"].endswith("found it down.") and not p["body"].endswith("..")


def test_severity_is_a_word_not_a_shout():
    assert _raised(severity="warning")["body"].startswith("Warning.")
    assert _raised(severity="info")["body"].startswith("For information.")


def test_a_fault_that_keeps_returning_says_which_time_this_is():
    assert "Back for the 2nd time." in _raised(reopen_count=1)["body"]
    assert "Back for the 4th time." in _raised(reopen_count=3)["body"]
    assert "Back for the 12th time." in _raised(reopen_count=11)["body"]
    assert "Back for" not in _raised(reopen_count=0)["body"]


# --- what is cleared ------------------------------------------------------

def test_a_resolve_says_it_is_over_and_how_long_it_lasted():
    p = _resolved()
    assert p["title"] == "Cleared: Link down: xe-0/1/3 on EX3300 (Juniper - edited)"
    assert p["body"] == "Lasted 3 minutes. The device reported it back to normal."
    assert p["quiet"] is True and p["severity"] == "ok"


def test_durations_are_read_in_whatever_unit_suits_them():
    def lasted(**kw):
        return _resolved(resolved_at=(datetime.fromisoformat(RAISED) + timedelta(**kw)).isoformat())["body"]

    assert lasted(seconds=4).startswith("Lasted 4 seconds.")
    assert lasted(seconds=1).startswith("Lasted 1 second.")
    assert lasted(minutes=1).startswith("Lasted 1 minute.")
    assert lasted(hours=3).startswith("Lasted 3 hours.")
    assert lasted(days=4).startswith("Lasted 4 days.")


def test_who_cleared_it_is_said_in_words_not_in_source_names():
    assert "The SSH poll saw it recover." in _resolved(resolved_by="ssh")["body"]
    assert "It stopped being reported." in _resolved(resolved_by="timer")["body"]
    assert "Cleared by Switchboard." in _resolved(resolved_by="switchboard")["body"]
    assert "Resolved by jacob@example.com." in _resolved(resolved_by="jacob@example.com")["body"]


def test_a_resolve_with_no_times_still_says_something_useful():
    p = _resolved(raised_at=None, resolved_at=None, resolved_by="ssh")
    assert p["body"] == "The SSH poll saw it recover."


# --- neither ---------------------------------------------------------------

def test_other_bus_events_are_not_notifications():
    assert push.payload_for("command.ran", {"event": "command.ran"}) is None
    assert push.payload_for("event.raised", {}) is None
