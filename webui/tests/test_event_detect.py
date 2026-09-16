"""Syslog detectors: the lines the switches actually send become the right
event, at the site's severity, and the same line delivered twice (fast
path, then the Loki poll) is one transition. Pure - a fake store.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import event_detect as det  # noqa: E402
from eventstore import signature_for  # noqa: E402


class _Store:
    def __init__(self):
        self.open = {}
        self.log = []

    def raise_event(self, kind, severity, device_id, device, subject, title, detail=None, labels=None, source="syslog", signal_at=None):
        sig = signature_for(kind, device_id or device, subject)
        if sig in self.open:
            return self.open[sig], False
        ev = {"id": len(self.log) + 1, "signature": sig, "kind": kind, "severity": severity, "device_id": device_id,
              "device": device, "subject": subject, "title": title, "detail": detail, "labels": labels, "source": source}
        self.open[sig] = ev
        self.log.append(("raise", kind, subject, severity))
        return ev, True

    def resolve(self, sig, by="syslog", detail=None):
        ev = self.open.pop(sig, None)
        if ev:
            self.log.append(("resolve", ev["kind"], ev["subject"], by))
        return ev


class _Settings:
    def __init__(self, **over):
        self.over = over

    def severity_for(self, kind):
        import event_catalog
        return self.over.get(kind, event_catalog.BY_KIND[kind]["default"])

    def params_for(self, kind):
        import event_catalog
        return dict(event_catalog.BY_KIND[kind].get("params") or {})


class _Ports:
    def __init__(self, **over):
        self.over = over

    def severity_for(self, device_id, port, fallback):
        return self.over.get(port, fallback)


def _dev(e):
    return ("ex3300", "EX3300") if e.get("device_host") == "192.168.4.1" else ("s4048", "S4048")


def _detector(settings=None, ports=None):
    store = _Store()
    return store, det.SyslogDetector(store, settings or _Settings(), ports or _Ports())


def _line(message, ts=1, **fields):
    return {"message": message, "detail": message, "_timestamp_ns": ts, "device_host": "192.168.4.1", **fields}


# --- the real EX3300 lines from the missed link test ---------------------------

DOWN = "mib2d[1344]: SNMP_TRAP_LINK_DOWN: ifIndex 587, ifAdminStatus up(1), ifOperStatus down(2), ifName ge-0/0/5"
UP = "mib2d[1344]: SNMP_TRAP_LINK_UP: ifIndex 587, ifAdminStatus up(1), ifOperStatus up(1), ifName ge-0/0/5"


def test_a_link_down_on_any_port_is_an_event_and_the_up_resolves_it():
    store, d = _detector()
    assert d.process([_line(DOWN, 1, link_event=True, interface="ge-0/0/5", link_state="down", vendor="junos", event_category="interface")], _dev) == 1
    assert store.log[-1] == ("raise", "port.link_down", "ge-0/0/5", "warning")
    assert d.process([_line(UP, 2, link_event=True, interface="ge-0/0/5", link_state="up", vendor="junos", event_category="interface")], _dev) == 1
    assert store.log[-1] == ("resolve", "port.link_down", "ge-0/0/5", "syslog")


def test_the_logical_unit_line_is_not_a_second_event():
    store, d = _detector()
    d.process([_line(UP.replace("ge-0/0/5", "ge-0/0/5.0"), 1, link_event=True, interface="ge-0/0/5.0", link_state="up", vendor="junos")], _dev)
    assert store.log == []


def test_port_severity_overrides_and_ignore():
    store, d = _detector(ports=_Ports(**{"ge-0/0/5": "critical", "ge-0/0/6": "ignore"}))
    ev = lambda port, ts: _line(DOWN.replace("ge-0/0/5", port), ts, link_event=True, interface=port, link_state="down", vendor="junos")
    d.process([ev("ge-0/0/5", 1), ev("ge-0/0/6", 2)], _dev)
    assert store.log == [("raise", "port.link_down", "ge-0/0/5", "critical")] and d.ignored == 1


def test_the_same_line_from_two_paths_is_one_transition():
    store, d = _detector()
    line = _line(DOWN, 5, link_event=True, interface="ge-0/0/5", link_state="down", vendor="junos")
    assert d.process([line], _dev) == 1
    assert d.process([line], _dev) == 0, "the Loki poll re-delivers it: cursor says done"
    assert d.cursor_ns == 5


# --- LAG membership: the real S4048 lines from the 19:51 unplug ----------

UNGROUP = "CP %LACP-5-PORT-UNGROUPED: PortChannel-003-Ungrouped: Interface Te 1/41 exited port-channel 3."
GROUP = "CP %LACP-5-PORT-GROUPED: PortChannel-003-Grouped: Interface Te 1/41 joined port-channel 3."


def test_a_lag_member_leaving_is_an_event_and_rejoining_resolves_it():
    """Confirmed live: unplugging Te 1/41 (a port-channel 3 member) made
    the S4048 log *only* this - no OSTATE_DN, no link-state line at all -
    so without this the syslog path saw nothing and the port's outage
    waited on the SSH poll."""
    store, d = _detector()
    dell = dict(device_host="S4048", event_category="interface", facility="LACP", mnemonic="PORT-UNGROUPED", interface="Te 1/41")
    assert d.process([_line(UNGROUP, 1, **dell)], _dev) == 1
    assert store.log[-1] == ("raise", "port.lag_member_lost", "Te 1/41 in port-channel 3", "warning")

    dell["mnemonic"] = "PORT-GROUPED"
    assert d.process([_line(GROUP, 2, **dell)], _dev) == 1
    assert store.log[-1] == ("resolve", "port.lag_member_lost", "Te 1/41 in port-channel 3", "syslog")


def test_a_lag_line_is_not_also_a_link_down():
    """One unplug, one syslog event. The link itself is the SSH poll's to
    report - two events from one line would be the duplication this whole
    design is meant to avoid."""
    store, d = _detector()
    d.process([_line(UNGROUP, 1, device_host="S4048", event_category="interface", facility="LACP",
                     mnemonic="PORT-UNGROUPED", interface="Te 1/41")], _dev)
    assert [k for _, k, _, _ in store.log] == ["port.lag_member_lost"]


def test_three_downs_in_five_minutes_is_flapping():
    store, d = _detector()
    for i in range(3):
        d.process([_line(DOWN, 10 + i, link_event=True, interface="ge-0/0/5", link_state="down", vendor="junos")], _dev)
        store.open.clear()   # each down was resolved by an up in between
    assert ("raise", "port.flapping", "ge-0/0/5", "warning") in store.log


# --- hardware (the real captured Dell wording) ------------------------------------

def test_psu_down_and_up():
    store, d = _detector()
    dell = dict(device_host="S4048", event_category="hardware", facility="CHMGR")
    d.process([_line("%CHMGR-0-PS_DOWN: Major alarm: Power supply 2 in unit 1 is down", 1, detail="Major alarm: Power supply 2 in unit 1 is down", **dell)], _dev)
    assert store.log[-1] == ("raise", "env.psu", "PSU 2 (unit 1)", "critical")
    d.process([_line("%CHMGR-0-PS_UP: Power supply 2 in unit 1 is up", 2, detail="Power supply 2 in unit 1 is up", **dell)], _dev)
    assert store.log[-1] == ("resolve", "env.psu", "PSU 2 (unit 1)", "syslog")


def test_fan_severity_follows_the_catalogue_setting():
    store, d = _detector(settings=_Settings(**{"env.fan": "warning"}))
    d.process([_line("%CHMGR-2-FAN_BAD: Minor Alarm : Fan tray 1 in unit 1 is down", 1, detail="Minor Alarm : Fan tray 1 in unit 1 is down",
                     device_host="S4048", event_category="hardware")], _dev)
    assert store.log[-1] == ("raise", "env.fan", "Fan tray 1 (unit 1)", "warning")


# --- everything else --------------------------------------------------------------

def test_compute_device_and_protocol_patterns():
    store, d = _detector()
    lines = [
        _line("%SYS-2-MEM: DDR ECC uncorrectable error on unit 1", 1, device_host="S4048"),
        _line("%SYS-5-RESTART: System restarted -- Cold Start", 2, device_host="S4048"),
        _line("mgd[1]: UI_COMMIT_COMPLETED: commit complete", 3),
        _line("%STP-5-TOPOLOGY_CHANGE: Topology change on Vlan 10", 4, device_host="S4048", event_category="spanning-tree", facility="STP"),
        _line("%BGP-5-ADJCHANGE: neighbor 10.0.0.1 Down - hold timer expired", 5, device_host="S4048", event_category="routing"),
        _line("%BGP-5-ADJCHANGE: neighbor 10.0.0.1 Up", 6, device_host="S4048", event_category="routing"),
    ]
    d.process(lines, _dev)
    kinds = [(k, s) for _, k, s, _ in store.log]
    assert ("compute.memory_error", "memory") in kinds and ("device.rebooted", "restart") in kinds
    assert ("device.config_changed", "config") in kinds and ("protocol.stp_topology_change", "stp") in kinds
    assert store.log[-2] == ("raise", "protocol.neighbor_lost", "10.0.0.1", "critical")
    assert store.log[-1] == ("resolve", "protocol.neighbor_lost", "10.0.0.1", "syslog")


# --- optics, from the S4048's own captured lines -------------------------

def test_optic_removed_and_non_qualified_are_their_own_events():
    """Both lines captured live from this S4048."""
    store, d = _detector()
    dell = dict(device_host="S4048", event_category="interface", facility="IFAGT")
    d.process([_line("%IFAGT-5-REMOVED_OPTICS_PLUS: Optics SFP+ removed in slot 1 port 47", 1, mnemonic="REMOVED_OPTICS_PLUS", **dell)], _dev)
    assert store.log[-1] == ("raise", "optic.removed", "slot 1 port 47", "warning")

    d.process([_line("%IFAGT-5-UNSUP_OPTICS: Non-qualified optics in slot 1 port 47", 2, mnemonic="UNSUP_OPTICS", **dell)], _dev)
    assert store.log[-1] == ("raise", "optic.unsupported", "slot 1 port 47", "warning")


def test_putting_an_optic_back_resolves_the_removal():
    store, d = _detector()
    dell = dict(device_host="S4048", event_category="interface", facility="IFAGT")
    d.process([_line("%IFAGT-5-REMOVED_OPTICS_PLUS: Optics SFP+ removed in slot 1 port 47", 1, mnemonic="REMOVED_OPTICS_PLUS", **dell)], _dev)
    d.process([_line("%IFAGT-5-INSERTED_OPTICS_PLUS: Optics SFP+ inserted in slot 1 port 47", 2, mnemonic="INSERTED_OPTICS_PLUS", **dell)], _dev)
    assert store.log[-1] == ("resolve", "optic.removed", "slot 1 port 47", "syslog")


def test_a_command_someone_typed_is_never_a_fault():
    """Junos echoes every CLI line to syslog. Confirmed live: `show lldp
    neighbors` came through as a line the neighbour patterns matched, so
    typing a command with the wrong word in it raised critical events."""
    store, d = _detector()
    typed = [
        _line("mgd[49252]: UI_CMDLINE_READ_LINE: User 'root', command 'show lldp neighbors '", 1, facility="MGD", mnemonic="UI_CMDLINE_READ_LINE"),
        _line("mgd[49252]: UI_CMDLINE_READ_LINE: User 'root', command 'show interfaces | match down '", 2, facility="MGD", mnemonic="UI_CMDLINE_READ_LINE"),
        _line("mgd[49252]: UI_CMDLINE_READ_LINE: User 'root', command 'show system memory ecc error '", 3, facility="MGD", mnemonic="UI_CMDLINE_READ_LINE"),
    ]
    assert d.process(typed, _dev) == 0
    assert store.log == []


def test_a_firewalls_packet_log_is_never_a_fault():
    """OPNsense logs one line per matched packet - two thirds of what it
    sends - as bare CSV full of addresses and the word "block". It is
    data about other people's traffic, not about the device."""
    store, d = _detector()
    packets = [
        _line("94,,,ad6b,vtnet1,match,block,in,4,0x0,,62,13171,0,DF,6,tcp,60,192.168.3.104,3.0.149.98,53124,443,0,S",
              1, appname="filterlog", facility="local0", device_host="OPNsense"),
        _line("13,,,02f4,vtnet0,match,pass,out,4,0x0,,125,15376,0,DF,17,udp,1278,1.1.1.1,8.8.8.8,53991,443,1258",
              2, appname="filterlog", facility="local0", device_host="OPNsense"),
    ]
    assert d.process(packets, _dev) == 0
    assert store.log == []


def test_an_lldp_neighbour_going_away_is_not_a_routing_adjacency():
    """It goes away because the link went down, which is already the link
    event - counting it too made one unplug two critical events."""
    store, d = _detector()
    d.process([_line("lldpd[1370]: LLDP_NEIGHBOR_DOWN: A neighbor is down on interface xe-0/1/3", 1,
                     facility="LLDPD", mnemonic="LLDP_NEIGHBOR_DOWN", event_category="other")], _dev)
    assert store.log == []


def test_two_routing_neighbours_on_different_ports_are_two_events():
    store, d = _detector()
    base = dict(device_host="S4048", event_category="routing")
    d.process([_line("%OSPF-5-ADJCHG: adjacency down on interface", 1, interface="Te 1/1", **base),
               _line("%OSPF-5-ADJCHG: adjacency down on interface", 2, interface="Te 1/2", **base)], _dev)
    assert [s for _, _, s, _ in store.log] == ["Te 1/1", "Te 1/2"]


def test_user_rules_raise_with_their_own_severity_and_clear():
    store, d = _detector()
    rule = {"id": 9, "name": "Fan noise", "enabled": True, "severity": "info", "facility": "", "mnemonic": "",
            "pattern": "(?i)noisy", "clear_pattern": "(?i)quiet", "per_interface": False}
    d.process([_line("fan is noisy", 1, device_host="S4048")], _dev, rules=[rule])
    assert store.log[-1] == ("raise", "syslog.rule", "Fan noise", "info")
    d.process([_line("fan is quiet", 2, device_host="S4048")], _dev, rules=[rule])
    assert store.log[-1] == ("resolve", "syslog.rule", "Fan noise", "syslog")


def test_selftest_line_is_its_own_kind():
    store, d = _detector()
    d.process([_line("%SWB-4-SWITCHBOARD_SELFTEST: Fast-path self-test nonce=abc", 1, device_host="switchboard", mnemonic="SWITCHBOARD_SELFTEST", facility="SWB")],
              lambda e: ("", "switchboard"))
    assert store.log[-1] == ("raise", "switchboard.selftest", "selftest", "info")
    assert store.open[signature_for("switchboard.selftest", "switchboard", "selftest")]["labels"] == {"nonce": "abc"}


def test_a_detector_error_never_stops_the_batch():
    store, d = _detector()
    bad = {"message": None, "_timestamp_ns": 1, "link_event": True, "interface": 5, "link_state": "down"}
    ok = _line(DOWN, 2, link_event=True, interface="ge-0/0/5", link_state="down", vendor="junos")
    d.process([bad, ok], _dev)
    assert store.log[-1][1] == "port.link_down"
