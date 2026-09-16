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

Rules are evaluated by whichever path delivers the line - the fast path
(/api/ingest/syslog, sub-second) or the Loki poll behind it - inside
event_detect.SyslogDetector, and raise `syslog.rule` events like any
other kind. The event store is the state; the only job left here is the
per-rule auto-resolve timer.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webui.syslog_alerting")

SEVERITIES = ("info", "warning", "critical")
MAX_AUTO_RESOLVE = 86400

# Shipped disabled - a site turns on what it wants, and can see the
# pattern before it does. (The catalogue in event_catalog.py already
# covers the common kinds; rules are for a site's own lines.)
DEFAULT_RULES = [
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
        re-seeded into a table that has ever had rows."""
        # The self-test used to be a seeded rule; it is a catalogue kind now
        # (switchboard.selftest). A site that still carries the old row would
        # raise the test twice, so it goes - the only default ever removed.
        self.db.execute("DELETE FROM syslog_alert_rules WHERE key = 'selftest'")
        if self.db.query_one("SELECT 1 FROM syslog_alert_rules LIMIT 1"):
            return 0
        n = 0
        for r in DEFAULT_RULES:
            self.create(r, key=r["key"], builtin=r.get("builtin", False))
            n += 1
        log.info("seeded %d default syslog rules", n)
        return n


def _compile(rule):
    return (re.compile(rule["pattern"]) if rule.get("pattern") else None,
            re.compile(rule["clear_pattern"]) if rule.get("clear_pattern") else None)


def matches(rule, event, _cache={}):
    """Does this line fire (True), clear (False) or ignore (None) this
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


def expire_rules(store, rules):
    """Auto-resolve: every rule with a timer closes its open events that
    have not matched again within it. Called every few seconds."""
    n = 0
    for rule in rules:
        ttl = int(rule.get("auto_resolve_seconds") or 0)
        if ttl > 0:
            n += store.expire("syslog.rule", ttl, by="timer", rule_id=rule["id"])
    return n
