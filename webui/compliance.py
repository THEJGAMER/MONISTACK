"""Fleet-wide compliance checks (ROADMAP 3.6). Each check runs a real
allowlisted `show` command against a device (via the `run_command`
callback app.py passes in - same SSH path as everything else, just not
auto-saved to Saved Results the way an interactive run is, since a
compliance sweep across the whole fleet would otherwise flood that table)
and asserts an invariant against the live output.

Checks are intentionally scoped to what's actually derivable from
commands already in commands.py's allowlist and verified live against the
real fleet (one Dell OS9 switch, one Juniper EX3300, one OPNsense
firewall) - a check is marked "skip" rather than guessed/faked for a
platform where no fitting command exists yet, e.g. NTP status has no
Junos command in the tree.
"""
import re

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"


def _finding(check, device, status, detail):
    return {"check": check, "device_id": device.id, "device_name": device.name, "status": status, "detail": detail}


def _check_ntp(device, run_command):
    if device.platform != "os9":
        return _finding("ntp_configured", device, STATUS_SKIP, f"no NTP status command for platform {device.platform!r}")
    try:
        output = run_command(device, "diagnostics", "ntp_status", None)
    except Exception as e:
        return _finding("ntp_configured", device, STATUS_FAIL, f"could not check: {e}")
    if "synchronized" in output.lower():
        return _finding("ntp_configured", device, STATUS_PASS, "clock is synchronized")
    return _finding("ntp_configured", device, STATUS_FAIL, output.splitlines()[0] if output else "no output")


_OS9_VLAN_RE = re.compile(r"^\*?\s*(\d+)\s+\S", re.MULTILINE)
_JUNOS_VLAN_RE = re.compile(r"^\S+\s+(\d+)\s*$", re.MULTILINE)


def _check_vlans(device, run_command, expected_vlans):
    if not expected_vlans:
        return _finding("expected_vlans_present", device, STATUS_SKIP, "no expected VLANs configured")
    if device.platform == "os9":
        category_id, command_id, pattern = "l2", "vlan", _OS9_VLAN_RE
    elif device.platform == "junos":
        category_id, command_id, pattern = "l2", "vlans", _JUNOS_VLAN_RE
    else:
        return _finding("expected_vlans_present", device, STATUS_SKIP, f"no VLAN command for platform {device.platform!r}")
    try:
        output = run_command(device, category_id, command_id, None)
    except Exception as e:
        return _finding("expected_vlans_present", device, STATUS_FAIL, f"could not check: {e}")
    seen = {int(m) for m in pattern.findall(output)}
    missing = sorted(set(expected_vlans) - seen)
    if missing:
        return _finding("expected_vlans_present", device, STATUS_FAIL, f"missing VLAN(s): {missing}")
    return _finding("expected_vlans_present", device, STATUS_PASS, f"all {len(expected_vlans)} expected VLAN(s) present")


def _check_lag_uplinks(device, run_command):
    if device.platform == "os9":
        port_channels = device.valid_values_for("port_channel")
        if not port_channels:
            return _finding("lag_uplinks_healthy", device, STATUS_SKIP, "no port-channels configured")
        bad = []
        checked = 0
        for pc in port_channels:
            try:
                output = run_command(device, "port_channels", "lacp_detail", {"port_channel": pc})
            except Exception as e:
                bad.append(f"Po{pc}: could not check ({e})")
                continue
            first_line = output.splitlines()[0] if output else ""
            # The configurable port-channel range (devices.yaml) is
            # deliberately wider than what's actually cabled, so a manual
            # Console lookup can probe any number - "No such port channel
            # exists" here just means this one was never configured, not
            # that a real uplink is down, so it's skipped rather than
            # counted as a failure (confirmed live: Po4-8 aren't
            # configured on the real S4048, only Po1-3 are).
            if "no such port channel" in first_line.lower() or "% error" in first_line.lower():
                continue
            checked += 1
            if "admin up" not in first_line or "oper up" not in first_line:
                bad.append(f"Po{pc}: {first_line or 'no output'}")
        if bad:
            return _finding("lag_uplinks_healthy", device, STATUS_FAIL, "; ".join(bad))
        if checked == 0:
            return _finding("lag_uplinks_healthy", device, STATUS_SKIP, "no port-channels are actually configured")
        return _finding("lag_uplinks_healthy", device, STATUS_PASS, f"{checked} port-channel(s) up")
    elif device.platform == "junos":
        aggregates = device.valid_values_for("port_channel")
        if not aggregates:
            return _finding("lag_uplinks_healthy", device, STATUS_SKIP, "no aggregate interfaces configured")
        bad = []
        for ae in aggregates:
            try:
                output = run_command(device, "port_channels", "lacp_detail", {"port_channel": ae})
            except Exception as e:
                bad.append(f"{ae}: could not check ({e})")
                continue
            distributing = output.count("Collecting distributing")
            if distributing == 0:
                bad.append(f"{ae}: no members collecting/distributing")
        if bad:
            return _finding("lag_uplinks_healthy", device, STATUS_FAIL, "; ".join(bad))
        return _finding("lag_uplinks_healthy", device, STATUS_PASS, f"{len(aggregates)} aggregate(s) up")
    return _finding("lag_uplinks_healthy", device, STATUS_SKIP, f"no LAG command for platform {device.platform!r}")


def run_checks(devices, expected_vlans, run_command):
    """Runs every check against every device, live. `run_command(device,
    category_id, command_id, params)` is the caller's SSH execution path
    (app.py's `_resolve_command`+session, without the auto-save `/api/run`
    does). Returns a flat list of findings, most useful sorted/grouped by
    the caller."""
    findings = []
    for device in devices:
        findings.append(_check_ntp(device, run_command))
        findings.append(_check_vlans(device, run_command, expected_vlans))
        findings.append(_check_lag_uplinks(device, run_command))
    return findings
