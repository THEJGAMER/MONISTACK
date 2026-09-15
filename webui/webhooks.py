"""Outbound webhooks: POST app events to URLs other systems own.

Each delivery is a JSON body with the event envelope from events.py, and
carries `X-Switchboard-Signature: sha256=<hmac>` computed over the exact
bytes sent, keyed with the webhook's secret - so a receiver can verify the
call came from here and not from anyone who learned the URL. It also
carries `X-Switchboard-Event` and `X-Switchboard-Delivery` (a unique id
per attempt) so a receiver can route and de-duplicate.

Delivery runs on the event bus's worker thread, not the request that
produced the event, and each webhook gets its own small retry (three tries
with backoff) before the failure is recorded on the row. Consecutive
failures are counted; nothing is auto-disabled, because an integration
that quietly switches itself off is worse than one that keeps trying and
shows a red status on the page.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
import uuid

import events

log = logging.getLogger("webui.webhooks")

TIMEOUT_SECONDS = 10
RETRY_DELAYS = (0, 2, 8)          # seconds before each attempt


def sign(secret, body_bytes):
    return "sha256=" + hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def verify_signature(secret, body_bytes, header_value):
    """For receivers written in Python (and for the tests): constant-time."""
    return hmac.compare_digest(sign(secret, body_bytes), header_value or "")


def _matches(events_list, event):
    return "*" in events_list or event in events_list


class WebhookStore:
    def __init__(self, db):
        self.db = db

    def create(self, name, url, events_list, created_by, secret=None):
        name = (name or "").strip()
        url = (url or "").strip()
        if not name:
            raise ValueError("a webhook needs a name")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        events_list = list(events_list or ["*"])
        unknown = [e for e in events_list if e != "*" and e not in events.EVENTS]
        if unknown:
            raise ValueError(f"unknown event(s): {', '.join(unknown)}")
        secret = secret or secrets.token_urlsafe(32)
        row = self.db.query_one(
            """INSERT INTO webhooks (name, url, secret, events, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (name, url, secret, json.dumps(events_list), created_by),
        )
        return self._public(row, include_secret=True)

    def update(self, webhook_id, **fields):
        allowed = {"name", "url", "events", "enabled"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "events":
                v = json.dumps(list(v))
            if k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k} = %s")
            params.append(v)
        if not sets:
            return self.get(webhook_id)
        params.append(int(webhook_id))
        row = self.db.query_one(f"UPDATE webhooks SET {', '.join(sets)} WHERE id = %s RETURNING *", tuple(params))
        return self._public(row) if row else None

    def delete(self, webhook_id):
        cur = self.db.execute("DELETE FROM webhooks WHERE id = %s", (int(webhook_id),))
        return getattr(cur, "rowcount", 0) > 0

    def get(self, webhook_id):
        row = self.db.query_one("SELECT * FROM webhooks WHERE id = %s", (int(webhook_id),))
        return self._public(row) if row else None

    def list(self):
        return [self._public(r) for r in self.db.query("SELECT * FROM webhooks ORDER BY id")]

    def _rows_for(self, event):
        rows = self.db.query("SELECT * FROM webhooks WHERE enabled = 1")
        return [dict(r) for r in rows if _matches(json.loads(r["events"]), event)]

    def _record(self, webhook_id, status, error):
        if error is None:
            self.db.execute(
                """UPDATE webhooks SET last_delivery_at = now(), last_status = %s, last_error = NULL,
                          consecutive_failures = 0 WHERE id = %s""",
                (status, webhook_id),
            )
        else:
            self.db.execute(
                """UPDATE webhooks SET last_delivery_at = now(), last_status = %s, last_error = %s,
                          consecutive_failures = consecutive_failures + 1 WHERE id = %s""",
                (status, str(error)[:500], webhook_id),
            )

    @staticmethod
    def _public(row, include_secret=False):
        d = dict(row)
        d["events"] = json.loads(d["events"]) if isinstance(d.get("events"), str) else d.get("events")
        d["enabled"] = bool(d.get("enabled", 1))
        if not include_secret:
            d["secret"] = None
        return d


def deliver(url, secret, envelope, timeout=TIMEOUT_SECONDS, delays=RETRY_DELAYS, sleep=time.sleep, opener=None):
    """One event to one URL, with retries. Returns (status_code, error)."""
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Switchboard-Webhook/1",
        "X-Switchboard-Event": envelope.get("event", ""),
        "X-Switchboard-Delivery": str(uuid.uuid4()),
        "X-Switchboard-Signature": sign(secret, body),
    }
    opener = opener or urllib.request.urlopen
    last_status, last_error = None, None
    for i, delay in enumerate(delays):
        if delay:
            sleep(delay)
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with opener(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
            if 200 <= status < 300:
                return status, None
            last_status, last_error = status, f"HTTP {status}"
        except urllib.error.HTTPError as e:
            last_status, last_error = e.code, f"HTTP {e.code}"
            if 400 <= e.code < 500 and e.code != 429:
                break                # our fault or theirs, not transient - do not hammer
        except Exception as e:      # timeouts, refused, DNS
            last_status, last_error = None, str(e)
    return last_status, last_error


class WebhookDispatcher:
    """Subscribes to the event bus and fans each event out to matching hooks."""

    def __init__(self, store, deliver_fn=deliver):
        self.store = store
        self.deliver = deliver_fn

    def __call__(self, event, envelope):
        try:
            hooks = self.store._rows_for(event)
        except Exception:
            log.exception("could not load webhooks for %s", event)
            return
        for h in hooks:
            status, error = self.deliver(h["url"], h["secret"], envelope)
            try:
                self.store._record(h["id"], status, error)
            except Exception:
                log.exception("could not record delivery for webhook %s", h["id"])
            if error:
                log.warning("webhook %s (%s) failed for %s: %s", h["id"], h["name"], event, error)

    def test(self, webhook_id):
        """A synthetic delivery so a new hook can be proven before anything real fires."""
        h = self.store.db.query_one("SELECT * FROM webhooks WHERE id = %s", (int(webhook_id),))
        if not h:
            return None
        envelope = {"event": "test", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "message": f"Test delivery for webhook '{h['name']}' from Switchboard"}
        status, error = self.deliver(h["url"], h["secret"], envelope)
        self.store._record(h["id"], status, error)
        return {"status": status, "error": error}
