"""Bearer authentication: API tokens and the session path beside them.

Pinned: a token's own role is what a role check sees (not a cookie that
happens to be in the same request); a bad bearer is a 401, never a
fall-through to the cookie; and with no bearer at all the session path is
unchanged. The Keycloak-JWT path needs the realm's keys, so it is covered
by its issuer/expiry gates rather than a live signature here.
"""
import base64
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import itsdangerous
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import app as app_module  # noqa: E402


class _FakeTokens:
    """Behaves like ApiTokenStore.verify for a fixed set of tokens."""

    def __init__(self, tokens):
        self.tokens = tokens          # clear text -> row or None (None = revoked/expired)
        self.seen = []

    def verify(self, token):
        self.seen.append(token)
        return self.tokens.get(token)


def _session_cookie(role, username="cookie-user"):
    signer = itsdangerous.TimestampSigner(str(app_module.SESSION_SECRET_KEY))
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    return signer.sign(base64.b64encode(json.dumps(
        {"username": username, "role": role, "expires_at": expires}).encode())).decode()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "STORE", object())
    monkeypatch.setattr(app_module, "DB_ERROR", None)
    monkeypatch.setattr(app_module, "API_TOKENS", _FakeTokens({
        "sb_" + "v" * 40: {"id": 1, "name": "reader", "role": "viewer"},
        "sb_" + "a" * 40: {"id": 2, "name": "automation", "role": "admin"},
        "sb_" + "r" * 40: None,                                   # revoked
    }))
    monkeypatch.setattr(app_module, "OIDC_ISSUER_URL", "")   # no JWT path in these tests
    monkeypatch.setattr(app_module, "WEBHOOKS", type("W", (), {"list": lambda self: []})())
    return TestClient(app_module.app)


def test_a_viewer_token_can_read(client):
    r = client.get("/api/events", headers={"Authorization": "Bearer sb_" + "v" * 40})

    assert r.status_code == 200


def test_a_viewer_token_cannot_do_admin_things(client):
    r = client.get("/api/webhooks", headers={"Authorization": "Bearer sb_" + "v" * 40})

    assert r.status_code == 403
    assert "requires admin" in r.json()["detail"]


def test_an_admin_token_can(client):
    r = client.get("/api/webhooks", headers={"Authorization": "Bearer sb_" + "a" * 40})

    assert r.status_code == 200


def test_the_tokens_role_wins_over_a_cookie_in_the_same_request(client):
    """A stale admin cookie in the browser must not upgrade a viewer token,
    and a viewer cookie must not downgrade an admin token - the caller
    chose bearer, so bearer is what is judged."""
    client.cookies.set("switchboard_session", _session_cookie("admin"))
    r = client.get("/api/webhooks", headers={"Authorization": "Bearer sb_" + "v" * 40})
    assert r.status_code == 403

    client.cookies.set("switchboard_session", _session_cookie("viewer"))
    r = client.get("/api/webhooks", headers={"Authorization": "Bearer sb_" + "a" * 40})
    assert r.status_code == 200


def test_a_revoked_token_is_401_not_a_cookie_fallback(client):
    client.cookies.set("switchboard_session", _session_cookie("admin"))

    r = client.get("/api/events", headers={"Authorization": "Bearer sb_" + "r" * 40})

    assert r.status_code == 401
    assert "revoked" in r.json()["detail"]


def test_a_garbage_bearer_is_401(client):
    client.cookies.set("switchboard_session", _session_cookie("admin"))

    r = client.get("/api/events", headers={"Authorization": "Bearer not-a-token-at-all"})

    assert r.status_code == 401


def test_no_bearer_means_the_session_path_as_before(client):
    assert client.get("/api/events").status_code == 401

    client.cookies.set("switchboard_session", _session_cookie("viewer"))
    assert client.get("/api/events").status_code == 200


def test_the_api_docs_are_where_the_description_says(client):
    r = client.get("/api/openapi.json")

    assert r.status_code == 200
    spec = r.json()
    assert spec["info"]["title"] == "Switchboard API" and spec["info"]["version"] == app_module.API_VERSION
    assert "/api/tokens" in spec["paths"] and "/api/webhooks" in spec["paths"]
    assert "/sw.js" not in spec["paths"], "PWA plumbing is not part of the public API"
