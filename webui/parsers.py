"""Regex/text parsers for Dell OS9 (S4048-ON) `show` command output."""
import re


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

    for m in re.finditer(
        r"^\s*(\d+)\s+(\d+)\s+(up|down)\s+(AC|DC)\s+(up|down)\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+",
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
