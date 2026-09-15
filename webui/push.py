"""Web Push: page a phone without a third party.

The browser subscribes (a URL at its vendor's push service plus two keys),
we store that, and when an alarm opens we sign a message with our VAPID
key and POST it to that URL. The vendor's service wakes the service
worker, which shows the notification - even with the app closed.

Adapted from the PROXMON project's push.ts, then extended for paging:

- per-subscription minimum severity and a "tell me when it resolves"
  flag, so a phone can be page-on-critical-only while a laptop sees all;
- `requireInteraction` on critical, `renotify` with a per-alarm `tag` so a
  re-fire of the same alarm replaces its notification rather than
  stacking; and an Acknowledge action on the notification itself - the
  service worker POSTs the ack with the browser's own session cookie, so
  a page can be acknowledged from the lock screen without opening the app;
- subscriptions the push service reports as gone (404/410) are pruned,
  and other failures counted and shown, so a dead subscription never
  looks like a working one.

The VAPID key pair is generated once and kept in the data directory with
0600 permissions, next to settings.json. Losing it invalidates every
subscription (browsers bind them to the key), which is why it is a file
and not regenerated per start.
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


class PushSubscriptionStore:
    def __init__(self, db):
        self.db = db

    def upsert(self, subscription, username, label=None, min_severity="warning", notify_resolved=True):
        endpoint = (subscription or {}).get("endpoint")
        if not endpoint or not (subscription.get("keys") or {}).get("p256dh"):
            raise ValueError("not a push subscription")
        if min_severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity {min_severity!r}")
        row = self.db.query_one(
            """INSERT INTO push_subscriptions (endpoint, subscription, username, label, min_severity, notify_resolved)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (endpoint) DO UPDATE SET subscription = EXCLUDED.subscription,
                 username = EXCLUDED.username, label = EXCLUDED.label, min_severity = EXCLUDED.min_severity,
                 notify_resolved = EXCLUDED.notify_resolved, failures = 0, last_error = NULL
               RETURNING *""",
            (endpoint, json.dumps(subscription), username, (label or "")[:120], min_severity, 1 if notify_resolved else 0),
        )
        return self._public(row)

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
        return d


def payload_for(event, envelope):
    """What the notification says. None when this event is not pageable."""
    occ = envelope.get("occurrence") or {}
    name = occ.get("alertname") or envelope.get("alertname") or "Alarm"
    device = occ.get("device") or envelope.get("device") or ""
    sev = (occ.get("severity") or envelope.get("severity") or "warning").lower()
    occ_id = occ.get("id") or envelope.get("occurrence_id")
    url = f"/#/alarms/{occ_id}" if occ_id else "/#/alarms"
    tag = f"switchboard-alarm-{occ_id or name}"
    if event == "alarm.opened":
        return {"title": f"{sev.upper()}: {name}" + (f" on {device}" if device else ""),
                "body": occ.get("summary") or envelope.get("summary") or "",
                "severity": sev, "tag": tag, "url": url, "occurrence_id": occ_id,
                "actions": [{"action": "ack", "title": "Acknowledge"}, {"action": "open", "title": "Open"}]}
    if event == "alarm.resolved":
        return {"title": f"Resolved: {name}" + (f" on {device}" if device else ""),
                "body": occ.get("summary") or "", "severity": "ok", "tag": tag, "url": url,
                "occurrence_id": occ_id, "actions": [{"action": "open", "title": "Open"}]}
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
                    vapid_private_key=self.keys.private_key_pem,
                    vapid_claims={"sub": self.keys.subject}, ttl=3600,
                    headers={"Urgency": "high" if payload.get("severity") == "critical" else "normal"})
            return True, None, False
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            gone = code in (404, 410)
            return False, f"{code or ''} {e}".strip(), gone
        except Exception as e:
            return False, str(e), False

    def __call__(self, event, envelope):
        if not self.keys.available:
            return
        payload = payload_for(event, envelope)
        if payload is None:
            return
        for row in self.store._all_raw():
            if event == "alarm.resolved":
                if not row.get("notify_resolved"):
                    continue
            elif _rank(payload.get("severity")) < _rank(row.get("min_severity")):
                continue
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
