"""What the OIDC callback does when the code exchange fails.

The distinction being pinned: a *stale* login and a *broken* one look
identical at the point of failure, and they need opposite handling. The
state and nonce live in the session cookie, so an expired cookie, a
cleared one, or a callback URL re-opened from history all fail the
exchange while nothing is actually wrong - the user usually still has a
live Keycloak session and is one redirect from being signed in. A wrong
client secret fails the same way and always will.

So the callback retries, and counts. The counter is what stops a broken
setup from bouncing the browser between two hosts forever with nothing on
screen, which is the failure mode a bare retry would introduce.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module


class _FailingOidc:
    """Fails the exchange the way Authlib does for a stale state."""

    def __init__(self, error="mismatching_state: CSRF Warning! State not equal in request and response."):
        self.error = error
        self.calls = 0

    async def authorize_access_token(self, request):
        self.calls += 1
        raise Exception(self.error)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "oidc_client", _FailingOidc())
    monkeypatch.setattr(app_module, "MAX_LOGIN_RETRIES", 2)
    # The rate limiter is keyed per-IP and every test here shares one.
    monkeypatch.setattr(app_module, "_auth_attempts", {})
    return TestClient(app_module.app, follow_redirects=False)


def _callback(client, retries=None):
    cookies = {app_module.LOGIN_RETRY_COOKIE: str(retries)} if retries is not None else {}
    return client.get("/api/auth/callback?code=abc&state=xyz", cookies=cookies)


def test_a_stale_login_is_retried_rather_than_shown_as_an_error(client):
    """The reported symptom: a bare {"detail":"OIDC login failed"} where a
    working login was one redirect away."""
    resp = _callback(client)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/api/auth/login"


def test_the_retry_is_counted(client):
    resp = _callback(client)

    assert f"{app_module.LOGIN_RETRY_COOKIE}=1" in resp.headers.get("set-cookie", "")


def test_the_count_increments_across_attempts(client):
    resp = _callback(client, retries=1)

    assert resp.status_code == 302
    assert f"{app_module.LOGIN_RETRY_COOKIE}=2" in resp.headers.get("set-cookie", "")


def test_the_loop_stops_once_the_retries_are_used_up(client):
    """Without this the browser ping-pongs between Switchboard and Keycloak
    indefinitely, showing nothing."""
    resp = _callback(client, retries=2)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/#/login-failed")
    assert "/api/auth/login" not in resp.headers["location"]


def test_the_failure_page_is_told_what_went_wrong(client):
    resp = _callback(client, retries=2)

    assert "mismatching_state" in resp.headers["location"]


def test_giving_up_clears_the_counter(client):
    """So the next deliberate attempt starts with a full budget instead of
    dead-ending immediately."""
    resp = _callback(client, retries=2)

    cookie = resp.headers.get("set-cookie", "")
    assert app_module.LOGIN_RETRY_COOKIE in cookie
    assert 'Max-Age=0' in cookie or 'expires=Thu, 01 Jan 1970' in cookie.lower()


def test_a_nonsense_counter_does_not_break_the_callback(client):
    """The cookie is attacker-supplied; a bad value must fail towards
    retrying, not towards a 500."""
    resp = _callback(client, retries="not-a-number")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/api/auth/login"


def test_the_retry_cookie_is_not_readable_by_scripts(client):
    cookie = _callback(client).headers.get("set-cookie", "")

    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "samesite=lax" in cookie.lower()


def test_an_unconfigured_provider_still_says_so_plainly(client, monkeypatch):
    """Nothing to retry against - retrying would loop on a 503."""
    monkeypatch.setattr(app_module, "oidc_client", None)

    assert _callback(client).status_code == 503
