"""Route-level tests for command history / favourites (ROADMAP Phase 4).

The stores themselves are covered against a real Postgres in
test_command_history.py. What's pinned here is the part that only exists
at the route layer and that a store test can't reach: the RBAC boundary on
`?all_users=true`.

That boundary matters because command history is *personal*. Everything
else about these endpoints is a convenience feature, but "any authenticated
user can read what every other user has been running on the network gear"
would be an accidental disclosure, and it's exactly the kind of check that
looks obviously correct while reading and is trivially lost in a later
refactor. It reads the role straight out of `request.session`, so a real
signed session cookie is the only way to drive it - same reason and same
helper as test_settings_db_recovery.py.
"""
import base64
import json as j
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import itsdangerous
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module


def _session_cookie(app, role, username="test-user"):
    signer = itsdangerous.TimestampSigner(str(app.SESSION_SECRET_KEY))
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    session = {"username": username, "role": role, "expires_at": expires}
    data = base64.b64encode(j.dumps(session).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


class _FakeHistory:
    """Records how the route called the store - the assertion target is
    the `actor` argument, since that's what enforces the scoping."""

    def __init__(self):
        self.last_call = None

    def list(self, actor=None, device_id=None, status=None, q=None, limit=100, offset=0):
        self.last_call = {"actor": actor, "device_id": device_id, "status": status,
                          "q": q, "limit": limit, "offset": offset}
        return [], 0

    def recent_commands(self, actor, limit=10):
        self.last_call = {"actor": actor, "limit": limit}
        return []

    def clear(self, actor):
        self.last_call = {"cleared_for": actor}


@pytest.fixture
def history():
    return _FakeHistory()


@pytest.fixture
def client(monkeypatch, history):
    monkeypatch.setattr(app_module, "COMMAND_HISTORY", history)
    monkeypatch.setattr(app_module, "STORE", object())  # require_auth_and_db just needs this non-None
    monkeypatch.setattr(app_module, "AUDIT", type("A", (), {"record": lambda *a, **k: None})())
    return TestClient(app_module.app)


def test_history_defaults_to_the_callers_own_rows(client, history):
    client.cookies.set("switchboard_session", _session_cookie(app_module, "operator", "alice"))
    resp = client.get("/api/command-history")
    assert resp.status_code == 200, resp.text
    assert history.last_call["actor"] == "alice"


def test_non_admin_cannot_request_all_users_history(client, history):
    """The boundary this file exists for - a viewer/operator asking for
    the fleet-wide view must be refused, not quietly downgraded to their
    own rows (which would look like it worked)."""
    client.cookies.set("switchboard_session", _session_cookie(app_module, "operator", "alice"))
    resp = client.get("/api/command-history?all_users=true")
    assert resp.status_code == 403, resp.text


def test_admin_can_request_all_users_history(client, history):
    client.cookies.set("switchboard_session", _session_cookie(app_module, "admin", "root"))
    resp = client.get("/api/command-history?all_users=true")
    assert resp.status_code == 200, resp.text
    assert history.last_call["actor"] is None  # None = unscoped, fleet-wide


def test_history_requires_authentication(client):
    resp = client.get("/api/command-history")
    assert resp.status_code == 401


def test_clear_is_scoped_to_the_caller(client, history):
    """Clearing must never be able to wipe someone else's history - the
    route passes the session's own username, with no parameter to override
    it."""
    client.cookies.set("switchboard_session", _session_cookie(app_module, "operator", "alice"))
    resp = client.delete("/api/command-history")
    assert resp.status_code == 200, resp.text
    assert history.last_call == {"cleared_for": "alice"}


def test_recent_is_scoped_to_the_caller(client, history):
    client.cookies.set("switchboard_session", _session_cookie(app_module, "viewer", "bob"))
    resp = client.get("/api/command-history/recent?limit=5")
    assert resp.status_code == 200, resp.text
    assert history.last_call == {"actor": "bob", "limit": 5}
