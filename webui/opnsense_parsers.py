"""Regex/text parsers for OPNsense (FreeBSD shell) command output.

Kept in its own file, same convention as junos_parsers.py - a third
platform's output format has nothing in common with the first two. Every
sample below was captured live from a real OPNsense 26.1 box over SSH
(console menu -> option 8 "Shell"), not guessed from FreeBSD/OPNsense
documentation.
"""
import re

_STATUS_RE = re.compile(r"^\tstatus: (?P<status>\S.*)$", re.MULTILINE)


def parse_ifconfig(text):
    """Returns a list of {'interface','description','inet','status'} from
    `ifconfig -a`. `status` is the interface's own reported carrier state
    ("active"/"no carrier"/absent for loopback & virtual interfaces, which
    don't report one at all - reported as None, not fabricated as down)."""
    # Split into per-interface blocks on lines that start a new interface
    # (no leading whitespace, ends in "flags=...") - simpler and more
    # robust than trying to regex the whole multi-line block in one shot,
    # since which optional lines appear (description/ether/inet/status)
    # varies per interface.
    blocks = re.split(r"\n(?=\S)", text.strip())
    out = []
    for block in blocks:
        m = re.match(r"^(?P<iface>\S+?): flags=", block)
        if not m:
            continue
        desc_m = re.search(r"^\tdescription: (.+)$", block, re.MULTILINE)
        inet_m = re.search(r"^\tinet (\S+)", block, re.MULTILINE)
        status_m = _STATUS_RE.search(block)
        out.append({
            "interface": m.group("iface"),
            "description": desc_m.group(1).strip() if desc_m else None,
            "inet": inet_m.group(1) if inet_m else None,
            "status": status_m.group("status").strip() if status_m else None,
        })
    return out


def parse_uptime(text):
    """Returns {'load_1','load_5','load_15'} (floats) from `uptime`."""
    m = re.search(r"load averages?:\s*([\d.]+),?\s*([\d.]+),?\s*([\d.]+)", text)
    if not m:
        return {}
    return {"load_1": float(m.group(1)), "load_5": float(m.group(2)), "load_15": float(m.group(3))}


def parse_top(text):
    """Returns {'cpu_pct', 'mem_active_mb', 'mem_wired_mb', 'mem_free_mb'}
    from `top -b -d 1` (FreeBSD top's batch mode - `-bn1`, the Linux
    spelling, isn't valid here). `cpu_pct` is 100 - idle%, matching how
    junos_parsers derives a single utilization figure from Junos's own
    breakdown."""
    out = {}
    m = re.search(r"CPU:.*?([\d.]+)%\s*idle", text)
    if m:
        out["cpu_pct"] = round(100 - float(m.group(1)), 1)
    mem_m = re.search(
        r"Mem:\s*(?:([\d.]+)([MG]) Active,?\s*)?(?:([\d.]+)([MG]) Inact,?\s*)?"
        r"(?:([\d.]+)([MG]) Laundry,?\s*)?(?:([\d.]+)([MG]) Wired,?\s*)?"
        r"(?:([\d.]+)([MG]) Buf,?\s*)?(?:([\d.]+)([MG]) Free)?",
        text,
    )
    if mem_m:
        def _mb(value, unit):
            if value is None:
                return 0
            n = float(value)
            return n * 1024 if unit == "G" else n
        vals = mem_m.groups()
        active = _mb(vals[0], vals[1])
        wired = _mb(vals[6], vals[7])
        free = _mb(vals[10], vals[11])
        out["mem_active_mb"] = active
        out["mem_wired_mb"] = wired
        out["mem_free_mb"] = free
    return out


def parse_pf_info(text):
    """Returns {'enabled', 'current_states'} from `pfctl -s info`."""
    out = {"enabled": text.strip().startswith("Status: Enabled")}
    m = re.search(r"current entries\s+(\d+)", text)
    if m:
        out["current_states"] = int(m.group(1))
    return out


_ARP_ROW_RE = re.compile(r"^\?\s*\((?P<ip>\S+)\)\s+at\s+(?P<mac>[0-9a-fA-F:]{17})\s+on\s+(?P<interface>\S+)", re.MULTILINE)


def parse_arp(text):
    """Returns a list of {'ip','mac','interface'} from `arp -an`."""
    return [
        {"ip": m.group("ip"), "mac": m.group("mac").lower(), "interface": m.group("interface")}
        for m in _ARP_ROW_RE.finditer(text)
    ]
