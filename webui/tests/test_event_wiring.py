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


# --- event transitions reach the bus from the store, not a call site --

class _EventStore:
    on_raised = on_resolved = None


def test_store_transitions_become_bus_events(monkeypatch):
    got = []
    monkeypatch.setattr(app_module.events.BUS, "emit", lambda event, **p: got.append((event, p)))
    store = _EventStore()
    app_module._wire_event_hooks(store)
    ev = {"id": 9, "kind": "port.link_down", "severity": "warning", "device": "S4048", "subject": "Te 1/47", "title": "t"}

    store.on_raised(ev)
    store.on_resolved({**ev, "resolved_by": "ssh"})

    assert [e for e, _ in got] == ["event.raised", "event.resolved"]
    assert got[0][1]["event_data"]["subject"] == "Te 1/47" and got[1][1]["event_data"]["resolved_by"] == "ssh"
