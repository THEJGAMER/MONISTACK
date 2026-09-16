"""Insights: derived facts about the network, computed from what is
already collected.

The producers are what matter, so they are tested individually against
the shapes the real fleet produces - including the shapes that must
produce nothing, which is most of them on a healthy network.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import insights as ins  # noqa: E402


class _DB:
    """Answers each aggregate by the distinctive part of its SQL."""
    def __init__(self, **by_metric):
        self.by_metric = by_metric

    def query(self, sql, params=()):
        for key, rows in self.by_metric.items():
            if key in sql:
                return rows
        return []


class _Settings:
    def params_for(self, kind):
        import event_catalog
        return dict(event_catalog.BY_KIND[kind].get("params") or {})


class _Device:
    def __init__(self, id, name, host="10.0.0.1"):
        self.id, self.name, self.host = id, name, host


# Verbatim from the live S4048.
HEALTHY = {"present": True, "dom_supported": True, "temperature_c": 34.242, "rx_power_dbm": -1.6134,
           "tx_power_dbm": -0.8108, "type": "10GBASE-LR"}
DARK = {"present": True, "dom_supported": True, "temperature_c": 24.801, "rx_power_dbm": -40.0,
        "tx_power_dbm": -40.0, "type": "10GBASE-LR"}


def _iface(port, state="up", optic=None, description="", speed="10000 Mbit", **kw):
    return {"port": port, "port_state": state, "transceiver": optic, "description": description, "speed": speed, **kw}


def _insights(db=None, interfaces=None, syslog_seen=None, devices=None):
    devices = devices or [_Device("s4048", "S4048 (core switch)", "192.168.4.106")]
    status = {"interfaces": interfaces or []}
    return ins.Insights(db or _DB(), None, _Settings(), devices, lambda _id: status, syslog_seen=syslog_seen or {})


def _rows(finding):
    return finding["rows"] if finding else []


# --- optics ------------------------------------------------------------------

def test_a_link_losing_light_over_weeks_is_the_headline():
    db = _DB(optic_rx_power_dbm=[
        {"device_id": "s4048", "port": "Te 1/37", "recent": -9.4, "baseline": -1.6},   # lost 7.8 dB
        {"device_id": "s4048", "port": "Te 1/38", "recent": -2.6, "baseline": -2.5},   # noise
    ])
    f = _insights(db).optics_losing_light([])

    assert f["level"] == "act" and len(f["rows"]) == 1
    assert f["rows"][0]["port"] == "Te 1/37" and f["rows"][0]["change"] == -7.8


def test_a_dark_port_cannot_drift():
    """Eight of this S4048's optics sit at the module floor. Comparing two
    floor readings is not a measurement, and they would swamp everything."""
    db = _DB(optic_rx_power_dbm=[{"device_id": "s4048", "port": "Te 1/13", "recent": -40.0, "baseline": -38.0}])
    assert _insights(db).optics_losing_light([]) is None


def test_drift_only_counts_devices_still_registered():
    db = _DB(optic_rx_power_dbm=[{"device_id": "a-device-since-removed", "port": "Te 1/1", "recent": -20.0, "baseline": -2.0}])
    assert _insights(db).optics_losing_light([]) is None


def test_margin_ranks_the_weakest_live_link_first():
    d = _Device("s4048", "S4048")
    live = [(d, _iface("Te 1/37", optic=HEALTHY)),
            (d, _iface("Te 1/41", optic={**HEALTHY, "rx_power_dbm": -8.9})),
            (d, _iface("Te 1/13", "admin_down", DARK))]
    f = _insights().optic_margin(live)

    assert [r["port"] for r in f["rows"]] == ["Te 1/41", "Te 1/37"], "dark and shut ports are not live links"
    assert f["rows"][0]["margin"] == 3.1, "-8.9 dBm against a -12 dBm floor"


def test_dark_optics_are_reported_as_something_to_reclaim():
    d = _Device("s4048", "S4048")
    live = [(d, _iface("Te 1/13", "admin_down", DARK)), (d, _iface("Te 1/35", "down", DARK)),
            (d, _iface("Te 1/37", "up", HEALTHY)), (d, _iface("Te 1/2", "down", None))]
    f = _insights().dark_optics(live)

    assert f["level"] == "note" and [r["port"] for r in f["rows"]] == ["Te 1/13", "Te 1/35"]


def test_optics_well_under_the_ceiling_are_not_worth_a_card():
    d = _Device("s4048", "S4048")
    assert _insights().hot_optics([(d, _iface("Te 1/37", optic=HEALTHY))]) is None

    hot = {**HEALTHY, "temperature_c": 61.0}
    f = _insights().hot_optics([(d, _iface("Te 1/37", optic=hot))])
    assert f["level"] == "watch" and f["rows"][0]["headroom"] == 9.0


# --- interfaces ----------------------------------------------------------------

def test_only_a_rise_in_error_counters_is_reported():
    db = _DB(iface_input_errors=[
        {"device_id": "s4048", "port": "Te 1/47", "metric": "iface_input_errors", "grew": 412, "total": 8821},
        {"device_id": "s4048", "port": "Te 1/48", "metric": "iface_output_errors", "grew": 3, "total": 3},
    ])
    f = _insights(db).error_growth([])

    assert f["level"] == "act" and [r["grew"] for r in f["rows"]] == [412, 3]
    assert f["rows"][0]["kind"] == "input"


def test_a_clean_week_produces_no_error_finding():
    assert _insights(_DB(iface_input_errors=[])).error_growth([]) is None


def test_busiest_ports_are_a_note_until_one_is_actually_full():
    d = _Device("s4048", "S4048")
    live = [(d, _iface("Te 1/41", speed="10000 Mbit"))]
    db = _DB(iface_input_mbps=[{"device_id": "s4048", "port": "Te 1/41", "metric": "iface_input_mbps", "p95": 255.0, "peak": 1144.0}])
    f = _insights(db, interfaces=live).busiest_ports(live)

    assert f["level"] == "note" and f["rows"][0]["used"] == 2.5

    db = _DB(iface_input_mbps=[{"device_id": "s4048", "port": "Te 1/41", "metric": "iface_input_mbps", "p95": 8000.0, "peak": 9900.0}])
    f = _insights(db, interfaces=live).busiest_ports(live)
    assert f["level"] == "watch" and f["rows"][0]["used"] == 80.0


def test_a_port_with_no_rate_data_is_still_listed_as_unlabelled():
    """The EX3300 reports no throughput at all; that must not hide its
    undescribed ports behind a zero."""
    d = _Device("ex3300", "EX3300")
    f = _insights().undocumented_ports([(d, _iface("ge-0/0/0", description=""))])
    assert f["rows"][0]["traffic"] == 0.0 and "busiest first" in f["summary"]


def test_only_live_unlabelled_ports_count_as_undocumented():
    d = _Device("s4048", "S4048")
    live = [(d, _iface("Te 1/41", description="", input_mbps=200, output_mbps=55)),
            (d, _iface("Te 1/37", description="OPNsense")),
            (d, _iface("Te 1/2", "down", description=""))]
    f = _insights().undocumented_ports(live)

    assert [r["port"] for r in f["rows"]] == ["Te 1/41"] and f["rows"][0]["traffic"] == 255.0


def test_a_shut_port_still_holding_a_module_is_worth_saying():
    d = _Device("s4048", "S4048")
    live = [(d, _iface("Te 1/13", "admin_down", DARK)), (d, _iface("Te 1/35", "down", DARK))]
    f = _insights().shut_with_optic(live)

    assert [r["port"] for r in f["rows"]] == ["Te 1/13"], "down is not the same as shut"


# --- events and fleet ------------------------------------------------------------

def test_repeat_offenders_rank_by_returns_and_episodes():
    now = datetime.now(timezone.utc)
    db = _DB(reopen_count=[
        {"device": "S4048", "subject": "Te 1/41", "kind": "port.link_down", "reports": 9, "returns": 6, "episodes": 1, "last_seen": now},
        {"device": "EX3300", "subject": "ge-0/0/5", "kind": "port.link_down", "reports": 2, "returns": 0, "episodes": 2, "last_seen": now},
    ])
    f = _insights(db).repeat_offenders([])

    assert f["level"] == "watch" and [r["subject"] for r in f["rows"]] == ["Te 1/41", "ge-0/0/5"]


def test_long_open_events_are_measured_from_when_they_were_raised():
    raised = datetime.now(timezone.utc) - timedelta(days=3)
    db = _DB(resolved_at=[{"id": 7, "device": "S4048", "subject": "PSU 2 (unit 1)", "kind": "env.psu",
                           "severity": "critical", "raised_at": raised, "count": 40}])
    f = _insights(db).longest_open([])

    assert f["level"] == "act" and f["rows"][0]["open_for"] == "3 d"


def test_a_device_that_has_sent_nothing_is_distinguished_from_one_gone_quiet():
    devices = [_Device("a", "Talks", "10.0.0.1"), _Device("b", "Silent", "10.0.0.2")]
    seen = {"10.0.0.1": datetime.now(timezone.utc)}
    f = _insights(devices=devices, syslog_seen=seen).quiet_syslog([])

    assert [r["device"] for r in f["rows"]] == ["Silent"]
    assert "nothing since" in f["rows"][0]["last"]


# --- the run ----------------------------------------------------------------------

def test_a_producer_that_fails_does_not_take_the_page_with_it():
    class _Broken(ins.Insights):
        def error_growth(self, live):
            """Ports with errors climbing"""
            raise RuntimeError("bad SQL")

    got = _Broken(_DB(), None, _Settings(), [_Device("s4048", "S4048")], lambda _id: {"interfaces": []}).run()

    assert got["failed"] == ["Ports with errors climbing"]
    assert isinstance(got["clear"], list) and got["generated_at"]


def test_findings_come_out_most_urgent_first_and_silence_is_reported_as_checked():
    db = _DB(optic_rx_power_dbm=[{"device_id": "s4048", "port": "Te 1/37", "recent": -9.4, "baseline": -1.6}])
    d = _Device("s4048", "S4048")
    got = ins.Insights(db, None, _Settings(), [d], lambda _id: {"interfaces": [_iface("Te 1/37", optic=HEALTHY)]},
                       syslog_seen={"192.168.4.106": datetime.now(timezone.utc)}).run()

    levels = [f["level"] for f in got["findings"]]
    assert levels == sorted(levels, key=lambda l: {"act": 0, "watch": 1, "note": 2}[l])
    assert got["findings"][0]["id"] == "optic_drift"
    assert got["clear"], "checks that found nothing are named, so the page can show it looked"
