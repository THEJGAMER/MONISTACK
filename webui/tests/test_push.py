"""Web Push: keys, the subscription store, and what gets sent to whom.

The send itself is faked - nothing here talks to a push service - so the
tests are about the decisions around it: which subscriptions an event
reaches (severity floor, the resolve flag), what the notification says,
and that a subscription the service reports as gone is pruned rather than
retried forever.
"""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import push  # noqa: E402


# --- keys -----------------------------------------------------------

def test_vapid_keys_are_generated_once_and_reloaded(tmp_path):
    p = tmp_path / "vapid.json"
    a = push.VapidKeys(p, "mailto:x@example.com")
    if not a.available:
        pytest.skip("pywebpush/py_vapid not installed")
    b = push.VapidKeys(p, "mailto:x@example.com")

    assert a.public_key == b.public_key
    assert oct(p.stat().st_mode & 0o777) == "0o600", "the private key file must not be world-readable"


# --- payloads -------------------------------------------------------

def _env(event, **ev):
    data = {"id": 7, "kind": "port.link_down", "kind_name": "Link down", "severity": "critical", "device": "S4048",
            "subject": "Te 1/47", "title": "Link down: Te 1/47 on S4048", "detail": "SNMP_TRAP_LINK_DOWN ...", **ev}
    return {"event": event, "event_data": data}


def test_a_raised_event_is_a_notification():
    p = push.payload_for("event.raised", _env("event.raised"))
    assert p["title"] == "CRITICAL: Link down: Te 1/47 on S4048"
    assert p["url"] == "/#/events/7" and p["tag"] == "switchboard-event-7"
    assert [a["action"] for a in p["actions"]] == ["open"], "no acknowledge: actioning is the ticketing system's"


def test_a_resolved_event_replaces_it_quietly():
    p = push.payload_for("event.resolved", _env("event.resolved", resolved_by="ssh", resolve_detail="port up"))
    assert p["title"].startswith("Resolved: Link down") and p["severity"] == "ok" and p["quiet"] is True
    assert p["tag"] == "switchboard-event-7" and p["body"] == "Resolved by ssh: port up"


def test_other_bus_events_are_not_notifications():
    assert push.payload_for("command.ran", {"event": "command.ran"}) is None
    assert push.payload_for("event.raised", {"event": "event.raised"}) is None


# --- store + notifier against a real Postgres --------------------------

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")
DDL = """
CREATE TABLE push_subscriptions (
    endpoint TEXT PRIMARY KEY, subscription TEXT NOT NULL, username TEXT NOT NULL, label TEXT,
    min_severity TEXT NOT NULL DEFAULT 'warning', notify_resolved INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_used_at TIMESTAMPTZ,
    failures INTEGER NOT NULL DEFAULT 0, last_error TEXT);
"""


class _DB:
    def __init__(self, conn):
        self.conn = conn

    def _cur(self):
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur

    def query(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchall()

    def query_one(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchone()


@pytest.fixture
def store():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=4)
    except Exception:
        pytest.skip("test Postgres not reachable")
    conn.autocommit = True
    schema = f"test_push_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield push.PushSubscriptionStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _sub(endpoint):
    return {"endpoint": endpoint, "keys": {"p256dh": "P", "auth": "A"}}


class _Keys:
    available = True
    public_key = "pk"
    private_key_pem = "pem"
    subject = "mailto:x@example.com"


def test_listing_never_exposes_the_keys(store):
    store.upsert(_sub("https://push/1"), "jacob", "phone")

    row = store.list(username="jacob")[0]
    assert "subscription" not in row
    assert row["label"] == "phone" and row["notify_resolved"] is True


def test_upsert_replaces_the_same_endpoint(store):
    store.upsert(_sub("https://push/1"), "jacob", "phone", "warning")
    store.upsert(_sub("https://push/1"), "jacob", "phone", "critical")

    rows = store.list()
    assert len(rows) == 1 and rows[0]["min_severity"] == "critical"


def test_a_non_subscription_is_refused(store):
    with pytest.raises(ValueError):
        store.upsert({"endpoint": "x"}, "jacob")
    with pytest.raises(ValueError):
        store.upsert(_sub("https://push/1"), "jacob", min_severity="loud")


def test_severity_floor_and_resolve_flag_decide_who_is_told(store):
    store.upsert(_sub("https://push/crit-only"), "a", min_severity="critical", notify_resolved=False)
    store.upsert(_sub("https://push/all"), "b", min_severity="info", notify_resolved=True)
    sent = []
    n = push.PushNotifier(store, _Keys(), send_fn=lambda sub, payload: (sent.append((sub["endpoint"], payload["title"])), (True, None, False))[1])

    n("event.raised", _env("event.raised", severity="warning"))
    n("event.raised", _env("event.raised", severity="critical"))
    n("event.resolved", _env("event.resolved", severity="critical"))

    endpoints = [e for e, _ in sent]
    assert endpoints.count("https://push/crit-only") == 1, "critical only, and never a resolve"
    assert endpoints.count("https://push/all") == 3


def test_a_resolve_is_only_told_to_devices_that_would_have_heard_the_raise(store):
    store.upsert(_sub("https://push/crit"), "a", min_severity="critical", notify_resolved=True)
    sent = []
    n = push.PushNotifier(store, _Keys(), send_fn=lambda sub, payload: (sent.append(payload["title"]), (True, None, False))[1])

    n("event.resolved", _env("event.resolved", severity="warning"))

    assert sent == []


def test_a_subscription_the_service_says_is_gone_is_pruned(store):
    store.upsert(_sub("https://push/dead"), "a", min_severity="info")
    n = push.PushNotifier(store, _Keys(), send_fn=lambda sub, payload: (False, "410 Gone", True))

    n("event.raised", _env("event.raised"))

    assert store.list() == []


def test_other_failures_are_counted_not_pruned(store):
    store.upsert(_sub("https://push/flaky"), "a", min_severity="info")
    n = push.PushNotifier(store, _Keys(), send_fn=lambda sub, payload: (False, "timeout", False))

    n("event.raised", _env("event.raised"))
    n("event.raised", _env("event.raised"))

    row = store.list()[0]
    assert row["failures"] == 2 and row["last_error"] == "timeout"


def test_nothing_is_sent_when_keys_are_unavailable(store):
    store.upsert(_sub("https://push/1"), "a", min_severity="info")
    keys = _Keys(); keys.available = False
    sent = []
    n = push.PushNotifier(store, keys, send_fn=lambda *a: (sent.append(1), (True, None, False))[1])

    n("event.raised", _env("event.raised"))

    assert sent == []




# --- the VAPID subject (PROXMON's defaultSubject, ported) ---------------
# Apple rejects an invalid `sub` with BadJwtToken, so the fallback chain is
# the difference between iPhones being paged and silently never being.

def test_an_https_url_gives_its_origin():
    assert push.default_vapid_subject(["https://switchboard.example.com/api/auth/callback"]) == "https://switchboard.example.com"


def test_a_plain_http_url_still_names_the_site():
    """The real case: OIDC_REDIRECT_URI left as http:// behind a TLS proxy.
    The hostname is real, so a mailto on it is a valid subject."""
    assert push.default_vapid_subject(["http://switchboard.example.com/api/auth/callback"]) == "mailto:switchboard@switchboard.example.com"


def test_https_wins_over_http_regardless_of_order():
    assert push.default_vapid_subject(["http://a.example.com/x", "https://b.example.com/y"]) == "https://b.example.com"


def test_ips_and_localhost_are_never_used():
    """Exactly what Apple rejects, and what a dev instance is configured with."""
    assert push.default_vapid_subject(["http://192.168.0.147:8080/x"]) == "mailto:switchboard@example.com"
    assert push.default_vapid_subject(["http://localhost:8080/x"]) == "mailto:switchboard@example.com"
    assert push.default_vapid_subject([None, ""]) == "mailto:switchboard@example.com"


def test_an_explicit_override_always_wins():
    assert push.default_vapid_subject(["https://a.example.com"], override="mailto:ops@example.com") == "mailto:ops@example.com"


def test_a_non_default_https_port_is_kept():
    assert push.default_vapid_subject(["https://sb.example.com:8443/x"]) == "https://sb.example.com:8443"
