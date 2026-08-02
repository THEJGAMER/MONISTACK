"""Regression test for a real deadlock found live in production: PUT
/api/settings - the route whose entire purpose is fixing a broken Postgres
connection - was wired through require_admin, which is built on
require_auth_and_db, which 503s whenever the database is unreachable. That
made recovering from a broken DB connection require the DB connection to
already be working: a real admin, with a real broken DATABASE_URL, got a
503 trying to fix it from the Settings page, with no way to recover short
of touching the server directly.

Confirmed live against the actual app (STORE forced to None, matching a
genuinely broken DB) before the fix, and again after - this test pins that
same scenario down permanently.
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


def _session_cookie(app, role):
    """require_admin_no_db (like require_role generally) reads the role
    straight from request.session, so a real signed session cookie is the
    only way to drive it - a plain dependency_overrides swap doesn't
    reach code that reads request.session directly."""
    signer = itsdangerous.TimestampSigner(str(app.SESSION_SECRET_KEY))
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    session = {"username": "test-user", "role": role, "expires_at": expires}
    data = base64.b64encode(j.dumps(session).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


@pytest.fixture
def client(monkeypatch):
    # Real DB I/O is not what this test is about - _apply_settings's own
    # "a real DSN connects successfully" behavior is covered by live
    # verification (see ROADMAP.md), not re-proven here. This isolates the
    # test to what's actually being checked: which dependency gates this
    # route, not whether a real DSN can be connected to.
    monkeypatch.setattr(app_module, "_apply_settings", lambda settings_dict: None)
    monkeypatch.setattr(app_module.settings_store, "save", lambda data: None)
    # The real broken-DB state this bug only shows up in.
    monkeypatch.setattr(app_module, "STORE", None)
    monkeypatch.setattr(app_module, "DB_ERROR", "could not translate host name test")
    return TestClient(app_module.app)


def test_settings_put_works_for_admin_even_when_db_is_down(client):
    """The actual bug: this must succeed (or at least not 503) so an admin
    can fix a broken DATABASE_URL from the page that exists for exactly
    that purpose."""
    client.cookies.set("switchboard_session", _session_cookie(app_module, "admin"))
    resp = client.put("/api/settings", json={"database_url": "postgresql://x:y@z:5432/db"})
    assert resp.status_code == 200, resp.text


def test_settings_put_still_requires_admin_role_even_when_db_is_down(client):
    """The fix must not accidentally drop the role check along with the
    DB dependency - a viewer must still be rejected, just with 403 (a
    role problem), not 503 (mistaken for a DB problem)."""
    client.cookies.set("switchboard_session", _session_cookie(app_module, "viewer"))
    resp = client.put("/api/settings", json={"database_url": "postgresql://x:y@z:5432/db"})
    assert resp.status_code == 403, resp.text


def test_other_admin_routes_still_503_when_db_is_down(client):
    """The fix is deliberately scoped to PUT /api/settings only - every
    other DB-dependent route (device CRUD, etc.) must still 503 when the
    database is broken, since there's genuinely nothing they can do
    without it. Confirms the fix didn't overreach into a blanket
    DB-optional policy."""
    client.cookies.set("switchboard_session", _session_cookie(app_module, "admin"))
    resp = client.get("/api/devices")
    assert resp.status_code == 503
