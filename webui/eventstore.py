"""Events: what happened, how bad, and whether it is over.

One row per episode: raised once, bumped (count, last_seen_at) while the
same thing keeps being reported, resolved once - and *re-opened* rather
than duplicated when the same fault returns soon after it cleared. A port
unplugged five times in a minute is one event that reopened four times,
not five rows to read through. A partial unique index
keeps one open row per signature (kind + device + subject) so the syslog
path and the SSH fallback converge on the same event instead of two.
Hooks fire exactly once per transition, decided by the SQL (INSERT ...
ON CONFLICT DO NOTHING RETURNING; UPDATE ... WHERE resolved_at IS NULL
RETURNING), whichever path drove it - the lesson from the alarm era,
where events emitted from call sites missed most real alarms.

There is no acknowledgement, comment, hold or silence here on purpose:
actioning an event is the ticketing system's job. Events reach it via
webhooks (`event.raised` / `event.resolved`) and the API.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import event_catalog

log = logging.getLogger("webui.events.store")


# How soon a returning fault counts as the same episode. Long enough to
# absorb a flap (the thing that produces piles of near-identical rows),
# short enough that this morning's outage and this afternoon's are two
# events. The flapping kinds exist to say "this keeps happening"; this
# just stops it filling the list.
REOPEN_WITHIN_SECONDS = int(os.environ.get("EVENT_REOPEN_WITHIN_SECONDS", "900"))


def signature_for(kind, device_key, subject):
    parts = f"{kind}\0{device_key or ''}\0{subject or ''}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def _iso(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.isoformat()


class EventStore:
    def __init__(self, db):
        self.db = db
        self.on_raised = None      # fn(event)
        self.on_resolved = None    # fn(event)

    def _hook(self, fn, event):
        if fn is None:
            return
        try:
            fn(event)
        except Exception:
            log.exception("event hook %s failed", getattr(fn, "__name__", fn))

    @staticmethod
    def _to_dict(row):
        if row is None:
            return None
        try:
            labels = json.loads(row["labels"] or "{}")
        except ValueError:
            labels = {}
        entry = event_catalog.BY_KIND.get(row["kind"], {})
        return {
            "id": row["id"], "signature": row["signature"], "kind": row["kind"],
            "kind_name": entry.get("name", row["kind"]), "group": entry.get("group", row["kind"].split(".")[0]),
            "severity": row["severity"], "device_id": row["device_id"], "device": row["device"],
            "subject": row["subject"], "title": row["title"], "detail": row["detail"], "labels": labels,
            "source": row["source"], "signal_at": row["signal_at"],
            "raised_at": _iso(row["raised_at"]), "last_seen_at": _iso(row["last_seen_at"]), "count": row["count"],
            "resolved_at": _iso(row["resolved_at"]), "resolved_by": row["resolved_by"], "resolve_detail": row["resolve_detail"],
            "reopen_count": row["reopen_count"] if "reopen_count" in row else 0,
            "reopened_at": _iso(row["reopened_at"]) if "reopened_at" in row else None,
        }

    # --- transitions ------------------------------------------------------

    def _bump(self, signature, detail):
        """Same thing reported again while it is open: count it, quietly."""
        row = self.db.query_one(
            """UPDATE events SET count = count + 1, last_seen_at = now(), detail = COALESCE(%s, detail)
               WHERE signature = %s AND resolved_at IS NULL RETURNING *""",
            (detail, signature),
        )
        return self._to_dict(row)

    def raise_event(self, kind, severity, device_id, device, subject, title, detail=None, labels=None,
                    source="syslog", signal_at=None, reopen_within=None):
        """Open an event, bump the open one, or re-open the episode that
        just closed. Returns (event, is_news) - is_news is True when
        something happened worth telling anyone about (a new episode or a
        return), False when it is the same thing being reported again.

        Order matters and is the whole of the de-duplication: bump what is
        open, else re-open what closed recently, else start a new episode.
        Inserting first would make every flap a new row."""
        if severity not in event_catalog.SEVERITIES:
            raise ValueError(f"severity must be one of {event_catalog.SEVERITIES}")
        signature = signature_for(kind, device_id or device, subject)
        detail = (detail or "")[:4000] or None
        window = REOPEN_WITHIN_SECONDS if reopen_within is None else reopen_within

        row = self._bump(signature, detail)
        if row is not None:
            return row, False

        if window > 0:
            # The NOT EXISTS guard keeps this from colliding with the
            # partial unique index if something opened in between; the
            # index is still the authority, so a lost race falls through
            # to a bump rather than erroring.
            try:
                row = self.db.query_one(
                    """UPDATE events SET resolved_at = NULL, resolved_by = NULL, resolve_detail = NULL,
                              reopen_count = reopen_count + 1, reopened_at = now(), count = count + 1,
                              last_seen_at = now(), severity = %s, source = %s, signal_at = %s,
                              title = %s, detail = COALESCE(%s, detail)
                       WHERE id = (SELECT id FROM events
                                    WHERE signature = %s AND resolved_at IS NOT NULL
                                      AND resolved_at > now() - (%s || ' seconds')::interval
                                      AND NOT EXISTS (SELECT 1 FROM events WHERE signature = %s AND resolved_at IS NULL)
                                    ORDER BY id DESC LIMIT 1)
                       RETURNING *""",
                    (severity, source, signal_at, title, detail, signature, str(int(window)), signature),
                )
            except Exception:
                log.warning("re-open lost a race for %s; treating as a fresh report", signature, exc_info=True)
                row = None
            if row is not None:
                event = self._to_dict(row)
                self._hook(self.on_raised, event)
                return event, True

        row = self.db.query_one(
            """INSERT INTO events (signature, kind, severity, device_id, device, subject, title, detail, labels, source, signal_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (signature) WHERE resolved_at IS NULL DO NOTHING
               RETURNING *""",
            (signature, kind, severity, device_id or None, device or "unknown", subject or "", title,
             detail, json.dumps(labels or {}), source, signal_at),
        )
        if row is not None:
            event = self._to_dict(row)
            self._hook(self.on_raised, event)
            return event, True
        return self._bump(signature, detail), False

    def resolve(self, signature, by="syslog", detail=None):
        row = self.db.query_one(
            """UPDATE events SET resolved_at = now(), resolved_by = %s, resolve_detail = %s
               WHERE signature = %s AND resolved_at IS NULL RETURNING *""",
            (by, (detail or "")[:2000] or None, signature),
        )
        if row is None:
            return None
        event = self._to_dict(row)
        self._hook(self.on_resolved, event)
        return event

    def resolve_id(self, event_id, by, detail=None):
        row = self.db.query_one("SELECT signature FROM events WHERE id = %s AND resolved_at IS NULL", (int(event_id),))
        return self.resolve(row["signature"], by, detail) if row else None

    def resolve_kind(self, kind, device_id, subject, by="ssh", detail=None):
        return self.resolve(signature_for(kind, device_id, subject), by, detail)

    def resolve_open(self, kind=None, device_id=None, rule_id=None, by="switchboard", detail=None):
        """Resolve every open event matching; returns how many."""
        clauses, params = ["resolved_at IS NULL"], []
        if kind:
            clauses.append("kind = %s"); params.append(kind)
        if device_id:
            clauses.append("device_id = %s"); params.append(device_id)
        if rule_id is not None:
            clauses.append("labels::jsonb ->> 'rule_id' = %s"); params.append(str(rule_id))
        rows = self.db.query(f"SELECT signature FROM events WHERE {' AND '.join(clauses)}", tuple(params))
        n = 0
        for r in rows:
            if self.resolve(r["signature"], by, detail):
                n += 1
        return n

    def expire(self, kind, ttl_seconds, by="timer", rule_id=None):
        """Resolve open events of a kind not re-reported for ttl_seconds."""
        if not ttl_seconds or ttl_seconds <= 0:
            return 0
        clauses = ["resolved_at IS NULL", "kind = %s", "last_seen_at < now() - (%s || ' seconds')::interval"]
        params = [kind, str(int(ttl_seconds))]
        if rule_id is not None:
            clauses.append("labels::jsonb ->> 'rule_id' = %s"); params.append(str(rule_id))
        rows = self.db.query(f"SELECT signature FROM events WHERE {' AND '.join(clauses)}", tuple(params))
        n = 0
        for r in rows:
            if self.resolve(r["signature"], by, f"no further report for {int(ttl_seconds)}s"):
                n += 1
        return n

    # --- reads -----------------------------------------------------------------

    def open_for(self, signature):
        return self._to_dict(self.db.query_one("SELECT * FROM events WHERE signature = %s AND resolved_at IS NULL", (signature,)))

    def open_kind(self, kind, device_id, subject):
        return self.open_for(signature_for(kind, device_id, subject))

    def get(self, event_id):
        return self._to_dict(self.db.query_one("SELECT * FROM events WHERE id = %s", (int(event_id),)))

    def open_events(self, kind_prefix=None, device_id=None):
        clauses, params = ["resolved_at IS NULL"], []
        if kind_prefix:
            clauses.append("kind LIKE %s"); params.append(kind_prefix + "%")
        if device_id:
            clauses.append("device_id = %s"); params.append(device_id)
        rows = self.db.query(f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY raised_at DESC", tuple(params))
        return [self._to_dict(r) for r in rows]

    def list(self, open_only=False, severity=None, device_id=None, kind=None, group=None, q=None,
             since=None, before_id=None, limit=200):
        clauses, params = [], []
        if open_only:
            clauses.append("resolved_at IS NULL")
        if severity:
            sevs = [severity] if isinstance(severity, str) else list(severity)
            clauses.append("severity = ANY(%s)"); params.append(sevs)
        if device_id:
            clauses.append("device_id = %s"); params.append(device_id)
        if kind:
            clauses.append("kind = %s"); params.append(kind)
        if group:
            clauses.append("kind LIKE %s"); params.append(group + ".%")
        if q:
            clauses.append("(title ILIKE %s OR subject ILIKE %s OR device ILIKE %s OR detail ILIKE %s)")
            like = f"%{q}%"; params.extend([like, like, like, like])
        if since:
            clauses.append("raised_at >= %s"); params.append(since)
        if before_id:
            clauses.append("id < %s"); params.append(int(before_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 1000))
        params.append(limit)
        rows = self.db.query(f"SELECT * FROM events {where} ORDER BY id DESC LIMIT %s", tuple(params))
        return [self._to_dict(r) for r in rows]

    def summary(self):
        rows = self.db.query(
            """SELECT severity,
                      count(*) FILTER (WHERE resolved_at IS NULL) AS open_count,
                      count(*) FILTER (WHERE raised_at >= now() - interval '24 hours') AS day_count
               FROM events GROUP BY severity"""
        )
        out = {"open": {s: 0 for s in event_catalog.SEVERITIES}, "last_24h": {s: 0 for s in event_catalog.SEVERITIES}}
        for r in rows:
            if r["severity"] in out["open"]:
                out["open"][r["severity"]] = int(r["open_count"])
                out["last_24h"][r["severity"]] = int(r["day_count"])
        out["total_open"] = sum(out["open"].values())
        return out

    def prune(self, keep_days):
        """Retention: drop resolved events older than keep_days."""
        cur = self.db.execute(
            "DELETE FROM events WHERE resolved_at IS NOT NULL AND resolved_at < now() - (%s || ' days')::interval",
            (str(int(keep_days)),),
        )
        return getattr(cur, "rowcount", 0)
