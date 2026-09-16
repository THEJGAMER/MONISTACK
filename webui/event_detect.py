"""Syslog detectors: one interpreted line in, zero or one event transition
out. Fed by the fast path (Vector's POST, sub-second) and, when that is
silent, by the Loki poll; a single timestamp cursor makes the two paths
one stream.

Each detector maps a line to a catalogue kind, a subject and a verdict
(raise / resolve). Severity is the site's setting for the kind (or the
port's own, for links); `ignore` drops the line. Everything here is a
transition the device *told* us about - the SSH reconciler
(event_reconcile.py) is what notices state we were never told about.
"""
import logging
import re
import time
from collections import deque

import event_catalog

log = logging.getLogger("webui.events.detect")


# --- hardware text, mirrored from the Vector interpreter on purpose -------
# (a second implementation survives a Vector regression - see the syslog
# README's changelog for the day the first one silently emptied)

def classify_alarm(detail):
    """-> (severity or None, active True/False/None, component text)."""
    low = (detail or "").lower()
    severity = active = None
    if "cleared" in low:
        active = False
    elif "major alarm" in low:
        severity, active = "critical", True
    elif "minor alarm" in low:
        severity, active = "minor", True
    elif "is down" in low or "is removed" in low or "offline" in low or "power off" in low:
        severity, active = "minor", True
    elif "is up" in low or "is inserted" in low or "online" in low or "power on" in low:
        active = False
    component = None
    if severity is not None or active is False:
        component = detail
        component = re.sub(r"(?i)^(major|minor)\s+alarm\s+cleared\s*:?\s*", "", component)
        component = re.sub(r"(?i)^(major alarm:\s*|minor alarm\s*:\s*)", "", component)
        component = re.sub(r"(?i)^snmp\s+trap\s+generated\s*:\s*", "", component)
        component = re.sub(r"(?i)\s+is\s+(up|down|inserted|removed)\s*$", "", component)
        component = re.sub(r"(?i)\s+(alarm\s+)?reported(\s+in\s+unit\s+\d+)?\s+is\s+cleared\s*$", "", component)
        component = re.sub(r"(?i)\s+(went\s+)?(offline|online)\s*$", "", component)
        component = component.strip()
    return severity, active, component


_COMPONENT_RE = re.compile(r"(?i)(power\s*suppl\w*|fan\s*(?:tray)?)\s*(\d+)\s+in\s+unit\s+(\d+)")


def parse_component(component):
    """-> (kind 'env.psu'|'env.fan', subject) or None."""
    if not component:
        return None
    m = _COMPONENT_RE.search(component)
    if not m:
        return None
    prefix, bay, unit = m.groups()
    if prefix.lower().startswith("power"):
        return "env.psu", f"PSU {bay} (unit {unit})"
    return "env.fan", f"Fan tray {bay} (unit {unit})"


# --- pattern detectors: (kind, fire regex, clear regex, subject regex) ----------

_TEMP_FIRE = re.compile(r"(?i)temperature.*(high|exceed|critical|over|above|alarm)")
_TEMP_CLEAR = re.compile(r"(?i)temperature.*(normal|cleared|ok|below)")
_MEM_ERR = re.compile(r"(?i)\b(ECC|DDR|parity)\b.*\berror|memory error|uncorrectable|correctable error")
_CPU_FIRE = re.compile(r"(?i)cpu\s+util[a-z]*\s*.*(high|exceed|above|threshold)")
_CPU_CLEAR = re.compile(r"(?i)cpu\s+util[a-z]*\s*.*(normal|below|cleared)")
_REBOOT = re.compile(r"(?i)cold\s*start|system\s+(re)?start(ed)?|SYSTEM_RESTART|KERN_COLD_START|CHASSISD_.*BOOT|%SYS-5-RESTART|rebooted|Booting")
_CONFIG = re.compile(r"(?i)UI_COMMIT_COMPLETED|UI_COMMIT_PROGRESS.*commit complete|CONFIG_I|configured from|copy running-config|startup-config|commit complete")
_STP = re.compile(r"(?i)topology\s*change")
_NEIGH_FIRE = re.compile(r"(?i)\b(bgp|ospf|neighbou?r|adjacency)\b.*\b(down|lost|deleted|expired|dead)\b")
_NEIGH_CLEAR = re.compile(r"(?i)\b(bgp|ospf|neighbou?r|adjacency)\b.*\b(up|established|full)\b")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Lines that carry text a *person* typed, not something the device
# observed. Confirmed live: Junos logs every CLI line as
# `mgd[49252]: UI_CMDLINE_READ_LINE: User 'root', command 'show lldp
# neighbors '`, so running the free-text fault patterns over it means
# typing `show interfaces | match down` raises a fault. Structured
# signals (link_event, the alarm_* fields) are never set on these, so
# nothing real is lost by skipping the text detectors entirely.
_CLI_ECHO = re.compile(r"(?i)UI_CMDLINE|UI_CHILD_START|UI_DBASE|UI_COMMIT_PROGRESS|command '")
# LLDP is not a routing protocol. An LLDP neighbour disappears because a
# link went down, which is already reported as the link event - counting
# it as a lost adjacency turned one unplug into two critical events.
_LLDP = re.compile(r"(?i)\bLLDP")
# Dell OS9's optic messages, both captured live on this fleet:
#   %IFAGT-5-REMOVED_OPTICS_PLUS: Optics SFP+ removed in slot 1 port 47
#   %IFAGT-5-UNSUP_OPTICS: Non-qualified optics in slot 1 port 47
# The insert wording is the documented counterpart and has not been seen
# here yet, so it is matched generically (inserted/installed) rather than
# pinned to one mnemonic.
_OPTIC_OUT = re.compile(r"(?i)REMOVED_OPTICS|optics?\b[^.]*\bremoved")
_OPTIC_IN = re.compile(r"(?i)INSERTED_OPTICS|INSTALLED_OPTICS|optics?\b[^.]*\b(inserted|installed)")
_OPTIC_UNSUP = re.compile(r"(?i)UNSUP_OPTICS|non-?qualified optics")
_SLOT_PORT = re.compile(r"(?i)slot\s+(\d+)\s+port\s+(\d+)")
# Dell OS9's LACP membership lines. The mnemonics are exact; the
# port-channel number is pulled from the text for the event's detail.
_LAG_OUT = re.compile(r"(?i)PORT[-_]UNGROUPED|exited\s+port-channel")
_LAG_IN = re.compile(r"(?i)PORT[-_]GROUPED|joined\s+port-channel")
_LAG_NUM = re.compile(r"(?i)port-channel\s+(\d+)")
_JUNOS_UNIT = re.compile(r"\.\d+$")


class SyslogDetector:
    def __init__(self, store, settings, ports):
        self.store = store
        self.settings = settings
        self.ports = ports
        self.cursor_ns = 0
        self._downs = {}          # (device_key, port) -> deque of monotonic times
        self.ignored = 0
        self.acted = 0

    # --- entry -------------------------------------------------------------

    def process(self, events, device_for, rules=None, source="syslog"):
        """`events` oldest first with `_timestamp_ns`; `device_for(e)` ->
        (device_id or "", name). Returns transitions made."""
        acted = 0
        newest = self.cursor_ns
        for e in events:
            ts = int(e.get("_timestamp_ns") or 0)
            if ts and ts <= self.cursor_ns:
                continue
            newest = max(newest, ts)
            try:
                acted += self._one(e, device_for, rules or [], source)
            except Exception:
                log.exception("detector failed on %r", str(e.get("message"))[:120])
        self.cursor_ns = newest
        self.acted += acted
        return acted

    # --- helpers --------------------------------------------------------------

    def _raise(self, kind, device_id, device, subject, title, e, source, severity=None, labels=None):
        sev = severity or self.settings.severity_for(kind)
        if sev == "ignore":
            self.ignored += 1
            return 0
        event, created = self.store.raise_event(
            kind, sev, device_id, device, subject, title, detail=str(e.get("message") or "")[:1000],
            labels=labels or {}, source=source, signal_at=e.get("device_timestamp") or e.get("timestamp"))
        return 1 if created else 0

    def _resolve(self, kind, device_id, device, subject, e, source):
        from eventstore import signature_for
        return 1 if self.store.resolve(signature_for(kind, device_id or device, subject), by=source,
                                       detail=str(e.get("message") or "")[:500]) else 0

    # --- the detectors -------------------------------------------------------------

    def _one(self, e, device_for, rules, source):
        device_id, device = device_for(e)
        msg = str(e.get("message") or "")
        detail = str(e.get("detail") or msg)
        category = e.get("event_category") or "other"
        acted = 0

        # LAG membership. Deliberately checked before link state and
        # returned on: a member leaving its bundle is its own fact, and on
        # this hardware it is frequently the *only* thing logged - the
        # SSH poll is what then reports the link itself, seconds later.
        if e.get("interface") and (_LAG_OUT.search(msg) or _LAG_IN.search(msg)):
            port = str(e["interface"])
            chan = _LAG_NUM.search(msg)
            subject = f"{port} in port-channel {chan.group(1)}" if chan else port
            if _LAG_IN.search(msg):
                return self._resolve("port.lag_member_lost", device_id, device, subject, e, source)
            return self._raise("port.lag_member_lost", device_id, device, subject,
                               f"LAG member left: {subject} on {device}", e, source)

        # links: what the device says about its ports
        if e.get("link_event") and e.get("interface") and e.get("link_state") in ("up", "down"):
            port = str(e["interface"])
            if e.get("vendor") == "junos" and _JUNOS_UNIT.search(port):
                return 0   # the logical unit follows its physical port; one event, not two
            if e["link_state"] == "down":
                sev = self.ports.severity_for(device_id, port, self.settings.severity_for("port.link_down"))
                acted += self._raise("port.link_down", device_id, device, port, f"Link down: {port} on {device}", e, source, severity=sev)
                acted += self._flap(device_id, device, port, e, source)
            else:
                acted += self._resolve("port.link_down", device_id, device, port, e, source)
            return acted

        # optics the switch tells us about itself
        if _OPTIC_OUT.search(msg) or _OPTIC_IN.search(msg) or _OPTIC_UNSUP.search(msg):
            slot = _SLOT_PORT.search(msg)
            subject = e.get("interface") or (f"slot {slot.group(1)} port {slot.group(2)}" if slot else "optic")
            if _OPTIC_UNSUP.search(msg):
                return self._raise("optic.unsupported", device_id, device, subject,
                                   f"Non-qualified optic: {subject} on {device}", e, source)
            if _OPTIC_IN.search(msg):
                return self._resolve("optic.removed", device_id, device, subject, e, source)
            return self._raise("optic.removed", device_id, device, subject,
                               f"Transceiver removed: {subject} on {device}", e, source)

        # environment: fans, PSUs, temperature
        if category == "hardware" or e.get("alarm_active") is not None:
            severity, active, component = classify_alarm(detail)
            parsed = parse_component(component) if component else None
            if parsed:
                kind, subject = parsed
                if active:
                    return self._raise(kind, device_id, device, subject, f"{event_catalog.BY_KIND[kind]['name']}: {subject} on {device}", e, source)
                if active is False:
                    return self._resolve(kind, device_id, device, subject, e, source)
            if _TEMP_FIRE.search(detail):
                return self._raise("env.temperature", device_id, device, "temperature", f"Temperature alarm on {device}", e, source)
            if _TEMP_CLEAR.search(detail):
                return self._resolve("env.temperature", device_id, device, "temperature", e, source)

        # Everything below matches on free text, so a line that merely
        # quotes what someone typed stops here.
        if _CLI_ECHO.search(msg):
            return acted

        # compute
        if _MEM_ERR.search(detail):
            return self._raise("compute.memory_error", device_id, device, "memory", f"Memory error on {device}: {detail[:80]}", e, source)
        if _CPU_FIRE.search(detail):
            return self._raise("compute.cpu_high", device_id, device, "cpu", f"High CPU on {device}", e, source)
        if _CPU_CLEAR.search(detail):
            return self._resolve("compute.cpu_high", device_id, device, "cpu", e, source)

        # device
        if _REBOOT.search(detail) and category in ("hardware", "other"):
            acted += self._raise("device.rebooted", device_id, device, "restart", f"{device} restarted", e, source)
        if _CONFIG.search(msg):
            acted += self._raise("device.config_changed", device_id, device, "config", f"Configuration changed on {device}", e, source)

        # protocol
        if _STP.search(detail) and (category == "spanning-tree" or (e.get("facility") or "").upper() == "STP"):
            acted += self._raise("protocol.stp_topology_change", device_id, device, "stp", f"STP topology change on {device}", e, source)
        routing = category in ("routing", "other") and not _LLDP.search(msg) and not _LLDP.search(str(e.get("facility") or ""))
        if routing and _NEIGH_FIRE.search(detail):
            ip = _IPV4.search(detail)
            # A bare "neighbour" subject would collapse two different
            # neighbours into one event, so fall back to the interface.
            subject = ip.group(0) if ip else (e.get("interface") or "neighbour")
            acted += self._raise("protocol.neighbor_lost", device_id, device, subject, f"Routing neighbour lost on {device}: {subject}", e, source)
        elif routing and _NEIGH_CLEAR.search(detail):
            ip = _IPV4.search(detail)
            acted += self._resolve("protocol.neighbor_lost", device_id, device, ip.group(0) if ip else (e.get("interface") or "neighbour"), e, source)

        # self-test
        if (e.get("mnemonic") or "").upper() == "SWITCHBOARD_SELFTEST":
            acted += self._raise("switchboard.selftest", device_id, device, "selftest", "Fast-path self-test", e, source,
                                 labels={"nonce": (re.search(r"nonce=([A-Za-z0-9]+)", msg) or [None, None])[1]})

        # user rules
        for rule in rules:
            acted += self._rule(rule, e, device_id, device, source)
        return acted

    def _flap(self, device_id, device, port, e, source):
        key = (device_id or device, port)
        q = self._downs.setdefault(key, deque(maxlen=10))
        now = time.monotonic()
        q.append(now)
        recent = [t for t in q if now - t <= 300]
        if len(recent) >= 3:
            return self._raise("port.flapping", device_id, device, port, f"Link flapping: {port} on {device} ({len(recent)} downs in 5 min)", e, source)
        return 0

    def _rule(self, rule, e, device_id, device, source):
        import syslog_alerting
        if not rule.get("enabled"):
            return 0
        verdict = syslog_alerting.matches(rule, e)
        if verdict is None:
            return 0
        interface = (e.get("interface") or "") if rule.get("per_interface") else ""
        subject = rule["name"] + (f" {interface}" if interface else "")
        if verdict:
            detail = str(e.get("detail") or e.get("message") or "")[:160]
            return self._raise("syslog.rule", device_id, device, subject, f"{rule['name']} on {device}" + (f" {interface}" if interface else "") + (f": {detail}" if detail else ""),
                               e, source, severity=rule["severity"], labels={"rule_id": str(rule["id"]), "rule": rule["name"], "interface": interface})
        return self._resolve("syslog.rule", device_id, device, subject, e, source)
