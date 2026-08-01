"""Saved command results, backed by the shared Postgres database (db.py).

Every command run through /api/run is auto-saved (see app.py) - manual
saves from the UI are just a flag (`auto_saved=0`) distinguishing
"something I ran happened to get logged" from "I deliberately kept this" -
both live in the same table and both are listed/viewed/deleted the same
way. Each row also carries a rendered Markdown snapshot (`render_markdown`)
so viewing a result never needs to re-derive its formatting later.
"""
import re
import time
from datetime import datetime, timezone


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def render_markdown(device_id, device_name, host, command, summary, output, generated_at):
    parts = [
        f"## {device_name} — `{command}`",
        "",
        f"**Device:** {device_name} ({host})  ",
        f"**Device ID:** `{device_id}`  ",
        f"**Command:** `{command}`  ",
        f"**Generated:** {generated_at}",
    ]
    if summary:
        parts.append(f"\n**Summary:** {summary}")
    parts += ["", "```text", output or "(no output)", "```", ""]
    return "\n".join(parts)


class ResultsStore:
    def __init__(self, db):
        self.db = db

    def save(self, device_id, device_name, host, category_id, command_id, command, summary, output, auto_saved=False):
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"
        filename = f"{timestamp}_{_slug(device_name)}-{category_id}-{command_id}.md"
        generated_at = now.isoformat()
        markdown = render_markdown(device_id, device_name, host, command, summary, output, generated_at)
        self.db.execute(
            """INSERT INTO results
               (filename, device_id, device_name, host, category_id, command_id, command, summary, output,
                markdown, auto_saved, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                filename, device_id, device_name, host, category_id, command_id, command, summary, output,
                markdown, 1 if auto_saved else 0, generated_at,
            ),
        )
        return {"filename": filename, "generated_at": generated_at}

    def list(self, device_id=None, limit=200, offset=0, q=None):
        """Returns (items, total). `total` reflects the full filtered set
        (device_id/q applied) before limit/offset, so callers can drive
        real server-side pagination instead of truncating at `limit` and
        pretending that's everything - which is what this used to do."""
        where = []
        params = []
        if device_id is not None:
            where.append("device_id = %s")
            params.append(device_id)
        if q:
            where.append("(command ILIKE %s OR device_id ILIKE %s OR filename ILIKE %s)")
            like = f"%{q}%"
            params += [like, like, like]
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        total_row = self.db.query_one(f"SELECT COUNT(*) AS n FROM results {where_sql}", tuple(params))
        total = total_row["n"] if total_row else 0

        rows = self.db.query(
            f"SELECT filename, device_id, command, auto_saved, created_at, length(markdown) AS size "
            f"FROM results {where_sql} ORDER BY filename DESC LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        )
        items = []
        for r in rows:
            items.append(
                {
                    "filename": r["filename"],
                    "title": f"{r['device_id']} — {r['command']}",
                    "device_id": r["device_id"],
                    "auto_saved": bool(r["auto_saved"]),
                    "size": r["size"],
                    "saved_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.strptime(r["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
                    ),
                }
            )
        return items, total

    def read(self, filename):
        row = self.db.query_one("SELECT markdown FROM results WHERE filename = %s", (filename,))
        return row["markdown"] if row else None

    def delete(self, filename):
        cur = self.db.execute("DELETE FROM results WHERE filename = %s", (filename,))
        return cur.rowcount > 0
