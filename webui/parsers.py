"""Regex/text parsers for Dell OS9 (S4048-ON) `show` command output."""
import re

# Force10/Dell OS9 spells its own interfaces out in full in `show lldp
# neighbors detail` ("Local Port ID: TenGigabitEthernet 1/47") even though
# every other command in this app - and the Console's own port dropdowns -
# uses the short form ("Te 1/47"). Mapping back to short form here keeps
# LLDP-derived port names comparable to (and displayable next to) status
# poller / front-panel port names instead of introducing a second spelling.
_LLDP_PORT_PREFIXES = {
    "TenGigabitEthernet": "Te",
    "GigabitEthernet": "Gi",
    "FortyGigE": "Fo",
    "HundredGigE": "Hu",
    "TwentyFiveGigE": "Tf",
    "ManagementEthernet": "Ma",
    "Port-channel": "Po",
}


def _short_port_name(name):
    for long, short in _LLDP_PORT_PREFIXES.items():
        if name.startswith(long):
            return short + name[len(long):]
    return name


def parse_cpu(text):
    """Returns {'cores': {core_id: {'5sec':.., '1min':.., '5min':..}}, 'overall': {...}}"""
    result = {"cores": {}, "overall": {}}
    for m in re.finditer(
        r"^(CORE\s+(\d+)|Overall)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$",
        text,
        re.MULTILINE,
    ):
        label, core_id, s5, m1, m5 = m.groups()
        vals = {"5sec": float(s5), "1min": float(m1), "5min": float(m5)}
        if label == "Overall":
            result["overall"] = vals
        else:
            result["cores"][core_id] = vals
    return result


def parse_memory(text):
    """Returns {'total':int,'used':int,'free':int,'lowest':int,'largest':int} in bytes."""
    m = re.search(
        r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", text.strip(), re.MULTILINE
    )
    if not m:
        return {}
    total, used, free, lowest, largest = (int(g) for g in m.groups())
    return {
        "total": total,
        "used": used,
        "free": free,
        "lowest": lowest,
        "largest": largest,
    }


def parse_environment(text):
    """Returns fans, power supplies, unit status and thermal sensors."""
    out = {"fans": [], "psus": [], "units": [], "sensors": {}}

    for m in re.finditer(
        r"^\s*(\d+)\s+(\d+)\s+(up|down)\s+(up|down)\s+(\d+)\s+(up|down)\s+(\d+)\s*$",
        text,
        re.MULTILINE,
    ):
        unit, bay, tray, fan1, speed1, fan2, speed2 = m.groups()
        out["fans"].append(
            {
                "unit": unit,
                "bay": bay,
                "tray_status": tray,
                "fan1_status": fan1,
                "fan1_rpm": int(speed1),
                "fan2_status": fan2,
                "fan2_rpm": int(speed2),
            }
        )

    # A physically removed fan tray reports TrayStatus `absent` with none of
    # the Fan1/Speed/Fan2/Speed columns present at all - confirmed live
    # (`' 1    3     absent      '`) - so the row above never matches it.
    # Same bug shape as the PSU fix below: the row being silently dropped
    # from `out["fans"]` meant `s4048_fan_status` just held its last-known
    # "up" reading forever, so pulling a fan tray produced no alarm at all
    # despite `s4048_fan_status == 0` being exactly the rule meant to catch
    # this. Treated as both fans down (0 RPM) - "absent" is a strictly
    # worse cooling state than "down" (there's nothing there to fail back
    # to), so it must alert at least as readily.
    for m in re.finditer(r"^\s*(\d+)\s+(\d+)\s+absent\s*$", text, re.MULTILINE):
        unit, bay = m.groups()
        out["fans"].append(
            {
                "unit": unit,
                "bay": bay,
                "tray_status": "absent",
                "fan1_status": "down",
                "fan1_rpm": 0,
                "fan2_status": "down",
                "fan2_rpm": 0,
            }
        )

    # Type is `AC`/`DC` when the PSU is up, but confirmed live: a PSU that's
    # down or removed reports Type `UNKNOWN` instead - the device stops
    # being able to identify it, but the row is still there. An earlier
    # version of this regex only matched AC|DC, which silently dropped the
    # one PSU row that actually matters (the failed one) from `out["psus"]`
    # entirely - in the exporter this meant Prometheus's psu_status gauge
    # just went stale at its last "up" value forever instead of ever
    # reflecting the fault, so the S4048PSUDown alert never fired despite
    # a real, ongoing failure.
    for m in re.finditer(
        r"^\s*(\d+)\s+(\d+)\s+(up|down)\s+(AC|DC|UNKNOWN)\s+(up|down)\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+",
        text,
        re.MULTILINE,
    ):
        unit, bay, status, ptype, fanstatus, fanspeed, power, avgpower = m.groups()
        out["psus"].append(
            {
                "unit": unit,
                "bay": bay,
                "status": status,
                "type": ptype,
                "fan_status": fanstatus,
                "fan_speed_rpm": int(fanspeed),
                "power_watts": int(power),
                "avg_power_watts": int(avgpower),
            }
        )

    for m in re.finditer(
        r"^\*?\s*(\d+)\s+(online|offline)\s+(-?\d+)C\s+(ok|failed?)\s*$",
        text,
        re.MULTILINE,
    ):
        unit, status, temp, voltage = m.groups()
        out["units"].append(
            {
                "unit": unit,
                "status": status,
                "temp_c": int(temp),
                "voltage_ok": voltage.startswith("ok"),
            }
        )

    m = re.search(
        r"CPU\s+MAC\s+NIC\s+AMB\s+BCM\s*\n[-\s]*\n?\s*(\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)",
        text,
    )
    if m:
        unit, cpu, mac, nic, amb, bcm = m.groups()
        out["sensors"] = {
            "cpu": int(cpu),
            "mac": int(mac),
            "nic": int(nic),
            "amb": int(amb),
            "bcm": int(bcm),
        }
    return out


_STATUS_HEADER_RE = re.compile(r"^Port\s+Description\s+Status\s+Speed\s+Duplex\s+Vlan")


def parse_interfaces_status(text):
    """Returns list of {'port','description','status','speed','duplex','vlan'}."""
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if _STATUS_HEADER_RE.match(line):
            header_idx = i
            break
    if header_idx is None:
        return []

    header = lines[header_idx]
    idx_desc = header.index("Description")
    idx_status = header.index("Status")
    idx_speed = header.index("Speed")
    idx_duplex = header.index("Duplex")
    idx_vlan = header.index("Vlan")

    results = []
    for line in lines[header_idx + 1 :]:
        if not line.strip():
            continue
        if len(line) < idx_status:
            continue
        port = line[0:idx_desc].strip()
        description = line[idx_desc:idx_status].strip()
        status = line[idx_status:idx_speed].strip()
        speed = line[idx_speed:idx_duplex].strip()
        duplex = line[idx_duplex:idx_vlan].strip()
        vlan = line[idx_vlan:].strip()
        if not port:
            continue
        results.append(
            {
                "port": port,
                "description": description,
                "status": status,
                "speed": speed,
                "duplex": duplex,
                "vlan": vlan,
            }
        )
    return results


_IFACE_RATE_RE = re.compile(
    r"^(TenGigabitEthernet|fortyGigE)\s+(\S+)\s+is\s+(?:up|down).*?"
    r"Rate info \(interval \d+ seconds\):\s*\n"
    r"\s*Input\s+([\d.]+)\s+Mbits/sec,\s+(\d+)\s+packets/sec.*?\n"
    r"\s*Output\s+([\d.]+)\s+Mbits/sec,\s+(\d+)\s+packets/sec",
    re.MULTILINE | re.DOTALL,
)
_RATE_PREFIX = {"TenGigabitEthernet": "Te", "fortyGigE": "Fo"}


def parse_interfaces_rates(text):
    """Returns {port: {input_mbps, input_pps, output_mbps, output_pps}} from
    a bare `show interfaces` (no args - dumps every interface's full detail
    in one round trip, verified live: one command returns all 54 ports'
    blocks, each with the switch's own device-computed "Rate info" section
    - a real 299-second rolling average, not something derived by polling
    counters ourselves). Used as the Front Panel's per-port activity
    signal: real traffic, not a fabricated blink."""
    out = {}
    for m in _IFACE_RATE_RE.finditer(text):
        iftype, num, in_mbps, in_pps, out_mbps, out_pps = m.groups()
        port = f"{_RATE_PREFIX[iftype]} {num}"
        out[port] = {
            "input_mbps": float(in_mbps),
            "input_pps": int(in_pps),
            "output_mbps": float(out_mbps),
            "output_pps": int(out_pps),
        }
    return out


_IFACE_BLOCK_START_RE = re.compile(r"^(TenGigabitEthernet|fortyGigE)\s+(\S+)\s+is\s+(?:up|down)", re.MULTILINE)
_INPUT_RUNTS_RE = re.compile(r"(\d+)\s+runts,\s+(\d+)\s+giants")
_INPUT_CRC_RE = re.compile(r"(\d+)\s+CRC,\s+(\d+)\s+overrun,\s+(\d+)\s+discarded")
_OUTPUT_STATS_RE = re.compile(r"(\d+)\s+throttles,\s+(\d+)\s+discarded,\s+(\d+)\s+collisions,\s+(\d+)\s+wreddrops")


def parse_interfaces_errors(text):
    """Returns {port: {'input_errors','input_discards','output_errors',
    'output_discards'}} (all ints) from the same bare `show interfaces`
    output parse_interfaces_rates already reads (confirmed live, same
    per-port blocks - no extra SSH round trip needed to also get this).
    Each direction's individually-rare counters (runts/giants/CRC/overrun
    on input, collisions/throttles on output) are summed into one "errors"
    number per direction - trending a single number per direction is more
    useful than four columns that are almost always zero - while discards
    are kept separate since they're a different signal (queue/congestion
    drops, not corrupted frames)."""
    out = {}
    starts = list(_IFACE_BLOCK_START_RE.finditer(text))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[m.start():end]
        iftype, num = m.group(1), m.group(2)
        port = f"{_RATE_PREFIX[iftype]} {num}"

        runts_m = _INPUT_RUNTS_RE.search(block)
        crc_m = _INPUT_CRC_RE.search(block)
        output_m = _OUTPUT_STATS_RE.search(block)
        if not (runts_m or crc_m or output_m):
            continue

        runts, giants = (int(x) for x in runts_m.groups()) if runts_m else (0, 0)
        crc, overrun, in_discarded = (int(x) for x in crc_m.groups()) if crc_m else (0, 0, 0)
        throttles, out_discarded, collisions, _wreddrops = (int(x) for x in output_m.groups()) if output_m else (0, 0, 0, 0)

        out[port] = {
            "input_errors": runts + giants + crc + overrun,
            "input_discards": in_discarded,
            "output_errors": collisions,
            "output_discards": out_discarded + throttles,
        }
    return out


def parse_transceiver(text):
    """Returns None if not present, else dict with diagnostics for one port.

    Verified live against three real cases: a genuine optical SFP+ (type
    "10GBASE-LR", full DOM diagnostics with real temp/voltage/power
    readings), an Active Optical Cable ("10GBASE-SR-AOC-5M", "DOM is not
    supported" - no readings), and a copper DAC ("10GBASE-CU1M", also "DOM
    is not supported"). `dom_supported` is the authoritative signal for
    whether the light-reading fields below are meaningful - AOC/DAC always
    report them as None even when `present` is True.
    """
    if "cannot be read" in text or "not present" in text:
        return {"present": False}
    if "SFP+ is present" not in text and "QSFP" not in text and "is present" not in text:
        # Unrecognized / empty output - treat as not present rather than guessing.
        if not re.search(r"Temperature\s*=", text):
            return {"present": False}

    out = {"present": True, "dom_supported": "DOM is not supported" not in text}

    def find_float(label, unit):
        m = re.search(re.escape(label) + r"\s*=\s*(-?[\d.]+)" + re.escape(unit), text)
        return float(m.group(1)) if m else None

    out["temperature_c"] = find_float("Temperature", "C")
    out["voltage_v"] = find_float("Voltage", "V")
    out["tx_bias_ma"] = find_float("Tx Bias Current", "mA")
    out["tx_power_dbm"] = find_float("Tx Power", "dBm")
    out["rx_power_dbm"] = find_float("Rx Power", "dBm")

    for flag in (
        "Rx LOS state",
        "Tx Fault state",
        "Temperature High Alarm Flag",
        "Temperature Low Alarm Flag",
        "Rx Power High Alarm Flag",
        "Rx Power Low Alarm Flag",
        "Tx Power High Alarm Flag",
        "Tx Power Low Alarm Flag",
    ):
        m = re.search(re.escape(flag) + r"\s*=\s*(True|False)", text)
        if m:
            out[flag.lower().replace(" ", "_")] = m.group(1) == "True"

    # Dell prints the transceiver type designation (e.g. "10GBASE-LR",
    # "10GBASE-SR-AOC-5M", "10GBASE-CU1M", "40GBASE-SR4") as the last
    # non-blank line of the command's output.
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out["type"] = lines[-1] if lines else None

    return out


_DESC_ROW_RE = re.compile(
    r"^(TenGigabitEthernet|fortyGigE)\s+(\S+)\s+(?:YES|NO)\s+(admin down|up|down)\s+(up|down|not present)\s*(.*)$"
)
_DESC_PREFIX = {"TenGigabitEthernet": "Te", "fortyGigE": "Fo"}


def parse_interfaces_description(text):
    """Returns [{'port','admin_status','protocol_status','description'}] for
    Te/Fo interfaces from `show interfaces description` - the only command
    on this switch that distinguishes administratively-shutdown ports
    ("admin down") from ports that are enabled but simply have no link
    ("up" / "down"). `show interfaces status` alone can't tell these apart -
    both show as plain "Down"."""
    results = []
    for line in text.splitlines():
        m = _DESC_ROW_RE.match(line)
        if not m:
            continue
        iftype, num, admin_status, protocol_status, description = m.groups()
        results.append(
            {
                "port": f"{_DESC_PREFIX[iftype]} {num}",
                "admin_status": admin_status,
                "protocol_status": protocol_status,
                "description": description.strip(),
            }
        )
    return results


def parse_alarms(text):
    """Returns {'minor': [...], 'major': [...]} from `show alarms` - the
    switch's own authoritative current alarm list. Each entry (when alarms
    are present) is {'text', 'duration'}, split using the header's "Alarm
    Type"/"Duration" column positions. Verified live against the real
    "no alarms" case on this switch; the populated-alarm line format is
    inferred from that same header layout since no real alarm was
    available to capture without forcing one on production hardware -
    if a real populated example ever surfaces looking different, adjust
    this parser against it rather than assuming it's right."""
    lines = text.splitlines()
    duration_col = None
    for line in lines:
        if line.strip().startswith("Alarm Type"):
            idx = line.find("Duration")
            if idx != -1:
                duration_col = idx
            break

    def section(name, stop_names):
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == name)
        except StopIteration:
            return []
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].strip() in stop_names:
                end = i
                break
        entries = []
        for line in lines[start + 1 : end]:
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("no minor") or stripped.lower().startswith("no major"):
                continue
            if duration_col is not None and len(line) > duration_col:
                text_part = line[:duration_col].strip()
                duration_part = line[duration_col:].strip()
            else:
                text_part, duration_part = stripped, ""
            if text_part:
                entries.append({"text": text_part, "duration": duration_part or None})
        return entries

    return {
        "minor": section("Minor Alarms", {"Major Alarms"}),
        "major": section("Major Alarms", set()),
    }


def parse_lldp_neighbors_detail(text):
    """Parses `show lldp neighbors detail` - one dict per neighbor entry:
    {'local_port', 'remote_chassis_id', 'remote_port_id',
    'remote_port_description', 'remote_system_desc'}. The plain `show lldp
    neighbors` table is deliberately not used for topology - it only gives
    the remote port as a bare number for non-Dell neighbors (e.g. a Junos
    peer's ifIndex), where `detail`'s "Remote Port Description" gives the
    peer's actual interface name instead (confirmed live against the real
    Dell<->Juniper LACP uplink). `remote_port_description`/
    `remote_system_desc` are None when the neighbor didn't advertise them
    (e.g. a bare NIC without LLDP-MED)."""
    results = []
    for chunk in re.split(r"(?=Remote Chassis ID Subtype:)", text)[1:]:
        chassis_m = re.search(r"Remote Chassis ID:\s*(\S+)", chunk)
        local_m = re.search(r"Local Port ID:\s*(.+)", chunk)
        if not (chassis_m and local_m):
            continue
        port_id_m = re.search(r"Remote Port ID:\s*(\S+)", chunk)
        desc_m = re.search(r"Remote Port Description:\s*(.+)", chunk)
        sysdesc_m = re.search(r"Remote System Desc:\s*(.+?)(?=\s*Existing System Capabilities:)", chunk, re.DOTALL)
        results.append({
            "local_port": _short_port_name(local_m.group(1).strip()),
            "remote_chassis_id": chassis_m.group(1).strip(),
            "remote_port_id": port_id_m.group(1).strip() if port_id_m else None,
            "remote_port_description": desc_m.group(1).strip() if desc_m else None,
            "remote_system_desc": re.sub(r"\s+", " ", sysdesc_m.group(1)).strip() if sysdesc_m else None,
        })
    return results


_ARP_ROW_RE = re.compile(
    r"^Internet\s+(?P<ip>\S+)\s+\S+\s+(?P<mac>[0-9a-fA-F:]{17})\s+(?P<interface>\S+(?: \S+)?)\s+", re.MULTILINE
)


def parse_arp(text):
    """Returns a list of {'ip','mac','interface'} from `show arp`."""
    return [
        {"ip": m.group("ip"), "mac": m.group("mac").lower(), "interface": m.group("interface")}
        for m in _ARP_ROW_RE.finditer(text)
    ]


_MAC_TABLE_ROW_RE = re.compile(
    r"^\s*\d+\t(?P<mac>[0-9a-fA-F:]{17})\t(?P<type>\S+)\s*\t(?P<interface>.+?)\t", re.MULTILINE
)


def parse_mac_address_table(text):
    """Returns a list of {'mac','interface'} from `show mac-address-table`
    - every MAC the switch has ever learned on every port, not just the
    ones that happen to speak LLDP. This is a second, independent way to
    discover what's connected to a port: a bridge/CAM table entry exists
    for *any* device that's sent or received a frame, LLDP or not (e.g. an
    unmanaged switch/hub full of hosts that never announce themselves via
    LLDP at all)."""
    return [
        {"mac": m.group("mac").lower(), "interface": m.group("interface").strip()}
        for m in _MAC_TABLE_ROW_RE.finditer(text)
    ]


# Matches both a port-channel's header line (starts with the LACP code
# letter, LAG number, mode/status/uptime columns, then its first member
# port) and a continuation line for its other members (just indentation
# then a port name) - confirmed live: `show interfaces port-channel brief`
# lists every additional member on its own indented line below the header,
# with no per-line repeat of the LAG number.
_PC_LINE_RE = re.compile(
    r"^\s*(?:[A-Z]\s+(?P<num>\d+)\s+\S+\s+\S+\s+\S+\s+)?(?P<port>[A-Za-z]+ \d+/\d+)\s+\([^)]+\)\s*$", re.MULTILINE
)


def parse_port_channel_brief(text):
    """Returns {member_port: 'Po N'} from `show interfaces port-channel
    brief` - which physical ports are actually bundled into which
    port-channel. Dell's LLDP output never reports this (unlike Junos,
    which names the parent `ae` interface directly in `show lldp
    neighbors`), so without this a port-channel whose members happen to
    carry a confirmed link to another known device (e.g. the uplink to a
    second switch) can't be told apart from a port-channel that's just a
    server's LACP-bonded NIC - both would otherwise look like a bare
    physical port with no lag name at all."""
    members_by_pc = {}
    current_pc = None
    for line in text.splitlines():
        m = _PC_LINE_RE.match(line)
        if not m:
            continue
        if m.group("num"):
            current_pc = f"Po {m.group('num')}"
        if current_pc:
            members_by_pc.setdefault(current_pc, []).append(m.group("port"))
    return {port: pc for pc, ports in members_by_pc.items() for port in ports}


# Dell OS9 spells interface types out in full in `show interfaces`
# ("TenGigabitEthernet 1/37") while every other surface in this app - the
# front panel, status poller, sFlow's arithmetic fallback - uses the short
# form ("Te 1/37"). Normalised here so a discovered name and a computed
# one are never two different labels for the same port.
_OS9_TYPE_ABBREV = {
    "TenGigabitEthernet": "Te",
    "fortyGigE": "Fo",
    "GigabitEthernet": "Gi",
    "Port-channel": "Po",
    "ManagementEthernet": "Ma",
    "Vlan": "Vlan",
}


def parse_os9_ifindex(text):
    """{ifindex: short_port_name} from `show interfaces`.

    sFlow identifies interfaces by ifIndex. Physical ports on this platform
    happen to be arithmetic, but port-channels, management and VLAN
    interfaces are not - measured on a real S4048, Te 1/1 is 2097156,
    Port-channel 1 is 1258291712 and ManagementEthernet 1/1 is 9437185,
    three unrelated ranges. So the map is read off the device rather than
    derived.

    Paired by walking the output rather than zipping two regex results:
    a block missing its index line would silently shift every subsequent
    pairing by one, naming every port after it incorrectly.
    """
    mapping = {}
    current = None
    for line in (text or "").splitlines():
        header = re.match(r"^(\S+)\s+(\S+)\s+is\s+(?:up|down)", line)
        if header:
            kind, ident = header.group(1), header.group(2)
            current = f"{_OS9_TYPE_ABBREV.get(kind, kind)} {ident}"
            continue
        m = re.search(r"Interface index is (\d+)", line)
        if m and current:
            mapping[int(m.group(1))] = current
            current = None  # one index per block; don't reuse a stale name
    return mapping
