"""Per-user command history and favourites (ROADMAP Phase 4).

Until this existed, running a command was recorded nowhere durable: the
app had per-user OIDC identity and RBAC, but a run only produced a stdout
log line and a `results` row with no actor column. So "who ran what"
- the question the whole identity change was made to answer - had no
answer for the app's primary action.

Two stores here, sharing a module because they're two halves of the same
Console feature (what I ran / what I want to run again), not because they
share behaviour:

- `CommandHistoryStore` - append-only log of every run, success or
  failure. Written from `_run_and_save` in app.py, the single choke point
  all three run paths (single, bulk, scheduled) already pass through, so
  no path can silently skip being recorded.
- `CommandFavoritesStore` - a user's pinned commands.

Both are scoped by `actor` (the OIDC `preferred_username` the rest of the
app already uses for identity) at the *query* level, not just in the UI:
history and favourites are personal, so the store never returns another
user's rows unless explicitly asked fleet-wide by an admin route.
"""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("webui.command_history")

STATUS_OK = "ok"
STATUS_ERROR = "error"

# Bounds `limit` on the list endpoints. High enough that the History tab
# never feels truncated in normal use, low enough that a hand-crafted
# ?limit=1000000 can't ask Postgres to materialise the entire table.
MAX_LIMIT = 500


def _dumps(params):
    """Params are stored as a JSON string rather than a jsonb column to
    match how every other structured blob in this schema is kept (see
    devices.data, alert_history.labels) - one convention, not two."""
    if not params:
        return None
    try:
        return json.dumps(params, sort_keys=True)
    except (TypeError, ValueError):
        return None


def _loads(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


class CommandHistoryStore:
    def __init__(self, db):
        self.db = db

    def record(self, actor, device_id, device_name, category_id, command_id,
               command, params=None, status=STATUS_OK, error=None,
               duration_ms=None, result_filename=None, source="console"):
        """Never raises. A history write failing must not be able to fail
        the command that was actually run - the same rule audit.py already
        applies for the same reason: the side record of an action is less
        important than the action, and a user watching real switch output
        appear should not see it replaced by an error because a logging
        insert hit a constraint."""
        try:
            self.db.execute(
                """INSERT INTO command_history
                   (ts, actor, device_id, device_name, category_id, command_id, command,
                    params, status, error, duration_ms, result_filename, source)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    datetime.now(timezone.utc).isoformat(), actor, device_id, device_name,
                    category_id, command_id, command, _dumps(params), status,
                    (error or None), duration_ms, result_filename, source,
                ),
            )
        except Exception:
            log.exception("could not record command history for %s by %s", command, actor)

    def list(self, actor=None, device_id=None, status=None, q=None, limit=100, offset=0):
        """Returns (items, total). `total` is the full filtered count
        before limit/offset so the UI can paginate for real rather than
        truncating and calling it the whole set - same contract as
        ResultsStore.list()."""
        where, params = [], []
        if actor is not None:
            where.append("actor = %s")
            params.append(actor)
        if device_id:
            where.append("device_id = %s")
            params.append(device_id)
        if status:
            where.append("status = %s")
            params.append(status)
        if q:
            where.append("(command ILIKE %s OR device_id ILIKE %s OR category_id ILIKE %s)")
            like = f"%{q}%"
            params += [like, like, like]
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM command_history {where_sql}", tuple(params)
        )
        total = total_row["n"] if total_row else 0

        limit = max(1, min(int(limit), MAX_LIMIT))
        rows = self.db.query(
            f"SELECT * FROM command_history {where_sql} ORDER BY ts DESC, id DESC LIMIT %s OFFSET %s",
            tuple(params) + (limit, max(0, int(offset))),
        )
        return [self._to_dict(r) for r in rows], total

    def recent_commands(self, actor, limit=10):
        """The distinct commands this user ran most recently - what the
        Console's "recent" shortcut list is built from. Deduplicated on
        (device, command, params) so hammering Run on one command doesn't
        crowd everything else out of a 10-row list."""
        limit = max(1, min(int(limit), MAX_LIMIT))
        rows = self.db.query(
            """SELECT DISTINCT ON (device_id, category_id, command_id, COALESCE(params, ''))
                      id, ts, actor, device_id, device_name, category_id, command_id,
                      command, params, status, error, duration_ms, result_filename, source
                 FROM command_history
                WHERE actor = %s
                ORDER BY device_id, category_id, command_id, COALESCE(params, ''), ts DESC
            """,
            (actor,),
        )
        items = sorted((self._to_dict(r) for r in rows), key=lambda x: x["ts"], reverse=True)
        return items[:limit]

    def clear(self, actor):
        """Wipes one user's own history. Deliberately scoped to the caller
        - there's no "clear everyone's" here, since audit_log (which is
        append-only and admin-only) is the record that must survive, and
        this one is the user's own working scratch list."""
        self.db.execute("DELETE FROM command_history WHERE actor = %s", (actor,))

    @staticmethod
    def _to_dict(row):
        return {
            "id": row["id"],
            "ts": row["ts"],
            "actor": row["actor"],
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "category_id": row["category_id"],
            "command_id": row["command_id"],
            "command": row["command"],
            "params": _loads(row["params"]),
            "status": row["status"],
            "error": row["error"],
            "duration_ms": row["duration_ms"],
            "result_filename": row["result_filename"],
            "source": row["source"],
        }


class CommandFavoritesStore:
    def __init__(self, db):
        self.db = db

    def add(self, actor, category_id, command_id, device_id=None, params=None, label=None):
        """Idempotent - re-favouriting something already pinned returns the
        existing row instead of erroring or duplicating (see the unique
        index in db.py). Makes the UI's star toggle safe to double-click."""
        self.db.execute(
            """INSERT INTO command_favorites (actor, label, device_id, category_id, command_id, params, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (actor, COALESCE(device_id, ''), category_id, command_id, COALESCE(params, ''))
               DO NOTHING""",
            (actor, label, device_id, category_id, command_id, _dumps(params),
             datetime.now(timezone.utc).isoformat()),
        )
        return self.find(actor, category_id, command_id, device_id, params)

    def find(self, actor, category_id, command_id, device_id=None, params=None):
        row = self.db.query_one(
            """SELECT * FROM command_favorites
                WHERE actor = %s AND category_id = %s AND command_id = %s
                  AND COALESCE(device_id, '') = %s AND COALESCE(params, '') = %s""",
            (actor, category_id, command_id, device_id or "", _dumps(params) or ""),
        )
        return self._to_dict(row) if row else None

    def list(self, actor):
        rows = self.db.query(
            "SELECT * FROM command_favorites WHERE actor = %s ORDER BY created_at DESC", (actor,)
        )
        return [self._to_dict(r) for r in rows]

    def delete(self, actor, favorite_id):
        """Scoped to the owner in the WHERE clause, not just checked in the
        route - deleting by id alone would let any authenticated user
        remove someone else's favourite by guessing a number."""
        self.db.execute(
            "DELETE FROM command_favorites WHERE id = %s AND actor = %s", (favorite_id, actor)
        )

    @staticmethod
    def _to_dict(row):
        return {
            "id": row["id"],
            "actor": row["actor"],
            "label": row["label"],
            "device_id": row["device_id"],
            "category_id": row["category_id"],
            "command_id": row["command_id"],
            "params": _loads(row["params"]),
            "created_at": row["created_at"],
        }
