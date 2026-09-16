"""Syslog rules: alarms raised from what a device logs, evaluated on arrival.

PROXMON's agent tails /dev/kmsg against a fault regex and pages within a
second of a matching line. A switch's syslog is the same signal: STP
topology changes, a BGP/OSPF neighbour dropping, a duplicate IP, a config
commit, or anything else a site cares about - none of which are a metric
Prometheus could scrape in time, all of which arrive as one log line the
instant they happen. A rule here is: match (facility and/or mnemonic
exactly, and/or a regex on the message), severity, how the alarm ends
(a clearing pattern, an auto-resolve timer, or both) and whether it is
one alarm per device or per device+interface.

Rules are evaluated by whichever path delivers the event - the fast path
(/api/ingest/syslog, sub-second) or the Loki poll behind it - and fire
through the same Alertmanager client as every other checker, so they
page phones via the local-first wrapper *and* reach Alertmanager's own
receivers. In-memory state (what is firing, when it expires) is rebuilt
from Alertmanager on restart, as the other checkers do.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webui.syslog_alerting")

SEVERITIES = ("info", "warning", "critical")
SOURCE_LABEL = "syslog-rule"
HEARTBEAT_SECONDS = 120     # same Alertmanager resolve_timeout reasoning as the other checkers
MAX_AUTO_RESOLVE = 86400

# Shipped disabled except the self-test - a site turns on what it wants,
# and can see the pattern before it does. The self-test rule is what the
# "Send a test" button on the Alerts page fires through.
DEFAULT_RULES = [
    {"key": "selftest", "name": "Switchboard fast-path self-test", "enabled": True, "severity": "warning",
     "mnemonic": "SWITCHBOARD_SELFTEST", "auto_resolve_seconds": 60, "builtin": True},
    {"key": "stp-topology-change", "name": "Spanning-tree topology change", "enabled": False, "severity": "warning",
     "facility": "STP", "pattern": r"(?i)topology\s*change", "auto_resolve_seconds": 300},
    {"key": "routing-neighbour-lost", "name": "Routing neighbour lost", "enabled": False, "severity": "critical",
     "pattern": r"(?i)\b(bgp|ospf|neighbou?r|adjacency)\b.*\b(down|lost|deleted|expired|dead)\b",
     "clear_pattern": r"(?i)\b(bgp|ospf|neighbou?r|adjacency)\b.*\b(up|established|full)\b"},
    {"key": "duplicate-ip", "name": "Duplicate IP address", "enabled": False, "severity": "warning",
     "pattern": r"(?i)duplicate\s+(ip|address)", "auto_resolve_seconds": 900},
    {"key": "config-committed", "name": "Configuration changed", "enabled": False, "severity": "info",
     "pattern": r"(?i)(UI_COMMIT_COMPLETED|CONFIG_I|configured from|copy running-config)", "auto_resolve_seconds": 60},
]


def _clean(rule):
    """Validate and normalise a rule dict. Raises ValueError with a message
    fit for the form."""
    name = (rule.get("name") or "").strip()
    if not name:
        raise ValueError("a rule needs a name")
    if len(name) > 120:
        raise ValueError("name is too long (120 characters)")
    severity = (rule.get("severity") or "warning").lower()
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    facility = (rule.get("facility") or "").strip().upper()
    mnemonic = (rule.get("mnemonic") or "").strip().upper()
    pattern = (rule.get("pattern") or "").strip()
    clear_pattern = (rule.get("clear_pattern") or "").strip()
    if not (facility or mnemonic or pattern):
        raise ValueError("match on a facility, a mnemonic or a pattern - at least one")
    for label, rx in (("pattern", pattern), ("clear pattern", clear_pattern)):
        if rx:
            try:
                re.compile(rx)
            except re.error as e:
                raise ValueError(f"{label} is not a valid regular expression: {e}")
    try:
        auto = int(rule.get("auto_resolve_seconds") or 0)
    except (TypeError, ValueError):
        raise ValueError("auto-resolve must be a number of seconds")
    if auto < 0 or auto > MAX_AUTO_RESOLVE:
        raise ValueError("auto-resolve must be between 0 (never) and 86400 seconds")
    return {
        "name": name, "enabled": bool(rule.get("enabled", True)), "severity": severity,
        "facility": facility, "mnemonic": mnemonic, "pattern": pattern, "clear_pattern": clear_pattern,
        "per_interface": bool(rule.get("per_interface", False)), "auto_resolve_seconds": auto,
    }


class SyslogRuleStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _public(row):
        if not row:
            return None
        d = dict(row)
        for k in ("enabled", "per_interface", "builtin"):
            d[k] = bool(d.get(k))
        for k in ("created_at", "updated_at"):
            if d.get(k) is not None and not isinstance(d[k], str):
                d[k] = d[k].isoformat()
        return d

    def list(self, enabled_only=False):
        where = "WHERE enabled = 1" if enabled_only else ""
        return [self._public(r) for r in self.db.query(f"SELECT * FROM syslog_alert_rules {where} ORDER BY id")]

    def get(self, rule_id):
        return self._public(self.db.query_one("SELECT * FROM syslog_alert_rules WHERE id = %s", (int(rule_id),)))

    def get_by_key(self, key):
        return self._public(self.db.query_one("SELECT * FROM syslog_alert_rules WHERE key = %s", (key,)))

    def create(self, rule, key=None, builtin=False):
        c = _clean(rule)
        row = self.db.query_one(
            """INSERT INTO syslog_alert_rules (key, name, enabled, severity, facility, mnemonic, pattern, clear_pattern,
                                               per_interface, auto_resolve_seconds, builtin)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (key, c["name"], 1 if c["enabled"] else 0, c["severity"], c["facility"], c["mnemonic"], c["pattern"],
             c["clear_pattern"], 1 if c["per_interface"] else 0, c["auto_resolve_seconds"], 1 if builtin else 0),
        )
        return self._public(row)

    def update(self, rule_id, rule):
        current = self.get(rule_id)
        if current is None:
            return None
        merged = {**current, **{k: v for k, v in rule.items() if v is not None}}
        c = _clean(merged)
        row = self.db.query_one(
            """UPDATE syslog_alert_rules SET name = %s, enabled = %s, severity = %s, facility = %s, mnemonic = %s,
                      pattern = %s, clear_pattern = %s, per_interface = %s, auto_resolve_seconds = %s, updated_at = now()
               WHERE id = %s RETURNING *""",
            (c["name"], 1 if c["enabled"] else 0, c["severity"], c["facility"], c["mnemonic"], c["pattern"],
             c["clear_pattern"], 1 if c["per_interface"] else 0, c["auto_resolve_seconds"], int(rule_id)),
        )
        return self._public(row)

    def delete(self, rule_id):
        cur = self.db.execute("DELETE FROM syslog_alert_rules WHERE id = %s AND builtin = 0", (int(rule_id),))
        return getattr(cur, "rowcount", 0) > 0

    def seed_defaults(self):
        """First run only (an empty table): the site starts with something
        to look at. Deleting a default later is respected - nothing is
        re-seeded into a table that has ever had rows. The self-test rule
        is the exception: `ensure_selftest` puts it back on demand."""
        if self.db.query_one("SELECT 1 FROM syslog_alert_rules LIMIT 1"):
            return 0
        n = 0
        for r in DEFAULT_RULES:
            self.create(r, key=r["key"], builtin=r.get("builtin", False))
            n += 1
        log.info("seeded %d default syslog rules", n)
        return n

    def ensure_selftest(self, severity="warning"):
        row = self.get_by_key("selftest")
        spec = next(r for r in DEFAULT_RULES if r["key"] == "selftest")
        if row is None:
            return self.create({**spec, "severity": severity}, key="selftest", builtin=True)
        if row["severity"] != severity or not row["enabled"]:
            return self.update(row["id"], {"severity": severity, "enabled": True})
        return row


def _compile(rule):
    return (re.compile(rule["pattern"]) if rule.get("pattern") else None,
            re.compile(rule["clear_pattern"]) if rule.get("clear_pattern") else None)


def matches(rule, event, _cache={}):
    """Does this event fire (True), clear (False) or ignore (None) this
    rule? Facility/mnemonic are exact; the patterns run over the raw
    message so they can see everything the interpreter saw."""
    fac = (event.get("facility") or "").upper()
    mn = (event.get("mnemonic") or "").upper()
    if rule.get("facility") and fac != rule["facility"]:
        return None
    if rule.get("mnemonic") and mn != rule["mnemonic"]:
        return None
    key = (rule.get("id"), rule.get("pattern"), rule.get("clear_pattern"))
    compiled = _cache.get(key)
    if compiled is None:
        try:
            compiled = _compile(rule)
        except re.error:
            return None
        _cache[key] = compiled
        if len(_cache) > 500:
            _cache.clear()
    fire_rx, clear_rx = compiled
    text = str(event.get("message") or event.get("detail") or "")
    if clear_rx is not None and clear_rx.search(text):
        return False
    if fire_rx is not None:
        return True if fire_rx.search(text) else None
    return True


class SyslogRuleEngine:
    """In-memory firing state, keyed by (rule id, device key, interface)."""

    def __init__(self):
        self._active = {}   # identity -> {"labels", "annotations", "posted_at" (monotonic), "expires_at" (monotonic|None)}
        self.cursor_ns = 0  # newest event timestamp evaluated, see evaluate_new

    def evaluate_new(self, events, rules, device_for, alertmanager):
        """evaluate() for events that may have been seen before: the fast
        path and the Loki poll behind it deliver the same events (same
        Vector timestamps, to the nanosecond), and whichever is first
        wins. Events must carry `_timestamp_ns`; pass them oldest first."""
        fresh = []
        newest = self.cursor_ns
        for e in events:
            ts = int(e.get("_timestamp_ns") or 0)
            if ts and ts <= self.cursor_ns:
                continue
            newest = max(newest, ts)
            fresh.append(e)
        changed = self.evaluate(fresh, rules, device_for, alertmanager)
        self.cursor_ns = newest
        return changed

    def evaluate(self, events, rules, device_for, alertmanager):
        """`device_for(event)` -> (device_id or "", display name). Returns
        the number of alarms fired or cleared."""
        changed = 0
        for event in events:
            for rule in rules:
                if not rule.get("enabled"):
                    continue
                verdict = matches(rule, event)
                if verdict is None:
                    continue
                device_id, device_name = device_for(event)
                interface = (event.get("interface") or "") if rule.get("per_interface") else ""
                identity = (rule["id"], device_id or device_name, interface)
                if verdict:
                    changed += self._fire(identity, rule, event, device_id, device_name, interface, alertmanager)
                else:
                    changed += self._clear(identity, alertmanager, reason="clearing message")
        return changed

    def _labels(self, rule, device_id, device_name, interface):
        labels = {"alertname": rule["name"], "source": SOURCE_LABEL, "rule_id": str(rule["id"]),
                  "device": device_name, "severity": rule["severity"]}
        if device_id:
            labels["device_id"] = device_id
        if interface:
            labels["interface"] = interface
        return labels

    def _fire(self, identity, rule, event, device_id, device_name, interface, alertmanager):
        now = time.monotonic()
        auto = int(rule.get("auto_resolve_seconds") or 0)
        detail = str(event.get("detail") or event.get("message") or "")[:200]
        state = self._active.get(identity)
        labels = self._labels(rule, device_id, device_name, interface)
        where = f"{device_name}" + (f" {interface}" if interface else "")
        annotations = {"summary": f"{rule['name']} on {where}: {detail}" if detail else f"{rule['name']} on {where}",
                       "description": str(event.get("message") or "")[:1000]}
        is_new = state is None
        self._active[identity] = {"labels": labels, "annotations": annotations, "posted_at": now,
                                  "expires_at": (now + auto) if auto > 0 else None}
        self._post(labels, annotations, alertmanager)
        if is_new:
            log.info("syslog rule fired: %r on %s", rule["name"], where)
        return 1 if is_new else 0

    def _post(self, labels, annotations, alertmanager, ends_at=None):
        alert = {"labels": labels, "annotations": annotations,
                 "startsAt": datetime.now(timezone.utc).isoformat()}
        if ends_at is not None:
            alert["startsAt"] = (ends_at - timedelta(minutes=1)).isoformat()
            alert["endsAt"] = ends_at.isoformat()
        try:
            alertmanager.post_alerts([alert])
        except Exception:
            log.exception("could not post syslog-rule alert %s", labels.get("alertname"))

    def _clear(self, identity, alertmanager, reason):
        state = self._active.pop(identity, None)
        if state is None:
            return 0
        self._post(state["labels"], {"summary": f"{state['labels']['alertname']} cleared ({reason})"},
                   alertmanager, ends_at=datetime.now(timezone.utc))
        log.info("syslog rule cleared (%s): %r on %s", reason, state["labels"]["alertname"], state["labels"].get("device"))
        return 1

    def tick(self, alertmanager):
        """Every few seconds: auto-resolve what has expired, heartbeat what
        is still firing so Alertmanager does not time it out."""
        now = time.monotonic()
        n = 0
        for identity, state in list(self._active.items()):
            if state["expires_at"] is not None and now >= state["expires_at"]:
                n += self._clear(identity, alertmanager, reason="auto-resolve")
            elif now - state["posted_at"] >= HEARTBEAT_SECONDS:
                state["posted_at"] = now
                self._post(state["labels"], state["annotations"], alertmanager)
        return n

    def active(self):
        return [dict(labels=s["labels"], summary=s["annotations"].get("summary")) for s in self._active.values()]

    def forget_rule(self, rule_id, alertmanager):
        """A rule being deleted or disabled takes its alarms with it."""
        n = 0
        for identity in [i for i in self._active if str(i[0]) == str(rule_id)]:
            n += self._clear(identity, alertmanager, reason="rule removed")
        return n

    def reseed_from_alertmanager(self, alertmanager, rules_by_id):
        """After a restart, adopt what Alertmanager still has firing from
        us so it keeps being heartbeated and can still auto-resolve."""
        try:
            alerts = alertmanager.list_alerts()
        except Exception:
            log.warning("could not reseed syslog-rule state from Alertmanager", exc_info=True)
            return 0
        now = time.monotonic()
        seeded = 0
        for alert in alerts or []:
            labels = alert.get("labels") or {}
            if labels.get("source") != SOURCE_LABEL or alert.get("status", {}).get("state") != "active":
                continue
            rule = rules_by_id.get(labels.get("rule_id"))
            if rule is None:
                continue
            identity = (rule["id"], labels.get("device_id") or labels.get("device"), labels.get("interface") or "")
            auto = int(rule.get("auto_resolve_seconds") or 0)
            self._active[identity] = {"labels": labels, "annotations": alert.get("annotations") or {},
                                      "posted_at": now, "expires_at": (now + auto) if auto > 0 else None}
            seeded += 1
        if seeded:
            log.info("syslog rule state reseeded from Alertmanager: %d alarm(s) still active", seeded)
        return seeded
