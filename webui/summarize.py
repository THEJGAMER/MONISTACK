"""Best-effort one/two-line human summaries for command output.

Keyed by (category_id, command_id) so the same literal command text used
in two different menu spots can't accidentally get mismatched summarizers.
Every summarizer takes the raw output string and returns a short string,
or None if it has nothing useful to add (the raw output is always shown
regardless - this is a supplement, never a replacement).

A summarizer must never raise - anything that goes wrong here should just
mean "no summary", not a broken response to the user's click.
"""
import re

import parsers

SUMMARIZERS = {}


def summarizer(category_id, command_id):
    def deco(fn):
        SUMMARIZERS[(category_id, command_id)] = fn
        return fn

    return deco


def summarize(category_id, command_id, output):
    fn = SUMMARIZERS.get((category_id, command_id))
    if fn is None:
        return None
    try:
        return fn(output)
    except Exception:
        return None


def _count(pattern, text, flags=0):
    return len(re.findall(pattern, text, flags))


# --- System --------------------------------------------------------------


@summarizer("system", "version")
def _version(out):
    m = re.search(r"Application Software Version:\s*([^\n]+)", out)
    u = re.search(r"uptime is (.+)", out)
    parts = []
    if m:
        parts.append(f"OS {m.group(1).strip()}")
    if u:
        parts.append(f"uptime {u.group(1).strip()}")
    return " | ".join(parts) or None


@summarizer("system", "system_info")
def _system_info(out):
    status = re.search(r"Status\s*:\s*(\S+)", out)
    uptime = re.search(r"Up Time\s*:\s*(.+)", out)
    parts = []
    if status:
        parts.append(f"status {status.group(1)}")
    if uptime:
        parts.append(f"up {uptime.group(1).strip()}")
    return ", ".join(parts) or None


@summarizer("system", "cpu")
def _cpu(out):
    d = parsers.parse_cpu(out)
    overall = d.get("overall")
    if not overall:
        return None
    return f"CPU overall: 5sec={overall.get('5sec')}% 1min={overall.get('1min')}% 5min={overall.get('5min')}%"


@summarizer("system", "memory")
def _memory(out):
    d = parsers.parse_memory(out)
    if not d.get("total"):
        return None
    pct = 100 * d["used"] / d["total"]
    return f"Memory: {pct:.2f}% used ({d['used']:,} / {d['total']:,} bytes)"


@summarizer("system", "proc_memory")
def _proc_memory(out):
    m = re.search(r"Total:\s*(\d+),\s*MaxUsed:\s*(\d+),\s*CurrentUsed:\s*(\d+),\s*CurrentFree:\s*(\d+)", out)
    if not m:
        return None
    total, max_used, cur_used, cur_free = (int(g) for g in m.groups())
    return f"Memory: {100 * cur_used / total:.1f}% used now ({cur_used:,}/{total:,}), peak {100 * max_used / total:.1f}%"


@summarizer("system", "environment")
def _environment(out):
    d = parsers.parse_environment(out)
    fans = d.get("fans", [])
    psus = d.get("psus", [])
    fans_down = sum(1 for f in fans if f["fan1_status"] != "up" or f["fan2_status"] != "up")
    psus_down = sum(1 for p in psus if p["status"] != "up")
    temp = d.get("units", [{}])[0].get("temp_c") if d.get("units") else None
    parts = [f"{len(fans)} fan trays ({fans_down} degraded)", f"{len(psus)} PSUs ({psus_down} down)"]
    if temp is not None:
        parts.append(f"chassis {temp}C")
    return " | ".join(parts)


# --- Interfaces ------------------------------------------------------------


@summarizer("interfaces", "if_status")
def _if_status(out):
    rows = parsers.parse_interfaces_status(out)
    if not rows:
        return None
    up = sum(1 for r in rows if r["status"] == "Up")
    return f"{len(rows)} ports: {up} up, {len(rows) - up} down"


@summarizer("interfaces", "if_desc")
def _if_desc(out):
    lines = [l for l in out.splitlines() if re.match(r"\S+ \S+/\S+", l) or re.match(r"^(Vlan|Port-channel)", l)]
    up = _count(r"^\S.*\sup\s+up(\s|$)", out, re.MULTILINE)
    total = len(lines)
    if total == 0:
        return None
    return f"{total} interfaces listed, {up} fully up"


@summarizer("interfaces", "if_brief")
def _if_brief(out):
    lines = out.splitlines()[1:]
    total = sum(1 for l in lines if l.strip())
    up = _count(r"\sup\s+up\s*$", out, re.MULTILINE)
    assigned = _count(r"\s(?!unassigned)\d+\.\d+\.\d+\.\d+\s", out)
    if total == 0:
        return None
    return f"{total} interfaces, {up} up/up, {assigned} with an IP assigned"


@summarizer("interfaces", "switchport")
def _switchport(out):
    n = _count(r"^Name:", out, re.MULTILINE)
    if n == 0:
        return None
    return f"{n} switchports listed"


def _iface_counters_summary(out):
    m_in = re.search(r"Input Statistics:\s*\n\s*(\d+) packets, (\d+) bytes", out)
    m_out = re.search(r"Output Statistics:\s*\n\s*(\d+) packets, (\d+) bytes", out)
    m_rate = re.search(r"Input ([\d.]+ \w+/sec),\s*([\d,]+) packets/sec.*\n\s*Output ([\d.]+ \w+/sec)", out)
    m_errs = re.search(r"(\d+) CRC, (\d+) overrun, (\d+) discarded", out)
    parts = []
    if m_in and m_out:
        parts.append(f"In {int(m_in.group(1)):,} pkts / Out {int(m_out.group(1)):,} pkts")
    if m_rate:
        parts.append(f"rate: {m_rate.group(1)} in, {m_rate.group(3)} out")
    if m_errs and any(int(g) for g in m_errs.groups()):
        parts.append(f"errors: {m_errs.group(1)} CRC, {m_errs.group(2)} overrun, {m_errs.group(3)} discarded")
    return " | ".join(parts) or None


summarize_interfaces_if_detail = summarizer("interfaces", "if_detail")(_iface_counters_summary)
summarize_port_channels_pc_detail = summarizer("port_channels", "pc_detail")(_iface_counters_summary)


@summarizer("interfaces", "if_transceiver")
def _if_transceiver(out):
    d = parsers.parse_transceiver(out)
    if not d.get("present"):
        return "No transceiver present"
    parts = [
        f"temp {d.get('temperature_c')}C",
        f"voltage {d.get('voltage_v')}V",
        f"tx {d.get('tx_power_dbm')}dBm",
        f"rx {d.get('rx_power_dbm')}dBm",
    ]
    alarms = [k for k, v in d.items() if k.endswith(("alarm_flag", "los_state", "fault_state")) and v is True]
    if alarms:
        parts.append(f"ALARMS: {', '.join(alarms)}")
    return " | ".join(parts)


# --- Port channels -----------------------------------------------------


@summarizer("port_channels", "pc_brief")
def _pc_brief(out):
    lags = re.findall(r"^\S?\s*(\d+)\s+\S+\s+(up|down)\s", out, re.MULTILINE)
    if not lags:
        return None
    up = sum(1 for _, status in lags if status == "up")
    return f"{len(lags)} port-channels: {up} up, {len(lags) - up} down"


@summarizer("port_channels", "lacp_detail")
def _lacp_detail(out):
    m = re.search(r"Port-channel (\d+) admin (up|down), oper (up|down), mode (\S+)", out)
    members = _count(r"^Port \S+ \S+/\S+ is enabled", out, re.MULTILINE)
    if not m:
        return None
    return f"Po{m.group(1)}: admin {m.group(2)}, oper {m.group(3)}, mode {m.group(4)}, {members} member port(s)"


# --- Layer 2 -------------------------------------------------------------


@summarizer("l2", "vlan")
def _vlan(out):
    rows = re.findall(r"^\S?\s+(\d+)\s+(Active|Inactive)", out, re.MULTILINE)
    if not rows:
        return None
    active = sum(1 for _, s in rows if s == "Active")
    return f"{len(rows)} VLANs ({active} active)"


@summarizer("l2", "mac")
def _mac(out):
    n = _count(r"^\s*\d+\t[0-9a-f:]{17}\t", out, re.MULTILINE)
    if n == 0:
        return "0 MAC addresses in table"
    return f"{n} MAC addresses learned"


@summarizer("l2", "stp")
def _stp(out):
    return out.strip().splitlines()[0] if out.strip() else None


# --- Layer 3 -------------------------------------------------------------


@summarizer("l3", "route")
def _route(out):
    codes = re.findall(r"^\s*\*?([A-Z]{1,2})\s+\d+\.\d+\.\d+\.\d+", out, re.MULTILINE)
    if not codes:
        return None
    from collections import Counter

    counts = Counter(codes)
    return f"{len(codes)} routes: " + ", ".join(f"{v} {k}" for k, v in counts.most_common())


@summarizer("l3", "route_static")
def _route_static(out):
    n = _count(r"^\s*\S?\s*S\s", out, re.MULTILINE)
    return f"{n} static route(s)"


@summarizer("l3", "route_summary")
def _route_summary(out):
    m = re.search(r"Total (\d+) active route\(s\)", out)
    return f"{m.group(1)} active routes total" if m else None


@summarizer("l3", "protocols")
def _protocols(out):
    m = re.search(r'Routing Protocol is "([^"]+)"', out)
    rid = re.search(r"Router ID is (\S+)", out)
    parts = []
    if m:
        parts.append(m.group(1))
    if rid:
        parts.append(f"router-id {rid.group(1)}")
    return " | ".join(parts) or None


@summarizer("l3", "arp")
def _arp(out):
    n = _count(r"^Internet\s", out, re.MULTILINE)
    return f"{n} ARP entries" if n else None


# --- OSPF ------------------------------------------------------------------


@summarizer("ospf", "ospf_process")
def _ospf_process(out):
    m = re.search(r"Routing Process ospf (\d+) with ID (\S+)", out)
    areas = set(re.findall(r"Area (\S+)", out))
    if not m:
        return None
    parts = [f"process {m.group(1)}, router-id {m.group(2)}"]
    if areas:
        parts.append(f"{len(areas)} area(s)")
    return " | ".join(parts)


@summarizer("ospf", "ospf_neighbor")
def _ospf_neighbor(out):
    states = re.findall(r"\s(FULL|2WAY|INIT|DOWN|EXSTART|EXCHANGE|LOADING)\S*", out)
    if not states:
        return "0 OSPF neighbors"
    from collections import Counter

    counts = Counter(states)
    return f"{len(states)} neighbor(s): " + ", ".join(f"{v} {k}" for k, v in counts.most_common())


@summarizer("ospf", "ospf_interface")
def _ospf_interface(out):
    n = _count(r"^\S+ .* is up, line protocol is up", out, re.MULTILINE)
    return f"{n} OSPF-enabled interface(s) up" if n else None


@summarizer("ospf", "ospf_database")
def _ospf_database(out):
    from collections import Counter

    current = None
    counts = Counter()
    for line in out.splitlines():
        header = re.match(r"\s*(Router|Network|Summary Net|Type-5 AS External)\b", line)
        if header:
            current = header.group(1)
            continue
        if current and re.match(r"^\d+\.\d+\.\d+\.\d+\s", line):
            counts[current] += 1
    return ", ".join(f"{v} {k}" for k, v in counts.items()) if counts else None


# --- Neighbors / Logging ---------------------------------------------------


@summarizer("neighbors", "lldp")
def _lldp(out):
    n = _count(r"^\s*\S+ \d+/\d+\s+\S", out, re.MULTILINE)
    return f"{n} LLDP neighbor(s)" if n else "0 LLDP neighbors"


@summarizer("logs", "logbuf")
def _logbuf(out):
    m = re.search(r"(\d+) Messages Logged", out)
    lines = [l for l in out.splitlines() if re.match(r"^\w{3} +\d+ ", l)]
    parts = []
    if m:
        parts.append(f"{m.group(1)} messages in buffer")
    if lines:
        parts.append(f"showing {len(lines)} most recent")
    return " | ".join(parts) or None


# --- Diagnostics -------------------------------------------------------


@summarizer("diagnostics", "ntp_status")
def _ntp_status(out):
    synced = "synchronized" in out.split(",")[0]
    m = re.search(r"stratum (\d+)", out)
    return f"{'synchronized' if synced else 'NOT synchronized'}" + (f", stratum {m.group(1)}" if m else "")


@summarizer("diagnostics", "ntp_assoc")
def _ntp_assoc(out):
    n = _count(r"^\*?\s*\d+\.\d+\.\d+\.\d+", out, re.MULTILINE)
    return f"{n} NTP peer(s)" if n else None


@summarizer("diagnostics", "cam_usage")
def _cam_usage(out):
    rows = re.findall(r"\|\s*([\w-]+(?: [\w-]+)*)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$", out, re.MULTILINE)
    best = None
    for name, total, used, _avail in rows:
        total, used = int(total), int(used)
        if total > 0:
            pct = 100 * used / total
            if best is None or pct > best[1]:
                best = (name.strip(), pct)
    if best is None:
        return None
    return f"highest utilization: {best[0]} at {best[1]:.1f}%"


@summarizer("diagnostics", "dhcp_snooping")
def _dhcp_snooping(out):
    m = re.search(r"Total number of Entries in the table\s*:\s*(\d+)", out)
    return f"{m.group(1)} DHCP snooping binding(s)" if m else None


@summarizer("diagnostics", "redundancy")
def _redundancy(out):
    role = re.search(r"Stack-unit Redundancy Role:\s*(\S+)", out)
    state = re.search(r"Stack-unit State:\s*(\S+)", out)
    peer = re.search(r"Link to Peer:\s*(\S+)", out)
    parts = []
    if role:
        parts.append(f"role {role.group(1)}")
    if state:
        parts.append(f"state {state.group(1)}")
    if peer:
        parts.append(f"peer link {peer.group(1)}")
    return ", ".join(parts) or None


@summarizer("diagnostics", "vrrp")
def _vrrp(out):
    if "No active VRRP group" in out:
        return "No active VRRP groups"
    n = _count(r"^\s*Interface\s", out, re.MULTILINE)
    return f"{n} VRRP group(s)" if n else out.strip().splitlines()[0]


@summarizer("diagnostics", "users")
def _users(out):
    n = _count(r"^\s*\*?\d+\s+vty", out, re.MULTILINE)
    return f"{n} active vty session(s)" if n else None
