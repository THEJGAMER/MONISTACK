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

    # Type is `AC`/`DC` when the PSU is up, but confirmed live: a PSU
    # that's down or removed reports Type `UNKNOWN` instead - the row is
    # still there, the device just can't identify it anymore. Matching
    # only AC|DC here silently dropped exactly the PSU row that matters
    # (the failed one) from `out["psus"]`, which meant `s4048_psu_status`
    # just went stale at its last "up" value forever instead of ever
    # reflecting the fault - a real PSU failure on this fleet never
    # triggered S4048PSUDown because of this.
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


def parse_transceiver(text):
    """Returns None if not present, else dict with diagnostics for one port."""
    if "cannot be read" in text or "not present" in text:
        return {"present": False}
    if "SFP+ is present" not in text and "QSFP" not in text and "is present" not in text:
        # Unrecognized / empty output - treat as not present rather than guessing.
        if not re.search(r"Temperature\s*=", text):
            return {"present": False}

    out = {"present": True}

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

    return out
