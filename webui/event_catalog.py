"""The event catalogue: every kind of thing Switchboard can notice, and
what severity a site wants it to be.

Event-driven monitoring means the device tells us something happened (a
syslog line, first) or we notice it ourselves (the SSH poll, as the
fallback), and it becomes an *event* - info, warning or critical - that
stays open until the device says it is over, the fallback sees it is
over, a timer says it is stale, or a person resolves it. Nothing here
acknowledges, pages, holds or silences: that is a ticketing system's
job, and the events go out to it over webhooks and the API.

Every kind has a default severity and a per-site override (`ignore`
drops it entirely); link events additionally take a per-port override,
because a port's importance is the one thing the catalogue cannot know.
"""
import json
import logging
import time

log = logging.getLogger("webui.events.catalog")

SEVERITIES = ("info", "warning", "critical")
CHOICES = SEVERITIES + ("ignore",)
GROUPS = [
    ("port", "Ports"),
    ("optic", "Optics"),
    ("env", "Environment"),
    ("compute", "Compute"),
    ("device", "Device"),
    ("protocol", "Protocol"),
    ("syslog", "Syslog rules"),
    ("switchboard", "Switchboard"),
]

# `resolves` is prose for the UI; `ttl_seconds` is the timer that resolves
# a kind nothing else can close (None = only its own clearing signal or a
# person). `params` are the thresholds a kind uses, overridable per site.
CATALOG = [
    {"kind": "port.link_down", "group": "port", "name": "Link down", "default": "warning",
     "description": "A physical interface lost link. Severity per port on the Ports tab; a port set to ignore never raises.",
     "sources": ["syslog", "ssh"], "resolves": "when the link comes back (syslog up, or the SSH poll sees it up)", "ttl_seconds": None},
    {"kind": "port.lag_member_lost", "group": "port", "name": "LAG member left", "default": "warning",
     "description": "An interface dropped out of its port-channel. On a LAG member this is often the only thing the switch logs - "
                    "confirmed live on the S4048: unplugging Te 1/41 produced LACP PORT-UNGROUPED and no link-state line at all.",
     "sources": ["syslog"], "resolves": "when it rejoins the port-channel", "ttl_seconds": None},
    {"kind": "port.flapping", "group": "port", "name": "Link flapping", "default": "warning",
     "description": "An interface went down at least 3 times within 5 minutes.",
     "sources": ["syslog"], "resolves": "after 10 minutes without another flap", "ttl_seconds": 600},
    {"kind": "port.input_errors", "group": "port", "name": "Input errors rising", "default": "warning",
     "description": "CRC, runt, giant and overrun errors are climbing on a live port - the classic bad cable, dirty "
                    "connector or failing optic. Counters only; a port that logged errors once and stopped is not a fault.",
     "sources": ["ssh"], "resolves": "when a poll shows no further increase", "ttl_seconds": None,
     "params": {"per_poll": 10}},
    {"kind": "port.output_errors", "group": "port", "name": "Output errors rising", "default": "warning",
     "description": "Collisions and output errors climbing on a live port - usually a duplex mismatch or a failing link partner.",
     "sources": ["ssh"], "resolves": "when a poll shows no further increase", "ttl_seconds": None,
     "params": {"per_poll": 10}},
    {"kind": "port.discards", "group": "port", "name": "Discards rising", "default": "info",
     "description": "Frames dropped on a live port. Usually congestion rather than a fault, which is why this starts at info.",
     "sources": ["ssh"], "resolves": "when a poll shows no further increase", "ttl_seconds": None,
     "params": {"per_poll": 1000}},
    {"kind": "optic.rx_power_low", "group": "optic", "name": "Low receive power (light loss)", "default": "critical",
     "description": "A live link's optic is receiving too little light - the module's own low alarm, or below the floor set "
                    "here. A dirty or bent fibre, a failing far-end laser, or too much attenuation. Only checked while the "
                    "link is up: an unused port with an optic in it reads -40 dBm and that is not a fault.",
     "sources": ["ssh"], "resolves": "when the reading recovers", "ttl_seconds": None,
     "params": {"floor_dbm": -12}},
    {"kind": "optic.rx_power_high", "group": "optic", "name": "High receive power", "default": "warning",
     "description": "A live link's receiver is being overdriven - too short a fibre for the optic, or the wrong module type. "
                    "It damages receivers over time.",
     "sources": ["ssh"], "resolves": "when the reading recovers", "ttl_seconds": None},
    {"kind": "optic.tx_fault", "group": "optic", "name": "Transmit fault", "default": "critical",
     "description": "The module reports a transmit fault, or its transmit power has fallen below its own low alarm, on a live "
                    "link. The laser is failing.",
     "sources": ["ssh"], "resolves": "when the module stops reporting it", "ttl_seconds": None},
    {"kind": "optic.temperature", "group": "optic", "name": "Optic temperature", "default": "warning",
     "description": "The transceiver's own temperature alarm, or above the ceiling set here. Hot optics fail and drift.",
     "sources": ["ssh"], "resolves": "when it cools", "ttl_seconds": None,
     "params": {"ceiling_c": 70}},
    {"kind": "optic.removed", "group": "optic", "name": "Transceiver removed", "default": "warning",
     "description": "A transceiver that was in the port is gone - from the SSH poll, and from the switch's own "
                    "'Optics SFP+ removed' log line.",
     "sources": ["syslog", "ssh"], "resolves": "when a transceiver is back in the port", "ttl_seconds": None},
    {"kind": "optic.unsupported", "group": "optic", "name": "Non-qualified optic", "default": "warning",
     "description": "The switch reports the installed optic as non-qualified. It may work, may be flaky, and is the first "
                    "thing to suspect when a link is unreliable.",
     "sources": ["syslog"], "resolves": "after 24 hours, or when the optic is replaced", "ttl_seconds": 86400},
    {"kind": "env.psu", "group": "env", "name": "Power supply fault", "default": "critical",
     "description": "A PSU reported down, removed or in alarm.",
     "sources": ["syslog", "ssh"], "resolves": "when it reports up again", "ttl_seconds": None},
    {"kind": "env.fan", "group": "env", "name": "Fan fault", "default": "critical",
     "description": "A fan or fan tray reported down, removed or in alarm.",
     "sources": ["syslog", "ssh"], "resolves": "when it reports up again", "ttl_seconds": None},
    {"kind": "env.temperature", "group": "env", "name": "Temperature alarm", "default": "critical",
     "description": "The device reported a temperature above its threshold.",
     "sources": ["syslog"], "resolves": "when it reports normal, or after 1 hour", "ttl_seconds": 3600},
    {"kind": "compute.cpu_high", "group": "compute", "name": "High CPU", "default": "warning",
     "description": "CPU utilisation stayed at or above the threshold for consecutive polls.",
     "sources": ["ssh", "syslog"], "resolves": "when it drops below the clear threshold", "ttl_seconds": None,
     "params": {"raise_percent": 90, "clear_percent": 80, "polls": 3}},
    {"kind": "compute.memory_high", "group": "compute", "name": "High memory", "default": "warning",
     "description": "Memory in use at or above the threshold.",
     "sources": ["ssh"], "resolves": "when it drops below the clear threshold", "ttl_seconds": None,
     "params": {"raise_percent": 90, "clear_percent": 85, "polls": 2}},
    {"kind": "compute.memory_error", "group": "compute", "name": "Memory / DDR error", "default": "critical",
     "description": "The device logged an ECC, DDR, parity or other memory error.",
     "sources": ["syslog"], "resolves": "after 1 hour without another", "ttl_seconds": 3600},
    {"kind": "device.unreachable", "group": "device", "name": "Device unreachable", "default": "critical",
     "description": "SSH polling failed for consecutive cycles.",
     "sources": ["ssh"], "resolves": "on the next successful poll", "ttl_seconds": None, "params": {"polls": 3}},
    {"kind": "device.rebooted", "group": "device", "name": "Device restarted", "default": "warning",
     "description": "The device logged a cold start or system restart.",
     "sources": ["syslog"], "resolves": "after 10 minutes", "ttl_seconds": 600},
    {"kind": "device.config_changed", "group": "device", "name": "Configuration changed", "default": "info",
     "description": "A configuration commit or copy was logged.",
     "sources": ["syslog"], "resolves": "after 5 minutes", "ttl_seconds": 300},
    {"kind": "device.syslog_silent", "group": "device", "name": "Syslog silent", "default": "warning",
     "description": "No syslog has arrived from the device for longer than the threshold - the receiver, the network path or the device's syslog config.",
     "sources": ["switchboard"], "resolves": "when a line arrives", "ttl_seconds": None, "params": {"minutes": 30}},
    {"kind": "protocol.stp_topology_change", "group": "protocol", "name": "Spanning-tree topology change", "default": "info",
     "description": "STP reported a topology change.",
     "sources": ["syslog"], "resolves": "after 5 minutes", "ttl_seconds": 300},
    {"kind": "protocol.neighbor_lost", "group": "protocol", "name": "Routing neighbour lost", "default": "critical",
     "description": "A BGP/OSPF neighbour or adjacency went down.",
     "sources": ["syslog"], "resolves": "when it comes back up, or after 1 hour", "ttl_seconds": 3600},
    {"kind": "syslog.rule", "group": "syslog", "name": "Syslog rule", "default": "warning",
     "description": "A user-defined syslog rule matched. Severity comes from the rule itself.",
     "sources": ["syslog"], "resolves": "per the rule: a clearing line and/or its timer", "ttl_seconds": None, "fixed": True},
    {"kind": "switchboard.selftest", "group": "switchboard", "name": "Fast-path self-test", "default": "info",
     "description": "The Events page's 'Send a test' line came back through the syslog receiver.",
     "sources": ["syslog"], "resolves": "after 60 seconds", "ttl_seconds": 60},
]
BY_KIND = {c["kind"]: c for c in CATALOG}


def group_name(group):
    return dict(GROUPS).get(group, group)


class EventSettings:
    """Per-kind severity and threshold overrides, in Postgres, cached for
    a few seconds so detection never costs a query per syslog line."""

    TTL = 5.0

    def __init__(self, db):
        self.db = db
        self._cache = None
        self._cached_at = 0.0

    def _overrides(self):
        now = time.monotonic()
        if self._cache is None or now - self._cached_at > self.TTL:
            rows = self.db.query("SELECT kind, severity, params FROM event_settings")
            out = {}
            for r in rows:
                try:
                    params = json.loads(r["params"] or "{}")
                except ValueError:
                    params = {}
                out[r["kind"]] = {"severity": r["severity"], "params": params}
            self._cache, self._cached_at = out, now
        return self._cache

    def invalidate(self):
        self._cache = None

    def entry(self, kind):
        base = BY_KIND.get(kind)
        if base is None:
            return None
        o = self._overrides().get(kind, {})
        params = dict(base.get("params") or {})
        params.update({k: v for k, v in (o.get("params") or {}).items() if k in params})
        return {**base, "severity": o.get("severity") or base["default"], "params": params,
                "overridden": kind in self._overrides()}

    def all(self):
        return [self.entry(c["kind"]) for c in CATALOG]

    def severity_for(self, kind):
        e = self.entry(kind)
        return e["severity"] if e else "warning"

    def params_for(self, kind):
        e = self.entry(kind)
        return e["params"] if e else {}

    def set(self, kind, severity=None, params=None):
        base = BY_KIND.get(kind)
        if base is None:
            raise ValueError(f"unknown event kind {kind!r}")
        if base.get("fixed") and severity is not None:
            raise ValueError(f"{base['name']}: severity is set on each rule, not here")
        current = self.entry(kind)
        new_sev = severity or current["severity"]
        if new_sev not in CHOICES:
            raise ValueError(f"severity must be one of {', '.join(CHOICES)}")
        new_params = dict(current["params"])
        for k, v in (params or {}).items():
            if k not in (base.get("params") or {}):
                raise ValueError(f"{base['name']} has no setting {k!r}")
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a whole number")
            # dBm floors are negative, so only the magnitude is bounded.
            if not -100000 <= v <= 100000:
                raise ValueError(f"{k} is out of range")
            new_params[k] = v
        self.db.execute(
            """INSERT INTO event_settings (kind, severity, params, updated_at) VALUES (%s, %s, %s, now())
               ON CONFLICT (kind) DO UPDATE SET severity = EXCLUDED.severity, params = EXCLUDED.params, updated_at = now()""",
            (kind, new_sev, json.dumps(new_params)),
        )
        self.invalidate()
        return self.entry(kind)

    def reset(self, kind):
        if kind not in BY_KIND:
            raise ValueError(f"unknown event kind {kind!r}")
        self.db.execute("DELETE FROM event_settings WHERE kind = %s", (kind,))
        self.invalidate()
        return self.entry(kind)


class PortSettings:
    """Per-port link-down severity. Absent = the catalogue's port.link_down
    severity; `ignore` = never raise for this port."""

    TTL = 5.0

    def __init__(self, db):
        self.db = db
        self._cache = None
        self._cached_at = 0.0

    def _all(self):
        now = time.monotonic()
        if self._cache is None or now - self._cached_at > self.TTL:
            rows = self.db.query("SELECT device_id, port, severity FROM port_settings")
            self._cache = {(r["device_id"], r["port"]): r["severity"] for r in rows}
            self._cached_at = now
        return self._cache

    def invalidate(self):
        self._cache = None

    def list(self, device_id):
        return {port: sev for (dev, port), sev in self._all().items() if dev == device_id}

    def has_override(self, device_id, port):
        return (device_id, port) in self._all()

    def severity_for(self, device_id, port, fallback):
        return self._all().get((device_id, port), fallback)

    def set(self, device_id, port, severity):
        """severity None or "default" clears the override."""
        if not device_id or not port:
            raise ValueError("device and port are required")
        if severity in (None, "", "default"):
            self.db.execute("DELETE FROM port_settings WHERE device_id = %s AND port = %s", (device_id, port))
            self.invalidate()
            return None
        if severity not in CHOICES:
            raise ValueError(f"severity must be one of {', '.join(CHOICES)} or default")
        self.db.execute(
            """INSERT INTO port_settings (device_id, port, severity) VALUES (%s, %s, %s)
               ON CONFLICT (device_id, port) DO UPDATE SET severity = EXCLUDED.severity""",
            (device_id, port, severity),
        )
        self.invalidate()
        return severity
