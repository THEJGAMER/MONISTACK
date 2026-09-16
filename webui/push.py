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


# --- saying it in words -------------------------------------------------
# What a phone showed before this: a title shouting "CRITICAL:" ahead of
# a colon-heavy line, and a body containing the raw log, e.g.
#   mib2d[1344]: SNMP_TRAP_LINK_DOWN: ifIndex 603, ifAdminStatus up(1),
#   ifOperStatus down(2), ifName xe-0/1/3
# You cannot read that on a lock screen, and by the time you have, you
# have opened the app anyway - where the raw line is, and belongs.

_SEVERITY_WORD = {"critical": "Critical", "warning": "Warning", "info": "For information", "ok": "Resolved"}

# Whether an event's `detail` is something a device said or something
# Switchboard wrote. The syslog paths carry the raw line; everything else
# carries a sentence the detectors composed ("95% for 3 polls, threshold
# 90%"), which is worth putting in front of someone.
_DEVICE_SAID = {"syslog", "loki"}


# The title already names the device, so these do not repeat it.
def _how_we_know(source):
    if source in _DEVICE_SAID:
        return "The device reported it."
    if source == "ssh":
        return "Found by the SSH poll."
    if source == "switchboard":
        return "Noticed by Switchboard."
    return f"Raised by {source}." if source else ""


def _how_it_cleared(by):
    if by in _DEVICE_SAID:
        return "The device reported it back to normal."
    if by == "ssh":
        return "The SSH poll saw it recover."
    if by == "timer":
        return "It stopped being reported."
    if by in ("switchboard", None, ""):
        return "Cleared by Switchboard."
    return f"Resolved by {by}."


def _as_sentence(text):
    text = (text or "").strip()
    if not text:
        return ""
    return text if text[-1] in ".!?" else text + "."


def _lasted(raised_at, resolved_at):
    """'Lasted 3 minutes.' - the one thing a resolve should always say."""
    start_, end_ = _as_dt(raised_at), _as_dt(resolved_at)
    if start_ is None or end_ is None:
        return ""
    seconds = max(0, (end_ - start_).total_seconds())
    if seconds < 60:
        n, unit = round(seconds), "second"
    elif seconds < 5400:
        n, unit = round(seconds / 60), "minute"
    elif seconds < 172800:
        n, unit = round(seconds / 3600), "hour"
    else:
        n, unit = round(seconds / 86400), "day"
    return f"Lasted {n} {unit}{'s' if n != 1 else ''}."


def _as_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _return_note(reopen_count):
    n = int(reopen_count or 0)
    if not n:
        return ""
    return f"Back for the {_ordinal(n + 1)} time."


def _sentences(*parts):
    return " ".join(p.strip() for p in parts if p and p.strip())


def payload_for(event, envelope):
    """What the notification says. None when this bus event is not one."""
    ev = envelope.get("event_data") or {}
    if not ev:
        return None
    sev = (ev.get("severity") or "warning").lower()
    tag = f"switchboard-event-{ev.get('id')}"
    url = f"/#/events/{ev.get('id')}" if ev.get("id") else "/#/events"
    title = ev.get("title") or ev.get("kind_name") or "Event"

    if event == "event.raised":
        # The raw log line stays in the app. Out here it is the severity,
        # where the news came from, and - only when Switchboard wrote it -
        # the reading behind it.
        detail = (ev.get("detail") or "").strip() if ev.get("source") not in _DEVICE_SAID else ""
        return {"title": title,
                "body": _sentences(_SEVERITY_WORD.get(sev, sev.title()) + ".",
                                   _how_we_know(ev.get("source")),
                                   _as_sentence(detail) if len(detail) <= 120 else "",
                                   _return_note(ev.get("reopen_count"))),
                "severity": sev, "tag": tag, "url": url,
                "event_id": ev.get("id"), "actions": [{"action": "open", "title": "Open"}]}

    if event == "event.resolved":
        return {"title": f"Cleared: {title}",
                "body": _sentences(_lasted(ev.get("raised_at"), ev.get("resolved_at")),
                                   _how_it_cleared(ev.get("resolved_by"))),
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
