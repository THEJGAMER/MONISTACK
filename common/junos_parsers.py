"""Regex/text parsers for Juniper Junos (EX3300) `show` command output.

Kept separate from parsers.py rather than merged into it: Dell OS9 and
Junos output share no format in common, so one file per platform's
parsing is the clearer boundary now that a second platform exists. Every
sample below was captured live from a real EX3300-48P (15.1R7.9), not
guessed from Junos documentation - same practice as parsers.py.
"""
import re

ALARM_ROW_RE = re.compile(r"^(?P<time>\S+ \S+ UTC)\s+(?P<cls>Major|Minor)\s+(?P<desc>.+?)\s*$", re.MULTILINE)


def parse_junos_alarms(chassis_text, system_text):
    """Merges `show chassis alarms` + `show system alarms` into the same
    {'minor': [...], 'major': [...]} shape parsers.parse_alarms already
    returns, so downstream code (Switch Status rendering, summarize)
    doesn't need a parallel shape. Both real outputs are simple "N alarms
    currently active" + a Class/Description table when N > 0; entries
    that appear in both (a real chassis alarm is also a system alarm) are
    deduped by (class, description)."""
    out = {"minor": [], "major": []}
    seen = set()
    for text in (chassis_text, system_text):
        for m in ALARM_ROW_RE.finditer(text):
            key = (m.group("cls"), m.group("desc"))
            if key in seen:
                continue
            seen.add(key)
            entry = {"text": m.group("desc"), "duration": None, "time": m.group("time")}
            (out["major"] if m.group("cls") == "Major" else out["minor"]).append(entry)
    return out


def parse_junos_routing_engine(text):
    """Returns temp/CPU/memory/uptime from `show chassis routing-engine`."""
    out = {}
    if m := re.search(r"CPU temperature\s+(\d+) degrees C", text):
        out["temp_c"] = int(m.group(1))
    if m := re.search(r"DRAM\s+(\d+) MB", text):
        out["dram_mb"] = int(m.group(1))
    if m := re.search(r"Memory utilization\s+(\d+) percent", text):
        out["memory_pct"] = int(m.group(1))
    cpu = {}
    for name, key in (("User", "user"), ("Background", "background"), ("Kernel", "kernel"), ("Interrupt", "interrupt"), ("Idle", "idle")):
        if m := re.search(rf"{name}\s+(\d+) percent", text):
            cpu[key] = int(m.group(1))
    out["cpu"] = cpu
    if m := re.search(r"Model\s+(.+)", text):
        out["model"] = m.group(1).strip()
    if m := re.search(r"Serial ID\s+(\S+)", text):
        out["serial"] = m.group(1).strip()
    if m := re.search(r"Uptime\s+(.+)", text):
        out["uptime"] = m.group(1).strip()
    return out


def parse_junos_environment(text):
    """Returns {'fans': [...], 'psus': [...], 'sensors': {...}} from `show
    chassis environment` - a Class/Item/Status/Measurement table where
    blank leading columns continue the previous row's Class, so the class
    for each row is tracked as lines are walked rather than read per-line."""
    lines = text.splitlines()
    header = next((l for l in lines if l.strip().startswith("Class")), None)
    if not header:
        return {"fans": [], "psus": [], "sensors": {}}
    item_col = header.find("Item")
    status_col = header.find("Status")
    measurement_col = header.find("Measurement")

    fans, psus, sensors = [], [], {}
    current_class = None
    for line in lines:
        if not line.strip() or line is header:
            continue
        class_field = line[:item_col].strip()
        if class_field:
            current_class = class_field
        item = line[item_col:status_col].strip()
        status = line[status_col:measurement_col].strip()
        measurement = line[measurement_col:].strip()
        if not item:
            continue
        if current_class == "Fans":
            fans.append({"item": item, "status": status, "measurement": measurement})
        elif current_class == "Power":
            psus.append({"item": item, "status": status, "measurement": measurement})
        elif current_class == "Temp":
            sensors[item] = measurement
    return {"fans": fans, "psus": psus, "sensors": sensors}


_PHYSICAL_IFACE_RE = re.compile(r"^(?P<iface>(?:ge|xe|ae)[-\d/]*)\s+(?P<admin>up|down)\s+(?P<link>up|down)", re.MULTILINE)


def parse_junos_interfaces_terse(text):
    """Returns a list of physical/aggregate interfaces (ge-*/xe-*/ae*)
    from `show interfaces terse` - deliberately excludes the ".0" logical
    unit lines (e.g. "ge-0/0/0.0"), which the regex naturally skips since
    there's no whitespace directly after the physical port name on those
    lines (the ".0" sits in between)."""
    return [
        {"interface": m.group("iface"), "admin": m.group("admin"), "link": m.group("link")}
        for m in _PHYSICAL_IFACE_RE.finditer(text)
    ]


def parse_junos_interfaces_descriptions(text):
    """Returns a list of {'interface','admin','link','description'} from
    `show interfaces descriptions` - only interfaces with a configured
    description appear in this output at all."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(?P<iface>\S+)\s+(?P<admin>up|down)\s+(?P<link>up|down)\s+(?P<desc>.+)$", line)
        if m:
            out.append({
                "interface": m.group("iface"),
                "admin": m.group("admin"),
                "link": m.group("link"),
                "description": m.group("desc").strip(),
            })
    return out


_LLDP_HEADER_RE = re.compile(r"^Local Interface\s+Parent Interface\s+Chassis Id\s+Port info\s+System Name")


def _strip_unit(iface):
    """Junos reports its own interfaces with a ".0" logical-unit suffix in
    LLDP output (e.g. "xe-0/1/3.0") - stripped here so it matches the
    unit-less form used everywhere else in this app (status poller,
    `show interfaces terse`, the front-panel port names)."""
    return re.sub(r"\.\d+$", "", iface)


def parse_junos_lldp(text):
    """Returns a list of {'local_port','parent_interface','remote_chassis_id',
    'remote_port_description','remote_system_name'} from `show lldp
    neighbors`. Columns are separated by runs of 2+ spaces rather than
    fixed positions - `Port info` (the neighbor's own interface
    name/description, e.g. "TenGigabitEthernet 1/47") contains a single
    internal space, which a fixed-column split would misparse whenever the
    value overflows the header's column width (confirmed live: it always
    does, on both real devices in this fleet)."""
    lines = text.splitlines()
    header_idx = next((i for i, l in enumerate(lines) if _LLDP_HEADER_RE.match(l)), None)
    if header_idx is None:
        return []

    out = []
    for line in lines[header_idx + 1:]:
        if not line.strip():
            break
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 4:
            continue
        local, parent, chassis, port_desc = parts[:4]
        sysname = parts[4] if len(parts) > 4 else None
        out.append({
            "local_port": _strip_unit(local),
            "parent_interface": _strip_unit(parent) if parent != "-" else None,
            "remote_chassis_id": chassis,
            "remote_port_description": port_desc or None,
            "remote_system_name": sysname,
        })
    return out


_ARP_ROW_RE = re.compile(r"^(?P<mac>[0-9a-fA-F:]{17})\s+(?P<ip>\S+)\s+\S+\s+(?P<interface>\S+)\s+\S+", re.MULTILINE)


def parse_arp(text):
    """Returns a list of {'ip','mac','interface'} from `show arp`."""
    return [
        {"ip": m.group("ip"), "mac": m.group("mac").lower(), "interface": m.group("interface")}
        for m in _ARP_ROW_RE.finditer(text)
    ]


_ETH_SWITCHING_ROW_RE = re.compile(
    r"^\s*\S+\s+(?P<mac>[0-9a-fA-F:]{17})\s+(?P<type>\S+)\s+\S+\s+(?P<interface>\S+)", re.MULTILINE
)


_EXTENSIVE_BLOCK_START_RE = re.compile(r"^Physical interface: (?P<iface>\S+),", re.MULTILINE)


def _physical_blocks(text, start_re):
    starts = list(start_re.finditer(text))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        yield m.group("iface"), text[m.start():end]


_JUNOS_INPUT_ERRORS_RE = re.compile(
    r"Input errors:\s*\n\s*Errors: (\d+), Drops: (\d+), Framing errors: \d+, Runts: \d+, Policed discards: (\d+),"
)
_JUNOS_OUTPUT_ERRORS_RE = re.compile(
    r"Output errors:\s*\n\s*Carrier transitions: \d+, Errors: (\d+), Drops: (\d+), Collisions: (\d+),"
)


def parse_junos_interfaces_errors(text):
    """Returns {interface: {'input_errors','input_discards','output_errors',
    'output_discards'}} from a bare `show interfaces extensive` (no port
    arg - confirmed live: one round trip returns every physical
    interface's full detail, same "ask once, parse all ports" shape as
    Dell's bare `show interfaces`). Unlike Dell's ad hoc sum of several
    always-near-zero counters, Junos already reports its own aggregate
    "Errors" counter per direction directly - used as-is; "discards" is
    Drops (+ Policed discards on input), a distinct queue/congestion
    signal from actual frame errors, same split as the Dell parser."""
    out = {}
    for iface, block in _physical_blocks(text, _EXTENSIVE_BLOCK_START_RE):
        in_m = _JUNOS_INPUT_ERRORS_RE.search(block)
        out_m = _JUNOS_OUTPUT_ERRORS_RE.search(block)
        if not (in_m or out_m):
            continue
        input_errors, input_drops, input_policed = (int(x) for x in in_m.groups()) if in_m else (0, 0, 0)
        output_errors, output_drops, output_collisions = (int(x) for x in out_m.groups()) if out_m else (0, 0, 0)
        out[iface] = {
            "input_errors": input_errors,
            "input_discards": input_drops + input_policed,
            "output_errors": output_errors + output_collisions,
            "output_discards": output_drops,
        }
    return out


_JUNOS_LINK_PARTNER_SPEED_RE = re.compile(r"Link partner Speed:\s+(\d+)\s*Mbps")


def parse_junos_interfaces_speed(text):
    """Returns {interface: speed_mbps} from the same bare `show interfaces
    extensive` output the other extensive-based parsers here read. Junos's
    own `Speed:` field on the `Link-level type:` line reports the port's
    *configured* mode ("Auto" on every real copper port on this fleet, not
    a negotiated rate) - the actual negotiated rate is one line further
    down, under "Link partner Speed: N Mbps" (confirmed live on a real
    EX3300 port at 1000 Mbps). Only present for ports with an active link
    partner - interfaces without it are simply omitted, not guessed at."""
    out = {}
    for iface, block in _physical_blocks(text, _EXTENSIVE_BLOCK_START_RE):
        m = _JUNOS_LINK_PARTNER_SPEED_RE.search(block)
        if m:
            out[iface] = int(m.group(1))
    return out


_JUNOS_TRAFFIC_RE = re.compile(r"Input\s+bytes\s*:\s+\d+\s+(\d+)\s+bps\s*\n\s*Output\s+bytes\s*:\s+\d+\s+(\d+)\s+bps")


def parse_junos_interfaces_traffic_mbps(text):
    """Returns {interface: {'input_mbps','output_mbps'}} from the same bare
    `show interfaces extensive` output parse_junos_interfaces_errors reads
    - Junos reports a live device-computed bps rate right in "Traffic
    statistics" (confirmed live), the same kind of real rolling-average
    rate Dell's "Rate info" gives, not something computed here from raw
    byte-counter deltas across polls."""
    out = {}
    for iface, block in _physical_blocks(text, _EXTENSIVE_BLOCK_START_RE):
        m = _JUNOS_TRAFFIC_RE.search(block)
        if not m:
            continue
        in_bps, out_bps = (int(x) for x in m.groups())
        out[iface] = {"input_mbps": in_bps / 1_000_000, "output_mbps": out_bps / 1_000_000}
    return out


_OPTICS_BLOCK_START_RE = re.compile(r"^Physical interface: (?P<iface>\S+)\s*$", re.MULTILINE)


def parse_junos_optics_diagnostics(text):
    """Returns {interface: {'present': bool, 'rx_power_dbm', 'tx_power_dbm',
    'temperature_c'}} from a bare `show interfaces diagnostics optics` (no
    port arg - confirmed live: returns every optics-capable interface in
    one round trip, each as "Physical interface: <name>" followed by
    either "Optical diagnostics : N/A" for a non-DOM-capable link (copper,
    DAC, or no transceiver - confirmed live on this fleet's real EX3300,
    whose populated ports are 10GBASE-CU1M DACs) or a real reading table
    when an actual optical module with DOM support is present.

    The `present`-with-real-numbers case is NOT verified live on this
    fleet (no port here currently has an actual optical transceiver
    installed, only DACs/copper) - the field-level regexes for that
    branch would be guessing from Junos's documented output format rather
    than a captured real device, which this codebase's own practice
    avoids. So today this only ever returns `present: False` in
    production; the moment a real optic is confirmed live on some Junos
    device, this needs the same live-capture-then-parse treatment as
    every other parser here before trusting its numbers."""
    out = {}
    for iface, block in _physical_blocks(text, _OPTICS_BLOCK_START_RE):
        if "N/A" in block:
            out[iface] = {
                "present": False, "dom_supported": False, "type": None,
                "rx_power_dbm": None, "tx_power_dbm": None, "temperature_c": None,
            }
        # else: a real reading is present but not yet verified/parsed (see
        # docstring) - deliberately omitted rather than guessed.
    return out


def parse_ethernet_switching_table(text):
    """Returns a list of {'mac','interface'} from `show ethernet-switching
    table` - every MAC the switch has ever learned on every port, not just
    the ones that happen to speak LLDP (same idea as Dell's `show
    mac-address-table` - a second, independent way to discover what's on a
    port). Flood entries ("*", interface "All-members") and the switch's
    own router MAC ("Static", interface "Router") are real rows in this
    table but not hosts, so both are skipped."""
    out = []
    for m in _ETH_SWITCHING_ROW_RE.finditer(text):
        if m.group("type") == "Static" or m.group("interface") in ("All-members", "Router"):
            continue
        out.append({"mac": m.group("mac").lower(), "interface": _strip_unit(m.group("interface"))})
    return out
