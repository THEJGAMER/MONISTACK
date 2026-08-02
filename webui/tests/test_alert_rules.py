"""Tests for AlertRuleStore.page_delay_for - the lookup paging.py relies on
to decide how long a rule's alarms are held before paging (see the Rules
tab's "Page delay" column). Pinned down here because a mistake in this
lookup fails in exactly the direction that matters: silently paging sooner
(or later) than an operator configured, with nothing in the UI to say why.

Uses a minimal in-memory fake of the Database interface (query/query_one/
execute) rather than a real Postgres connection - AlertRuleStore only calls
those three methods, and exercising the real SQL text against a fake table
is more honest than mocking the store itself.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from alert_rules import AlertRuleStore


class _FakeDB:
    """One table, `alert_rules`, as a list of dicts - just enough SQL
    pattern-matching to serve AlertRuleStore's actual queries."""

    def __init__(self, rows):
        self.rows = rows  # list of dicts, mutated in place like real rows

    def query_one(self, sql, params=()):
        if "SELECT COUNT(*)" in sql:
            return {"n": len(self.rows)}
        if "SELECT page_delay_seconds FROM alert_rules WHERE name" in sql:
            name = params[0]
            for r in self.rows:
                if r["name"] == name:
                    return {"page_delay_seconds": r.get("page_delay_seconds")}
            return None
        if "SELECT * FROM alert_rules WHERE name" in sql:
            name = params[0]
            for r in self.rows:
                if r["name"] == name:
                    return dict(r)
            return None
        raise AssertionError(f"unexpected query_one: {sql}")

    def query(self, sql, params=()):
        return [dict(r) for r in self.rows]

    def execute(self, sql, params=()):
        if "UPDATE alert_rules SET page_delay_seconds = NULL" in sql:
            name = params[-1]
            field, value = "page_delay_seconds", None
        elif "UPDATE alert_rules SET page_delay_seconds = %s" in sql:
            value, _updated_at, name = params
            field = "page_delay_seconds"
        elif "UPDATE alert_rules SET for_seconds = %s" in sql:
            value, _updated_at, name = params
            field = "for_seconds"
        elif "UPDATE alert_rules SET severity = %s" in sql:
            value, _updated_at, name = params
            field = "severity"
        elif "UPDATE alert_rules SET enabled = %s" in sql:
            value, _updated_at, name = params
            field = "enabled"
        else:
            raise AssertionError(f"unexpected execute: {sql}")
        for r in self.rows:
            if r["name"] == name:
                r[field] = value
        return _Result(1)


class _Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


def _row(name, page_delay_seconds):
    """A full alert_rules row - update() re-reads the row through
    AlertRuleStore._to_dict, which needs every column, not just the one
    under test."""
    return {
        "name": name,
        "expr": "up == 0",
        "for_seconds": 60,
        "severity": "warning",
        "summary_template": "test",
        "description_template": "test",
        "enabled": 1,
        "page_delay_seconds": page_delay_seconds,
        "updated_at": "2026-08-01T00:00:00+00:00",
    }


@pytest.fixture
def store():
    rows = [
        _row("S4048PSUDown", None),
        _row("S4048FanDown", 0),
        _row("S4048DeviceDown", 45),
    ]
    return AlertRuleStore(_FakeDB(rows))


def test_unset_rule_falls_back_to_app_default(store):
    assert store.page_delay_for("S4048PSUDown", 120) == 120


def test_explicit_zero_is_honored_not_treated_as_unset(store):
    """0 means "page this rule instantly" - a real, deliberate choice
    (see paging.hold_for_duration treating <=0 as "don't hold at all"),
    and must not be conflated with NULL/"use the default"."""
    assert store.page_delay_for("S4048FanDown", 120) == 0


def test_explicit_override_wins_over_app_default(store):
    assert store.page_delay_for("S4048DeviceDown", 120) == 45


def test_unknown_rule_name_falls_back_to_app_default(store):
    """Interface alerts (alertname always "InterfaceDown") never have a
    matching row in alert_rules - must fall through cleanly rather than
    erroring, so paging.py's hold-placement path never breaks on them."""
    assert store.page_delay_for("SomeRuleThatDoesNotExist", 120) == 120


def test_update_sets_override(store):
    updated = store.update("S4048PSUDown", page_delay_seconds=30)
    assert updated["page_delay_seconds"] == 30
    assert store.page_delay_for("S4048PSUDown", 120) == 30


def test_update_clears_override_back_to_default(store):
    store.update("S4048DeviceDown", page_delay_seconds=45)
    updated = store.update("S4048DeviceDown", clear_page_delay=True)
    assert updated["page_delay_seconds"] is None
    assert store.page_delay_for("S4048DeviceDown", 120) == 120


def test_update_rejects_out_of_range_delay(store):
    with pytest.raises(ValueError):
        store.update("S4048PSUDown", page_delay_seconds=-1)
    with pytest.raises(ValueError):
        store.update("S4048PSUDown", page_delay_seconds=999999)


def test_update_sets_for_seconds(store):
    """The user-facing feature this pins down: changing exactly how long a
    rule waits before counting as firing, per rule."""
    updated = store.update("S4048PSUDown", for_seconds=5)
    assert updated["for_seconds"] == 5


def test_update_for_seconds_zero_is_valid_not_ignored(store):
    """0 means "fire instantly, no confirmation window" - a real, deliberate
    choice, and Python's `if for_seconds:` truthiness trap would silently
    skip writing it since 0 is falsy. Must not be confused with "not
    provided" the way that trap would."""
    updated = store.update("S4048PSUDown", for_seconds=0)
    assert updated["for_seconds"] == 0


def test_update_rejects_out_of_range_for_seconds(store):
    with pytest.raises(ValueError):
        store.update("S4048PSUDown", for_seconds=-1)
    with pytest.raises(ValueError):
        store.update("S4048PSUDown", for_seconds=999999)


def test_update_for_seconds_does_not_touch_other_rules(store):
    store.update("S4048PSUDown", for_seconds=5)
    untouched = store.update("S4048FanDown", severity="warning")  # no-op touch, just re-reads
    assert untouched["for_seconds"] == 60  # the fixture's unrelated default, unchanged
