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
