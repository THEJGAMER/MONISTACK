"""API tokens: bearer credentials for scripts and integrations.

A token is `sb_` + 40 random URL-safe characters. Only its SHA-256 is
stored, so the clear text exists exactly once - in the response that
created it - and a database read cannot mint a login. Lookup is by hash,
which also means there is nothing to timing-attack: the comparison is an
indexed equality on a digest, not a byte-by-byte string compare.

Every token carries a role, and it can never exceed the role of whoever
created it: a viewer cannot hand out an admin token to a script. Tokens
can expire and can be revoked; a revoked token is kept (not deleted) so
the audit trail can still name it.
"""
import hashlib
import secrets
from datetime import datetime, timezone

PREFIX = "sb_"
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_token(value):
    return isinstance(value, str) and value.startswith(PREFIX) and len(value) > len(PREFIX) + 20


class ApiTokenStore:
    def __init__(self, db):
        self.db = db

    def create(self, name, role, created_by, creator_role, expires_at=None):
        """Returns (row, clear_text_token). The clear text is not stored."""
        name = (name or "").strip()
        if not name:
            raise ValueError("a token needs a name")
        if role not in ROLE_RANK:
            raise ValueError(f"unknown role {role!r}")
        if ROLE_RANK[role] > ROLE_RANK.get(creator_role, 0):
            raise ValueError(f"cannot create a {role} token with the {creator_role} role")
        token = PREFIX + secrets.token_urlsafe(30)
        row = self.db.query_one(
            """INSERT INTO api_tokens (name, token_hash, prefix, role, created_by, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id, name, prefix, role, created_by, created_at, expires_at, last_used_at, revoked_at""",
            (name, _hash(token), token[:8], role, created_by, expires_at),
        )
        return dict(row), token

    def verify(self, token):
        """The row for a live token, or None. Touches last_used_at."""
        if not looks_like_token(token):
            return None
        row = self.db.query_one(
            """SELECT id, name, prefix, role, created_by, expires_at, revoked_at
                 FROM api_tokens WHERE token_hash = %s""",
            (_hash(token),),
        )
        if not row:
            return None
        row = dict(row)
        now = datetime.now(timezone.utc)
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] is not None and row["expires_at"] <= now:
            return None
        self.db.execute("UPDATE api_tokens SET last_used_at = now() WHERE id = %s", (row["id"],))
        return row

    def list(self):
        rows = self.db.query(
            """SELECT id, name, prefix, role, created_by, created_at, expires_at, last_used_at, revoked_at
                 FROM api_tokens ORDER BY created_at DESC"""
        )
        return [dict(r) for r in rows]

    def revoke(self, token_id):
        cur = self.db.execute(
            "UPDATE api_tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (int(token_id),)
        )
        return getattr(cur, "rowcount", 0) > 0
