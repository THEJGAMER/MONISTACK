"""Alarm occurrences - one record per fired-to-resolved episode.

An occurrence is the unit of record here, not the alarm signature. If a
port flaps four times that is four separate occurrences, each with its own
id, acknowledgement, discussion thread and audit trail. Merging them into a
single long-lived row per signature destroys exactly the information an
operational log exists to keep: you can no longer say who handled the
second one versus the fourth, or what was found each time.

Occurrences of the same alarm are *linked*, not merged. `signature` (the
label-set fingerprint from alert_acks.fingerprint_for) groups them, so a
new occurrence can show its predecessors - the same shape a ticketing
system uses when it opens a fresh ticket and references prior ones rather
than reopening something already closed. That linkage is also what lets an
external ticketing system, fed from this, decide for itself whether to
reopen or cross-reference.

Every alerting path in this app funnels through Alertmanager's webhook
(app.py's /api/alertmanager/webhook) - Prometheus rules and the
directly-posted per-interface alerts alike - so opening and closing
occurrences from that one place covers both without a second code path.
"""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("webui.occurrences")


class OccurrenceStore:
    def __init__(self, db):
        self.db = db

    # --- lifecycle -------------------------------------------------

    def open(self, signature, alertname, severity, summary, labels, started_at=None):
        """Opens a new occurrence, or returns the existing open one.

        A repeated "firing" for an alarm that is already open is the same
        episode being re-notified - Alertmanager's repeat_interval, or
        interface_alerting.py's own heartbeat re-post - not a new
        occurrence. The partial unique index in db.py enforces this even
        if two webhook deliveries race; ON CONFLICT makes that a no-op
        rather than a 500."""
        started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO alert_occurrences (signature, alertname, severity, summary, labels, started_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (signature) WHERE resolved_at IS NULL DO NOTHING""",
            (signature, alertname, severity, summary, json.dumps(labels), started_at),
        )
        return self.open_for(signature)

    def close(self, signature, resolved_at=None):
        """Closes the open occurrence for a signature, if there is one. A
        resolve for something with nothing open is ignored rather than
        creating a phantom zero-length occurrence - it usually means a
        resolve arrived after a restart that lost nothing, or a duplicate
        resolve notification.

        Always finalizes paging bookkeeping here rather than leaving it to
        whoever calls close() to remember to. Confirmed live this was a
        real bug: an occurrence that resolved *inside* its paging hold (the
        alarm recovered before page_at) kept its now-stale, already-past
        page_at forever, so the UI showed "paging now..." on an alarm that
        had been resolved for hours and never actually paged. The fix has
        two halves: `page_at` is always cleared on close (there is no
        countdown left to show once an alarm is resolved), and `paged_at`
        is backfilled if `page_at` had already lapsed by the time this
        closed - Alertmanager's silence would have auto-expired at that
        same moment and let the real notification through, so the record
        should say "paged", not leave both fields empty as if paging had
        somehow been skipped entirely."""
        resolved_at = resolved_at or datetime.now(timezone.utc).isoformat()
        row = self.open_for(signature)
        if row is None:
            return None
        paged_at = row["paged_at"]
        if paged_at is None and row["page_at"] and not row["paging_disabled"]:
            if row["page_at"] <= resolved_at:  # ISO 8601 strings sort chronologically
                paged_at = row["page_at"]
        self.db.execute(
            "UPDATE alert_occurrences SET resolved_at = %s, page_at = NULL, paged_at = %s WHERE id = %s",
            (resolved_at, paged_at, row["id"]),
        )
        return self.get(row["id"])

    def open_for(self, signature):
        row = self.db.query_one(
            "SELECT * FROM alert_occurrences WHERE signature = %s AND resolved_at IS NULL", (signature,)
        )
        return self._to_dict(row) if row else None

    # --- reads -----------------------------------------------------

    def get(self, occurrence_id):
        row = self.db.query_one("SELECT * FROM alert_occurrences WHERE id = %s", (occurrence_id,))
        return self._to_dict(row) if row else None

    def list(self, limit=200, signature=None, open_only=False):
        limit = max(1, min(limit, 1000))
        clauses, params = [], []
        if signature:
            clauses.append("signature = %s")
            params.append(signature)
        if open_only:
            clauses.append("resolved_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM alert_occurrences {where} ORDER BY started_at DESC LIMIT %s", tuple(params)
        )
        return [self._to_dict(r) for r in rows]

    def previous_for(self, signature, before_id, limit=20):
        """Earlier occurrences of the same alarm - what makes a new one
        readable ("this has happened 3 times before, here they are")
        without pretending it's the same record."""
        rows = self.db.query(
            "SELECT * FROM alert_occurrences WHERE signature = %s AND id < %s ORDER BY started_at DESC LIMIT %s",
            (signature, before_id, limit),
        )
        return [self._to_dict(r) for r in rows]

    def count_for(self, signature):
        row = self.db.query_one(
            "SELECT count(*) AS n FROM alert_occurrences WHERE signature = %s", (signature,)
        )
        return row["n"] if row else 0

    # --- paging ----------------------------------------------------

    def set_paging(self, occurrence_id, page_at, silence_id):
        self.db.execute(
            "UPDATE alert_occurrences SET page_at = %s, silence_id = %s WHERE id = %s",
            (page_at, silence_id, occurrence_id),
        )
        return self.get(occurrence_id)

    def mark_paged(self, occurrence_id, when=None):
        self.db.execute(
            "UPDATE alert_occurrences SET paged_at = %s, page_at = NULL, silence_id = NULL WHERE id = %s",
            (when or datetime.now(timezone.utc).isoformat(), occurrence_id),
        )
        return self.get(occurrence_id)

    def set_paging_disabled(self, occurrence_id, disabled, silence_id=None):
        self.db.execute(
            "UPDATE alert_occurrences SET paging_disabled = %s, page_at = NULL, silence_id = %s WHERE id = %s",
            (1 if disabled else 0, silence_id, occurrence_id),
        )
        return self.get(occurrence_id)

    def due_to_page(self):
        """Open occurrences whose hold has lapsed - Alertmanager has (or is
        about to) let them through, so they count as paged. Read on a tick
        purely to keep the record honest; the actual delivery is
        Alertmanager's, not ours."""
        rows = self.db.query(
            "SELECT * FROM alert_occurrences WHERE resolved_at IS NULL AND paged_at IS NULL "
            "AND paging_disabled = 0 AND page_at IS NOT NULL AND page_at <= %s",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return [self._to_dict(r) for r in rows]

    # --- acknowledgement -------------------------------------------

    def ack(self, occurrence_id, acked_by, note=None):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO alarm_acks (occurrence_id, acked_by, acked_at, note) VALUES (%s, %s, %s, %s)
               ON CONFLICT (occurrence_id) DO UPDATE SET
                 acked_by = EXCLUDED.acked_by, acked_at = EXCLUDED.acked_at, note = EXCLUDED.note""",
            (occurrence_id, acked_by, now, note),
        )
        return self.ack_for(occurrence_id)

    def unack(self, occurrence_id):
        return self.db.execute("DELETE FROM alarm_acks WHERE occurrence_id = %s", (occurrence_id,)).rowcount > 0

    def ack_for(self, occurrence_id):
        row = self.db.query_one("SELECT * FROM alarm_acks WHERE occurrence_id = %s", (occurrence_id,))
        return dict(row) if row else None

    def acks_by_occurrence(self, ids):
        """One query for a page of occurrences - the list view refreshes on
        a timer, so a per-row lookup would be N round trips per render."""
        if not ids:
            return {}
        rows = self.db.query("SELECT * FROM alarm_acks WHERE occurrence_id = ANY(%s)", (list(ids),))
        return {r["occurrence_id"]: dict(r) for r in rows}

    # --- comments --------------------------------------------------

    def comments_for(self, occurrence_id):
        rows = self.db.query(
            "SELECT id, author, body, created_at FROM alarm_comments WHERE occurrence_id = %s ORDER BY created_at ASC",
            (occurrence_id,),
        )
        return [dict(r) for r in rows]

    def comment_counts(self, ids):
        if not ids:
            return {}
        rows = self.db.query(
            "SELECT occurrence_id, count(*) AS n FROM alarm_comments WHERE occurrence_id = ANY(%s) GROUP BY occurrence_id",
            (list(ids),),
        )
        return {r["occurrence_id"]: r["n"] for r in rows}

    def add_comment(self, occurrence_id, author, body):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO alarm_comments (occurrence_id, author, body, created_at) VALUES (%s, %s, %s, %s)",
            (occurrence_id, author, body, now),
        )
        return {"occurrence_id": occurrence_id, "author": author, "body": body, "created_at": now}

    def get_comment(self, occurrence_id, comment_id):
        row = self.db.query_one(
            "SELECT * FROM alarm_comments WHERE id = %s AND occurrence_id = %s", (comment_id, occurrence_id)
        )
        return dict(row) if row else None

    def delete_comment(self, comment_id):
        self.db.execute("DELETE FROM alarm_comments WHERE id = %s", (comment_id,))

    # --- migration -------------------------------------------------

    def repair_stale_paging_on_resolved(self):
        """One-time repair for rows closed before close() started clearing
        `page_at` itself (see close()'s docstring - confirmed live: an
        alarm resolved for hours was still showing "paging now..." because
        nothing had ever cleared its now-long-past countdown target).
        close() fixes this for every occurrence closed from here on; this
        repairs the ones that were already broken when that fix shipped.
        Same finalization logic as close() - backfill paged_at if page_at
        had already lapsed by resolution, otherwise leave it unpaged - just
        applied after the fact instead of at close time. Safe to run every
        startup: a resolved row with no page_at is already correct and
        won't match the WHERE clause."""
        rows = self.db.query(
            "SELECT id, page_at, paged_at, resolved_at, paging_disabled FROM alert_occurrences "
            "WHERE resolved_at IS NOT NULL AND page_at IS NOT NULL"
        )
        repaired = 0
        for row in rows:
            paged_at = row["paged_at"]
            if paged_at is None and not row["paging_disabled"] and row["page_at"] <= row["resolved_at"]:
                paged_at = row["page_at"]
            self.db.execute(
                "UPDATE alert_occurrences SET page_at = NULL, paged_at = %s WHERE id = %s",
                (paged_at, row["id"]),
            )
            repaired += 1
        if repaired:
            log.info("repaired stale paging state on %d already-resolved alarm(s)", repaired)
        return repaired

    def backfill_from_history(self, fingerprint_for):
        """Reconstructs occurrences from the existing alert_history rows.

        Without this, every alarm that fired before occurrences existed
        would be invisible in the alarm log - an install with real history
        would look like nothing had ever happened. Replays each signature's
        notifications in order: a "firing" with nothing open starts an
        occurrence, a "resolved" closes the open one, and a repeated
        "firing" while one is already open is the same episode being
        re-notified, not a new one. Runs once - a no-op afterwards, since
        it only fires when the table is empty."""
        existing = self.db.query_one("SELECT count(*) AS n FROM alert_occurrences")
        if existing and existing["n"]:
            return 0
        rows = self.db.query(
            "SELECT alertname, status, severity, summary, labels, received_at FROM alert_history "
            "ORDER BY received_at ASC"
        )
        open_by_signature = {}
        created = 0
        for r in rows:
            try:
                labels = json.loads(r["labels"])
            except (json.JSONDecodeError, TypeError):
                continue
            signature = fingerprint_for(labels)
            if r["status"] == "firing":
                if signature in open_by_signature:
                    continue  # already open - a re-notification of the same episode
                self.db.execute(
                    """INSERT INTO alert_occurrences (signature, alertname, severity, summary, labels, started_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (signature, r["alertname"], r["severity"], r["summary"], r["labels"], r["received_at"]),
                )
                open_by_signature[signature] = True
                created += 1
            elif r["status"] == "resolved" and signature in open_by_signature:
                self.db.execute(
                    "UPDATE alert_occurrences SET resolved_at = %s "
                    "WHERE signature = %s AND resolved_at IS NULL",
                    (r["received_at"], signature),
                )
                open_by_signature.pop(signature, None)
        log.info("backfilled %d alarm occurrence(s) from alert history", created)
        return created

    @staticmethod
    def _to_dict(row):
        return {
            "id": row["id"],
            "signature": row["signature"],
            "alertname": row["alertname"],
            "severity": row["severity"],
            "summary": row["summary"],
            "labels": json.loads(row["labels"]),
            "started_at": row["started_at"],
            "resolved_at": row["resolved_at"],
            "page_at": row["page_at"],
            "paged_at": row["paged_at"],
            "paging_disabled": bool(row["paging_disabled"]),
            "silence_id": row["silence_id"],
        }
