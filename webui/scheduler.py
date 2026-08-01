"""Scheduled/recurring command runs (ROADMAP 3.6). A schedule is just
(device, category/command, params, interval) - the background loop in
app.py polls `due()` and hands each due schedule to the same
`_run_and_save()` helper `/api/run` and bulk-run use, so scheduled output
lands in the normal `results` table (auto_saved=1) and shows up in Saved
Results/Console like any other run - no separate "scheduled output" log
to build or view.
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class ScheduleStore:
    def __init__(self, db):
        self.db = db

    def list(self):
        rows = self.db.query("SELECT * FROM schedules ORDER BY created_at DESC")
        return [self._to_dict(r) for r in rows]

    def get(self, schedule_id):
        row = self.db.query_one("SELECT * FROM schedules WHERE id = %s", (schedule_id,))
        return self._to_dict(row) if row else None

    def create(self, device_id, category_id, command_id, params, interval_minutes):
        schedule_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
        self.db.execute(
            """INSERT INTO schedules
               (id, device_id, category_id, command_id, params, interval_minutes, enabled,
                last_run_at, last_error, next_run_at, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, 1, NULL, NULL, %s, %s)""",
            (schedule_id, device_id, category_id, command_id, json.dumps(params or {}), interval_minutes, next_run, now),
        )
        return self.get(schedule_id)

    def update(self, schedule_id, enabled=None, interval_minutes=None):
        if enabled is not None:
            self.db.execute("UPDATE schedules SET enabled = %s WHERE id = %s", (1 if enabled else 0, schedule_id))
        if interval_minutes is not None:
            self.db.execute(
                "UPDATE schedules SET interval_minutes = %s WHERE id = %s", (interval_minutes, schedule_id)
            )
        return self.get(schedule_id)

    def delete(self, schedule_id):
        cur = self.db.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        return cur.rowcount > 0

    def due(self):
        """Schedules that are enabled and past their next_run_at - the
        caller (app.py's background loop) is responsible for actually
        running them and calling mark_run() afterward."""
        rows = self.db.query(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= %s", (_now_iso(),)
        )
        return [self._to_dict(r) for r in rows]

    def mark_run(self, schedule_id, interval_minutes, error=None):
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
        self.db.execute(
            "UPDATE schedules SET last_run_at = %s, last_error = %s, next_run_at = %s WHERE id = %s",
            (_now_iso(), error, next_run, schedule_id),
        )

    @staticmethod
    def _to_dict(row):
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "category_id": row["category_id"],
            "command_id": row["command_id"],
            "params": json.loads(row["params"]) if row["params"] else {},
            "interval_minutes": row["interval_minutes"],
            "enabled": bool(row["enabled"]),
            "last_run_at": row["last_run_at"],
            "last_error": row["last_error"],
            "next_run_at": row["next_run_at"],
            "created_at": row["created_at"],
        }
