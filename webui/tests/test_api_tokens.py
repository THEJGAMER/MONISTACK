"""API tokens: the store, against a real Postgres schema per test.

What matters most is one-way: a token must never grant more than its
creator had, and a revoked or expired token must never verify. The clear
text is returned exactly once and only its hash is stored.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

import api_tokens  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")
DDL = """
CREATE TABLE api_tokens (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
    role TEXT NOT NULL, created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ);
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
    schema = f"test_tok_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield api_tokens.ApiTokenStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def test_a_created_token_verifies_to_its_row(store):
    row, token = store.create("ci", "viewer", "admin-user", "admin")

    assert token.startswith("sb_")
    got = store.verify(token)
    assert got["id"] == row["id"] and got["role"] == "viewer"


def test_only_the_hash_is_stored(store):
    _, token = store.create("ci", "viewer", "admin-user", "admin")

    raw = store.db.query_one("SELECT token_hash, prefix FROM api_tokens")
    assert raw["token_hash"] != token
    assert token not in raw["token_hash"]
    assert raw["prefix"] == token[:8], "a short prefix is kept so the list page can identify it"


def test_a_token_cannot_exceed_its_creators_role(store):
    with pytest.raises(ValueError):
        store.create("escalate", "admin", "viewer-user", "viewer")
    with pytest.raises(ValueError):
        store.create("escalate", "operator", "viewer-user", "viewer")
    store.create("same-level", "operator", "op-user", "operator")


def test_a_revoked_token_no_longer_verifies(store):
    row, token = store.create("ci", "viewer", "admin-user", "admin")

    assert store.revoke(row["id"]) is True
    assert store.verify(token) is None
    assert store.revoke(row["id"]) is False, "revoking twice is not a second success"


def test_an_expired_token_no_longer_verifies(store):
    _, token = store.create("short", "viewer", "admin-user", "admin",
                            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    assert store.verify(token) is None


def test_a_future_expiry_still_verifies(store):
    _, token = store.create("long", "viewer", "admin-user", "admin",
                            expires_at=datetime.now(timezone.utc) + timedelta(days=1))

    assert store.verify(token) is not None


def test_garbage_never_verifies(store):
    assert store.verify("") is None
    assert store.verify("sb_") is None
    assert store.verify("sb_" + "x" * 40) is None
    assert store.verify("Bearer sb_whatever") is None


def test_verify_touches_last_used(store):
    row, token = store.create("ci", "viewer", "admin-user", "admin")
    assert store.list()[0]["last_used_at"] is None

    store.verify(token)

    assert store.list()[0]["last_used_at"] is not None


def test_list_never_includes_the_hash(store):
    store.create("ci", "viewer", "admin-user", "admin")

    assert "token_hash" not in store.list()[0]


def test_a_name_is_required(store):
    with pytest.raises(ValueError):
        store.create("   ", "viewer", "admin-user", "admin")
