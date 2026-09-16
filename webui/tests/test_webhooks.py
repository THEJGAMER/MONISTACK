"""Outbound webhooks: signing, delivery/retry, and the event-bus dispatcher.

The property a receiver relies on is the signature: the same secret and
the same bytes must produce the same HMAC, and a tampered body must not
verify. Delivery is tested against a fake opener so no network is
touched, and the retry rules - transient errors retry, client errors do
not - are pinned because getting them backwards either hammers a broken
receiver or gives up on a flaky one.
"""
import json
import os
import sys
import urllib.error
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import events  # noqa: E402
import webhooks  # noqa: E402


# --- signing --------------------------------------------------------

def test_signature_round_trips():
    body = b'{"event":"event.raised"}'
    sig = webhooks.sign("s3cret", body)

    assert sig.startswith("sha256=")
    assert webhooks.verify_signature("s3cret", body, sig)


def test_a_tampered_body_does_not_verify():
    sig = webhooks.sign("s3cret", b'{"a":1}')

    assert not webhooks.verify_signature("s3cret", b'{"a":2}', sig)
    assert not webhooks.verify_signature("other", b'{"a":1}', sig)
    assert not webhooks.verify_signature("s3cret", b'{"a":1}', None)


# --- delivery -------------------------------------------------------

class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(outcomes, seen):
    def open_(req, timeout=None):
        seen.append(req)
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return _Resp(out)
    return open_


def test_a_2xx_is_a_success_first_time(monkeypatch):
    seen = []
    status, err = webhooks.deliver("http://x/h", "s", {"event": "e"}, opener=_opener([204], seen), sleep=lambda s: None)

    assert (status, err) == (204, None)
    assert len(seen) == 1
    req = seen[0]
    assert req.get_header("X-switchboard-event") == "e"
    assert req.get_header("X-switchboard-signature") == webhooks.sign("s", req.data)
    assert json.loads(req.data)["event"] == "e"


def test_a_transient_failure_is_retried_then_succeeds():
    seen = []
    outcomes = [TimeoutError("slow"), urllib.error.HTTPError("u", 503, "x", {}, None), 200]
    status, err = webhooks.deliver("http://x/h", "s", {"event": "e"}, opener=_opener(outcomes, seen), sleep=lambda s: None)

    assert (status, err) == (200, None)
    assert len(seen) == 3


def test_a_client_error_is_not_retried():
    """A 404 or a 401 will not become a 200 by asking again; retrying only
    hammers a receiver that has already said no."""
    seen = []
    status, err = webhooks.deliver("http://x/h", "s", {"event": "e"},
                                   opener=_opener([urllib.error.HTTPError("u", 404, "x", {}, None), 200], seen),
                                   sleep=lambda s: None)

    assert status == 404 and "404" in err
    assert len(seen) == 1


def test_429_is_the_client_error_that_is_retried():
    seen = []
    status, err = webhooks.deliver("http://x/h", "s", {"event": "e"},
                                   opener=_opener([urllib.error.HTTPError("u", 429, "x", {}, None), 200], seen),
                                   sleep=lambda s: None)

    assert (status, err) == (200, None)
    assert len(seen) == 2


def test_all_attempts_failing_reports_the_last_error():
    seen = []
    status, err = webhooks.deliver("http://x/h", "s", {"event": "e"},
                                   opener=_opener([ConnectionRefusedError("no"), ConnectionRefusedError("no"),
                                                   ConnectionRefusedError("still no")], seen),
                                   sleep=lambda s: None)

    assert status is None and "still no" in err
    assert len(seen) == 3


# --- the store + dispatcher against a real Postgres ------------------

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")
DDL = """
CREATE TABLE webhooks (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
    events TEXT NOT NULL DEFAULT '["*"]', enabled INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_delivery_at TIMESTAMPTZ, last_status INTEGER,
    last_error TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0);
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
    schema = f"test_wh_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield webhooks.WebhookStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def test_the_secret_is_returned_once_and_never_listed(store):
    row = store.create("n", "https://x/h", ["event.raised"], "admin")

    assert row["secret"]
    assert store.list()[0]["secret"] is None
    assert store.get(row["id"])["secret"] is None


def test_unknown_events_and_bad_urls_are_refused(store):
    with pytest.raises(ValueError):
        store.create("n", "https://x/h", ["alarm.exploded"], "admin")
    with pytest.raises(ValueError):
        store.create("n", "ftp://x/h", ["*"], "admin")


def test_dispatcher_delivers_only_to_matching_enabled_hooks(store):
    a = store.create("all", "https://x/all", ["*"], "admin")
    b = store.create("acks", "https://x/acks", ["event.resolved"], "admin")
    c = store.create("off", "https://x/off", ["*"], "admin")
    store.update(c["id"], enabled=False)
    sent = []
    d = webhooks.WebhookDispatcher(store, deliver_fn=lambda url, secret, env: (sent.append((url, env["event"])), (200, None))[1])

    d("event.raised", {"event": "event.raised"})

    assert sent == [("https://x/all", "event.raised")]


def test_dispatcher_records_the_outcome_on_the_row(store):
    h = store.create("n", "https://x/h", ["*"], "admin")
    d = webhooks.WebhookDispatcher(store, deliver_fn=lambda *a: (503, "HTTP 503"))

    d("event.raised", {"event": "event.raised"})
    d("event.raised", {"event": "event.raised"})
    row = store.get(h["id"])
    assert row["consecutive_failures"] == 2 and row["last_status"] == 503

    webhooks.WebhookDispatcher(store, deliver_fn=lambda *a: (200, None))("event.raised", {"event": "event.raised"})
    row = store.get(h["id"])
    assert row["consecutive_failures"] == 0 and row["last_error"] is None


def test_it_plugs_into_the_event_bus(store):
    h = store.create("n", "https://x/h", ["command.ran"], "admin")
    got = []
    d = webhooks.WebhookDispatcher(store, deliver_fn=lambda url, secret, env: (got.append(env), (200, None))[1])
    bus = events.EventBus()
    bus.subscribe(d)

    bus.emit("command.ran", device="s4048", command="show version")
    bus.emit("event.raised", occurrence={"id": 1})
    bus.drain_now()

    assert [g["event"] for g in got] == ["command.ran"]
    assert got[0]["device"] == "s4048" and "at" in got[0]
