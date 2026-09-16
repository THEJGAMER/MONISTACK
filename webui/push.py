"""Web Push for events: a phone buzzes when something is raised, and hears
when it is over.

A notifier, not a pager: one push per raised event at or above the
device's severity floor, one on resolve if the device asked for it (same
notification tag, so the resolve replaces the raise rather than piling
up). No acknowledgement, no repeat-until-acked - actioning belongs to the
ticketing system that consumes the same events over webhooks.

Adapted from the PROXMON project's push implementation.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("webui.push")

SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}


def _rank(sev):
    return SEVERITY_RANK.get((sev or "warning").lower(), 2)


def default_vapid_subject(candidates, override=None, placeholder="mailto:switchboard@example.com"):
    """The VAPID `sub` claim, chosen the way PROXMON's defaultSubject does.

    Push services validate it and Apple rejects a bad one with BadJwtToken,
    so the fallback chain matters: prefer the https origin users open; then
    a mailto: on a real hostname (a plain-http URL still names the site);
    and only then a placeholder that is at least a syntactically valid
    address on a real domain. An explicit override always wins. Bare IPs
    and localhost are never used - they are exactly what Apple rejects.
    """
    if override and override.strip():
        return override.strip()
    from urllib.parse import urlsplit
    parsed = []
    for u in candidates or []:
        if not u:
            continue
        try:
            parts = urlsplit(u.strip())
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        if host:
            parsed.append((parts.scheme, host, parts.port))
    # Two passes, not first-match: an https origin anywhere in the list
    # beats a mailto: from an http URL listed before it. (PROXMON's
    # original is first-match, which makes the answer depend on the order
    # the URLs happen to be configured in.)
    for scheme, host, port in parsed:
        if scheme == "https":
            return f"https://{host}" + (f":{port}" if port and port != 443 else "")
    for scheme, host, port in parsed:
        is_ip = host.count(".") == 3 and all(seg.isdigit() for seg in host.split("."))
        if "." in host and not is_ip and host != "localhost":
            return f"mailto:switchboard@{host}"
    return placeholder


class VapidKeys:
    """Generate-once, load-forever key pair on disk."""

    def __init__(self, path, subject):
        self.path = Path(path)
        self.subject = subject
        self.public_key = None
        self.private_key_pem = None
        self._load_or_create()

    def _load_or_create(self):
        if self.path.exists():
            d = json.loads(self.path.read_text())
            self.public_key, self.private_key_pem = d["public_key"], d["private_key_pem"]
            return
        try:
            from py_vapid import Vapid
            from py_vapid.utils import b64urlencode
            from cryptography.hazmat.primitives import serialization
        except ImportError as e:            # pywebpush not installed: push stays off
            log.warning("web push unavailable: %s", e)
            return
        v = Vapid()
        v.generate_keys()
        pub = v.public_key.public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
        priv = v.private_pem().decode() if isinstance(v.private_pem(), bytes) else v.private_pem()
        self.public_key = b64urlencode(pub)
        self.private_key_pem = priv
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"public_key": self.public_key, "private_key_pem": self.private_key_pem}))
        os.chmod(self.path, 0o600)
        log.info("generated VAPID key pair for web push (%s)", self.path)

    @property
    def available(self):
        return bool(self.public_key and self.private_key_pem)

    def vapid(self):
        """The parsed key, as the Vapid instance pywebpush wants.

        pywebpush's vapid_private_key is a Vapid, a PEM *file path*, or a
        raw base64url key - never PEM text. Handing it the text made it
        parse the PEM as raw DER and fail with "Could not deserialize key
        data … ASN.1 parsing error", on the first real test page. Parsed
        once and cached: the key does not change while the process runs.
        """
        if getattr(self, "_vapid", None) is None:
            from py_vapid import Vapid
            pem = self.private_key_pem
            self._vapid = Vapid.from_pem(pem.encode() if isinstance(pem, str) else pem)
        return self._vapid


class PushSubscriptionStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _check_prefs(min_severity):
        if min_severity not in ("info", "warning", "critical"):
            raise ValueError("min_severity must be one of info, warning, critical")

    def upsert(self, subscription, username, label=None, min_severity="warning", notify_resolved=True):
        if not isinstance(subscription, dict) or not subscription.get("endpoint") or not subscription.get("keys"):
            raise ValueError("not a push subscription")
        self._check_prefs(min_severity)
        self.db.execute(
            """INSERT INTO push_subscriptions (endpoint, subscription, username, label, min_severity, notify_resolved)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (endpoint) DO UPDATE SET subscription = EXCLUDED.subscription, username = EXCLUDED.username,
                 label = EXCLUDED.label, min_severity = EXCLUDED.min_severity, notify_resolved = EXCLUDED.notify_resolved,
                 failures = 0, last_error = NULL""",
            (subscription["endpoint"], json.dumps(subscription), username, label, min_severity, 1 if notify_resolved else 0),
        )
        return self._public(self.db.query_one("SELECT * FROM push_subscriptions WHERE endpoint = %s", (subscription["endpoint"],)))

    def update_prefs(self, endpoint, min_severity, notify_resolved):
        """Change how a device is notified without re-subscribing it."""
        self._check_prefs(min_severity)
        row = self.db.query_one(
            "UPDATE push_subscriptions SET min_severity = %s, notify_resolved = %s WHERE endpoint = %s RETURNING *",
            (min_severity, 1 if notify_resolved else 0, endpoint),
        )
        return self._public(row) if row else None

    def remove(self, endpoint):
        cur = self.db.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))
        return getattr(cur, "rowcount", 0) > 0

    def list(self, username=None):
        if username:
            rows = self.db.query("SELECT * FROM push_subscriptions WHERE username = %s ORDER BY created_at", (username,))
        else:
            rows = self.db.query("SELECT * FROM push_subscriptions ORDER BY created_at")
        return [self._public(r) for r in rows]

    def _all_raw(self):
        return [dict(r) for r in self.db.query("SELECT * FROM push_subscriptions")]

    def _ok(self, endpoint):
        self.db.execute("UPDATE push_subscriptions SET last_used_at = now(), failures = 0, last_error = NULL WHERE endpoint = %s", (endpoint,))

    def _failed(self, endpoint, error):
        self.db.execute("UPDATE push_subscriptions SET failures = failures + 1, last_error = %s WHERE endpoint = %s",
                        (str(error)[:300], endpoint))

    @staticmethod
    def _public(row):
        d = dict(row)
        d.pop("subscription", None)      # the keys never leave the server
        d["notify_resolved"] = bool(d.get("notify_resolved", 1))
        d.setdefault("repeat_minutes", 5)
        d.setdefault("max_repeats", 12)
        return d


def payload_for(event, envelope):
    """What the notification says. None when this bus event is not one."""
    ev = envelope.get("event_data") or {}
    if not ev:
        return None
    sev = (ev.get("severity") or "warning").lower()
    tag = f"switchboard-event-{ev.get('id')}"
    url = f"/#/events/{ev.get('id')}" if ev.get("id") else "/#/events"
    if event == "event.raised":
        reopens = int(ev.get("reopen_count") or 0)
        again = f"back after {reopens} return{'s' if reopens > 1 else ''}: " if reopens else ""
        return {"title": f"{sev.upper()}: {ev.get('title') or ev.get('kind_name') or 'Event'}",
                "body": again + (ev.get("detail") or "")[:200], "severity": sev, "tag": tag, "url": url,
                "event_id": ev.get("id"), "actions": [{"action": "open", "title": "Open"}]}
    if event == "event.resolved":
        by = ev.get("resolved_by") or "switchboard"
        return {"title": f"Resolved: {ev.get('title') or ev.get('kind_name') or 'Event'}",
                "body": f"Resolved by {by}" + (f": {ev.get('resolve_detail')}" if ev.get("resolve_detail") else ""),
                "severity": "ok", "quiet": True, "tag": tag, "url": url, "event_id": ev.get("id"),
                "actions": [{"action": "open", "title": "Open"}]}
    return None


class PushNotifier:
    """Event-bus subscriber: turns alarm events into Web Push sends."""

    def __init__(self, store, keys, send_fn=None):
        self.store, self.keys = store, keys
        self._send = send_fn or self._webpush
        self.sent = 0

    def _webpush(self, subscription, payload):
        from pywebpush import webpush, WebPushException
        try:
            webpush(subscription_info=subscription, data=json.dumps(payload),
                    vapid_private_key=self.keys.vapid(),
                    vapid_claims={"sub": self.keys.subject}, ttl=3600,
                    headers={"Urgency": "high" if payload.get("severity") == "critical" else "normal"})
            return True, None, False
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            gone = code in (404, 410)
            return False, f"{code or ''} {e}".strip(), gone
        except Exception as e:
            return False, str(e), False

    def _deliver(self, row, payload):
        ok, error, gone = self._send(json.loads(row["subscription"]), payload)
        if ok:
            self.store._ok(row["endpoint"])
            self.sent += 1
        elif gone:
            self.store.remove(row["endpoint"])
            log.info("pruned push subscription that the service reports gone: %s", row["endpoint"][:60])
        else:
            self.store._failed(row["endpoint"], error)
            log.warning("push send failed for %s: %s", row["endpoint"][:60], error)
        return ok

    def __call__(self, event, envelope):
        if not self.keys.available:
            return
        payload = payload_for(event, envelope)
        if payload is None:
            return
        ev = envelope.get("event_data") or {}
        for row in self.store._all_raw():
            if event == "event.resolved":
                if not row.get("notify_resolved") or _rank(ev.get("severity")) < _rank(row.get("min_severity")):
                    continue   # a device hears the end of what it would have been told about
            elif _rank(payload.get("severity")) < _rank(row.get("min_severity")):
                continue
            self._deliver(row, payload)

    def test(self, endpoint):
        row = self.store.db.query_one("SELECT * FROM push_subscriptions WHERE endpoint = %s", (endpoint,))
        if not row:
            return None
        payload = {"title": "Switchboard test page", "body": "If you can read this, paging works on this device.",
                   "severity": "info", "tag": "switchboard-test", "url": "/#/account",
                   "actions": [{"action": "open", "title": "Open"}]}
        ok, error, gone = self._send(json.loads(row["subscription"]), payload)
        if ok:
            self.store._ok(endpoint)
        elif gone:
            self.store.remove(endpoint)
        else:
            self.store._failed(endpoint, error)
        return {"ok": ok, "error": error, "gone": gone}
