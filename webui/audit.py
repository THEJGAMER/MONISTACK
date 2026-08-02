"""Audit / event log - what *people* did in Switchboard, as opposed to
alert_history's record of what the *system* did (notifications Alertmanager
sent out).

Every operator-initiated mutation goes through here: acknowledging or
manually resolving an alert, creating or expiring a maintenance window,
changing an alert rule's severity, changing a port's interface-alert
config. Previously all of that was visible only as container log lines,
which are unqueryable from the UI and gone the moment the container is
recreated (something that happens routinely here - a webui recreate during
the 2026-08-01 alerting work is what orphaned a live alert and kicked off
that whole investigation).

Deliberately append-only and best-effort: `record()` never raises. An audit
write failing must not be able to fail the action being audited - refusing
to acknowledge a live alert because a logging insert failed would be a
worse outcome than a gap in the log, and the gap is visible either way
(the action still shows up in alert_history / Alertmanager).
"""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("webui.audit")


class AuditLog:
    def __init__(self, db):
        self.db = db

    def record(self, actor, action, target=None, detail=None, fingerprint=None, occurrence_id=None):
        """`action` is a short stable verb ("alert.ack", "silence.create")
        - stable so the log stays filterable as the UI wording changes.
        `target` is what it acted on (an alert name, a port, a rule).
        `detail` is a free-form dict of specifics, stored as JSON.
        `fingerprint` ties alert-related entries to one specific alarm
        identity, which is what the per-alarm timeline view queries on."""
        try:
            self.db.execute(
                "INSERT INTO audit_log (ts, actor, action, target, detail, fingerprint, occurrence_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    actor,
                    action,
                    target,
                    json.dumps(detail) if detail is not None else None,
                    fingerprint,
                    occurrence_id,
                ),
            )
        except Exception:
            log.exception("could not write audit log entry for %s by %s", action, actor)

    def list(self, limit=200, action_prefix=None, fingerprint=None, occurrence_id=None):
        limit = max(1, min(limit, 1000))
        clauses, params = [], []
        if action_prefix:
            clauses.append("action LIKE %s")
            params.append(f"{action_prefix}%")
        if fingerprint:
            clauses.append("fingerprint = %s")
            params.append(fingerprint)
        if occurrence_id is not None:
            clauses.append("occurrence_id = %s")
            params.append(occurrence_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(f"SELECT * FROM audit_log {where} ORDER BY ts DESC LIMIT %s", tuple(params))
        return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(row):
        return {
            "id": row["id"],
            "ts": row["ts"],
            "actor": row["actor"],
            "action": row["action"],
            "target": row["target"],
            "detail": json.loads(row["detail"]) if row["detail"] else None,
            "fingerprint": row["fingerprint"],
            "occurrence_id": row["occurrence_id"],
        }
