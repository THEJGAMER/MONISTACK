"""Tests for the service settings and health panel on the Settings page.

The property worth pinning hardest: **none of this may require a working
database**. The Settings page is where a broken deployment gets repaired,
so anything on it gated behind Postgres is unreachable at exactly the
moment it's needed. That mistake has already been made once here (PUT
/api/settings used to 503 when the DB was down - the page whose whole job
is fixing the DB), and adding four more settings plus a health panel is a
natural place to reintroduce it.
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
import settings as settings_store


def _session_cookie(app, role, username="test-user"):
    signer = itsdangerous.TimestampSigner(str(app.SESSION_SECRET_KEY))
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
    session = {"username": username, "role": role, "expires_at": expires}
    data = base64.b64encode(j.dumps(session).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


# --- reload URL derivation ------------------------------------------

def test_reload_url_derives_from_prometheus_url():
    """One less thing to keep in sync by hand - and getting it wrong
    silently breaks the Rules tab's live reload rather than erroring
    anywhere visible."""
    assert settings_store.reload_url_for({"prometheus_url": "http://p:9090"}) == "http://p:9090/-/reload"


def test_an_explicit_reload_url_wins():
    got = settings_store.reload_url_for(
        {"prometheus_url": "http://p:9090", "prometheus_reload_url": "http://proxy/reload"}
    )
    assert got == "http://proxy/reload"


def test_reload_url_tolerates_a_trailing_slash():
    assert settings_store.reload_url_for({"prometheus_url": "http://p:9090/"}) == "http://p:9090/-/reload"


def test_reload_url_is_blank_when_prometheus_is_unset():
    assert settings_store.reload_url_for({}) == ""


# --- env seeding -----------------------------------------------------

def test_bootstrap_seeds_every_service_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ALERTMANAGER_URL", "http://am:9093")
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)

    seeded = settings_store.bootstrap_from_env()

    assert seeded["alertmanager_url"] == "http://am:9093"
    # Unset env falls back to the documented default rather than vanishing.
    assert seeded["prometheus_url"] == "http://prometheus:9090"
    assert set(k for k, _, _ in settings_store.SERVICE_SETTINGS) <= set(seeded)


# --- probe semantics -------------------------------------------------

def test_probe_treats_any_http_response_as_reachable():
    """A 404 from a wrong path still proves something is listening. Calling
    that "down" makes a typo'd path indistinguishable from a dead host,
    which is the opposite of a useful diagnostic."""
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    orig = app_module.urllib.request.urlopen
    app_module.urllib.request.urlopen = fake_urlopen
    try:
        ok, detail = app_module._probe("http://host/wrong-path")
    finally:
        app_module.urllib.request.urlopen = orig

    assert ok is True
    assert "404" in detail


def test_probe_reports_an_unconfigured_url_distinctly():
    ok, detail = app_module._probe("")
    assert ok is False
    assert detail == "not configured"


def test_probe_reports_a_connection_failure_as_unreachable():
    ok, detail = app_module._probe("http://127.0.0.1:1/nope", timeout=2)
    assert ok is False


# --- the routes, with a broken database ------------------------------

@pytest.fixture
def broken_db_client(monkeypatch):
    """The state that matters: Postgres unreachable, everything else fine."""
    monkeypatch.setattr(app_module, "STORE", None)
    monkeypatch.setattr(app_module, "DB_ERROR", "could not translate host name")
    monkeypatch.setattr(app_module, "_apply_settings",
                        lambda d: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(app_module.settings_store, "save", lambda data: None)
    monkeypatch.setattr(app_module.settings_store, "load", lambda: {})
    return TestClient(app_module.app)


def test_health_panel_works_when_the_database_is_down(broken_db_client, monkeypatch):
    """A diagnostic panel that disappears when something is broken is
    worse than none - this is precisely when it's needed."""
    monkeypatch.setattr(app_module, "_probe", lambda url, timeout=3: (True, "HTTP 200"))
    broken_db_client.cookies.set("switchboard_session", _session_cookie(app_module, "viewer"))

    resp = broken_db_client.get("/api/settings/health")

    assert resp.status_code == 200, resp.text
    checks = {c["name"]: c for c in resp.json()["checks"]}
    assert checks["Postgres"]["ok"] is False
    assert "could not translate host name" in checks["Postgres"]["detail"]
    # The rest still report honestly rather than being suppressed.
    assert checks["Alertmanager"]["ok"] is True


def test_service_urls_are_applied_even_when_postgres_is_unreachable(broken_db_client, monkeypatch):
    """The core guarantee. An admin whose database is down must still be
    able to correct an unrelated address - the save is reported as partial
    (400) but the service settings really are applied."""
    applied = {}
    monkeypatch.setattr(app_module, "_apply_service_settings", lambda d: applied.update(d))
    broken_db_client.cookies.set("switchboard_session", _session_cookie(app_module, "admin"))

    resp = broken_db_client.put("/api/settings", json={"alertmanager_url": "http://new-am:9093"})

    assert resp.status_code == 400  # honest: Postgres genuinely is down
    assert "Saved" in resp.json()["detail"]
    assert applied["alertmanager_url"] == "http://new-am:9093"


def test_health_still_requires_authentication(broken_db_client):
    assert broken_db_client.get("/api/settings/health").status_code == 401


def test_saving_settings_is_still_admin_only(broken_db_client):
    broken_db_client.cookies.set("switchboard_session", _session_cookie(app_module, "operator"))

    resp = broken_db_client.put("/api/settings", json={"alertmanager_url": "http://x:9093"})

    assert resp.status_code == 403, "role check must survive the DB-independence work"


def test_omitted_fields_keep_their_stored_value(monkeypatch):
    """A partial PUT must not blank settings it didn't mention - the
    frontend sends the whole form, but an API client need not."""
    stored = {"database_url": "postgresql://u:p@h/db", "alertmanager_url": "http://kept:9093"}
    saved = {}
    monkeypatch.setattr(app_module, "STORE", object())
    monkeypatch.setattr(app_module, "DB_ERROR", None)
    monkeypatch.setattr(app_module.settings_store, "load", lambda: dict(stored))
    monkeypatch.setattr(app_module.settings_store, "save", lambda d: saved.update(d))
    monkeypatch.setattr(app_module, "_apply_settings", lambda d: None)
    monkeypatch.setattr(app_module, "_apply_service_settings", lambda d: None)
    client = TestClient(app_module.app)
    client.cookies.set("switchboard_session", _session_cookie(app_module, "admin"))

    resp = client.put("/api/settings", json={"prometheus_url": "http://new-prom:9090"})

    assert resp.status_code == 200, resp.text
    assert saved["alertmanager_url"] == "http://kept:9093"
    assert saved["prometheus_url"] == "http://new-prom:9090"
