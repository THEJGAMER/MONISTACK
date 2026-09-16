"""Wiring webhooks and push into configuration must never cost the rest.

Found live: on a dev instance with OIDC off, OIDC_REDIRECT_URI is None,
and one AttributeError while choosing the VAPID subject aborted
_apply_settings after the stores were built but before devices loaded -
the app came up with no devices and no push, and nothing in the health
panel said why.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import app as app_module  # noqa: E402


class _Store:
    def __init__(self, db=None):
        pass

    def list(self, *a, **k):
        return []

    def _all_raw(self):
        return []


def test_wiring_survives_a_missing_oidc_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "OIDC_REDIRECT_URI", None)
    monkeypatch.setattr(app_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "WEBHOOKS", _Store())
    monkeypatch.setattr(app_module, "PUSH_SUBS", _Store())
    monkeypatch.setattr(app_module, "PUSH_KEYS", None)
    monkeypatch.setattr(app_module, "WEBHOOK_DISPATCHER", None)
    monkeypatch.setattr(app_module, "PUSH_NOTIFIER", None)

    app_module._wire_event_bus()

    assert app_module.WEBHOOK_DISPATCHER is not None
    assert app_module.PUSH_KEYS is not None
    assert app_module.PUSH_KEYS.subject.startswith("mailto:")


def test_an_https_redirect_uri_becomes_the_vapid_subject(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "OIDC_REDIRECT_URI", "https://switchboard.example.com/api/auth/callback")
    monkeypatch.setattr(app_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "WEBHOOKS", _Store())
    monkeypatch.setattr(app_module, "PUSH_SUBS", _Store())
    monkeypatch.setattr(app_module, "PUSH_KEYS", None)

    app_module._wire_event_bus()

    assert app_module.PUSH_KEYS.subject == "https://switchboard.example.com"


# --- the alarm lifecycle reaches the bus from the store, not a call site --

class _Occurrences:
    on_opened = on_paged = on_closed = None


def _collect(monkeypatch):
    got = []
    real_emit = app_module.events.BUS.emit
    monkeypatch.setattr(app_module.events.BUS, "emit", lambda event, **p: got.append((event, p)))
    return got, real_emit


def test_store_transitions_become_the_three_alarm_events(monkeypatch):
    """Confirmed live: emitting alarm.opened only from the Alertmanager
    webhook meant an alarm the sync tick opened first never paged, and one
    the sweep closed never resolved on the bus."""
    got, _ = _collect(monkeypatch)
    store = _Occurrences()
    app_module._wire_occurrence_events(store)
    occ = {"id": 9, "alertname": "FanFailure", "severity": "critical", "summary": "s", "labels": '{"device": "s4048"}',
           "signature": "sig", "started_at": "2026-09-15T10:00:00+00:00", "paged_at": None, "resolved_at": None}

    store.on_opened(occ)
    store.on_paged({**occ, "paged_at": "2026-09-15T10:05:00+00:00"})
    store.on_closed({**occ, "paged_at": "2026-09-15T10:05:00+00:00", "resolved_at": "2026-09-15T10:30:00+00:00"}, "sync")

    assert [e for e, _ in got] == ["alarm.opened", "alarm.paged", "alarm.resolved"]
    opened, paged, resolved = (p["occurrence"] for _, p in got)
    assert opened["device"] == "s4048" and opened["paged_at"] is None
    assert paged["paged_at"] == "2026-09-15T10:05:00+00:00"
    assert resolved["resolved_at"] == "2026-09-15T10:30:00+00:00" and got[2][1]["by"] == "sync"


def test_a_close_with_no_author_is_attributed_to_switchboard(monkeypatch):
    got, _ = _collect(monkeypatch)
    store = _Occurrences()
    app_module._wire_occurrence_events(store)

    store.on_closed({"id": 1, "alertname": "x", "labels": {}}, None)

    assert got[0][1]["by"] == "switchboard"
