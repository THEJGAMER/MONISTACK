"""Parser tests against real captured Dell OS9 output (ROADMAP.md 0.2) -
every fixture in tests/fixtures/ was captured live via SSH from the real
S4048 in this fleet (see conftest.py), never hand-written/guessed, same
practice the parsers themselves were built against. Expected values below
were read back from the actual parser output against these exact fixture
files, not invented - the point is to freeze today's known-correct
behavior so a future change to these regex-based parsers gets caught
immediately instead of silently drifting.
"""
import parsers

from conftest import load_fixture


def test_parse_environment():
    env = parsers.parse_environment(load_fixture("environment.txt"))
    assert len(env["fans"]) == 3
    assert env["fans"][0] == {
        "unit": "1", "bay": "1", "tray_status": "up",
        "fan1_status": "up", "fan1_rpm": 10031,
        "fan2_status": "up", "fan2_rpm": 10031,
    }
    assert len(env["psus"]) == 2
    assert env["psus"][0]["status"] == "up"
    assert env["psus"][0]["type"] == "AC"
    assert env["psus"][0]["power_watts"] == 61
    assert len(env["units"]) == 1
    assert env["units"][0]["status"] == "online"
    assert env["units"][0]["voltage_ok"] is True


def test_parse_environment_psu_down_reports_type_unknown():
    """Regression test for a real bug: a PSU that's down/removed reports
    Type `UNKNOWN` instead of `AC`/`DC` - a fixture captured live from a
    real PSU 2 failure on the S4048. The regex used to only match
    AC|DC, which silently dropped this row entirely (so `s4048_psu_status`
    in the exporter went stale instead of ever reflecting the fault, and
    S4048PSUDown never fired despite a real ongoing failure)."""
    env = parsers.parse_environment(load_fixture("environment_psu_down.txt"))
    assert len(env["psus"]) == 2
    down = next(p for p in env["psus"] if p["bay"] == "2")
    assert down["status"] == "down"
    assert down["type"] == "UNKNOWN"
    assert down["fan_status"] == "down"
    assert down["power_watts"] == 0


def test_parse_environment_fan_absent_reports_both_fans_down():
    """Regression test for a real bug: a physically removed fan tray
    reports TrayStatus `absent` with none of the Fan1/Speed/Fan2/Speed
    columns present at all - a fixture captured live from a real fan-tray
    pull on the S4048 (bay 3). The regex used to only match rows with all
    five up|down/speed fields, which silently dropped this row entirely
    (so `s4048_fan_status{bay="3",...}` in the exporter went stale at its
    last "up" reading instead of ever reflecting the removal, and
    S4048FanDown never fired despite the tray being physically gone)."""
    env = parsers.parse_environment(load_fixture("environment_fan_absent.txt"))
    assert len(env["fans"]) == 3
    absent = next(f for f in env["fans"] if f["bay"] == "3")
    assert absent["tray_status"] == "absent"
    assert absent["fan1_status"] == "down"
    assert absent["fan1_rpm"] == 0
    assert absent["fan2_status"] == "down"
    assert absent["fan2_rpm"] == 0
    # The other two trays are genuinely fine and must not be affected.
    still_up = [f for f in env["fans"] if f["bay"] != "3"]
    assert all(f["fan1_status"] == "up" and f["fan2_status"] == "up" for f in still_up)


def test_parse_interfaces_status():
    rows = parsers.parse_interfaces_status(load_fixture("interfaces_status.txt"))
    assert len(rows) == 54
    te_1_37 = next(r for r in rows if r["port"] == "Te 1/37")
    assert te_1_37 == {
        "port": "Te 1/37", "description": "OPNsense", "status": "Up",
        "speed": "10000 Mbit", "duplex": "Full", "vlan": "20",
    }
    # A down/unused port should still be a real row, not skipped.
    assert any(r["status"] == "Down" for r in rows)


def test_parse_interfaces_description():
    rows = parsers.parse_interfaces_description(load_fixture("interfaces_description.txt"))
    assert len(rows) > 0
    te_1_1 = next(r for r in rows if r["port"] == "Te 1/1")
    assert te_1_1["admin_status"] == "admin down"
    assert te_1_1["protocol_status"] == "down"


def test_parse_transceiver_optical():
    """Te 1/37: a real 10GBASE-LR optic with full DOM diagnostics."""
    t = parsers.parse_transceiver(load_fixture("transceiver_optical.txt"))
    assert t["present"] is True
    assert t["dom_supported"] is True
    assert t["type"] == "10GBASE-LR"
    assert t["temperature_c"] == 33.414
    assert t["rx_power_dbm"] == -1.0936
    assert t["tx_power_dbm"] == -0.8166
    assert t["rx_los_state"] is False


def test_parse_transceiver_aoc():
    """Te 1/39: an Active Optical Cable - present, but no DOM readings."""
    t = parsers.parse_transceiver(load_fixture("transceiver_aoc.txt"))
    assert t["present"] is True
    assert t["dom_supported"] is False
    assert t["type"] == "10GBASE-SR-AOC-5M"
    assert t["rx_power_dbm"] is None
    assert t["temperature_c"] is None


def test_parse_transceiver_dac():
    """Te 1/47: a copper DAC - present, but no DOM readings either."""
    t = parsers.parse_transceiver(load_fixture("transceiver_dac.txt"))
    assert t["present"] is True
    assert t["dom_supported"] is False
    assert t["type"] == "10GBASE-CU1M"
    assert t["rx_power_dbm"] is None


def test_parse_interfaces_rates():
    rates = parsers.parse_interfaces_rates(load_fixture("interfaces_rates.txt"))
    assert len(rates) == 54  # every port has a "Rate info" block, up or down
    r = rates["Te 1/37"]
    assert set(r) == {"input_mbps", "input_pps", "output_mbps", "output_pps"}
    assert isinstance(r["input_mbps"], float)
    # A down/unused port still reports real zeros, not fabricated ones.
    assert rates["Te 1/2"] == {"input_mbps": 0.0, "input_pps": 0, "output_mbps": 0.0, "output_pps": 0}


def test_parse_interfaces_errors():
    errors = parsers.parse_interfaces_errors(load_fixture("interfaces_rates.txt"))
    assert "Te 1/37" in errors
    e = errors["Te 1/37"]
    assert set(e) == {"input_errors", "input_discards", "output_errors", "output_discards"}
    assert all(isinstance(v, int) for v in e.values())


def test_parse_alarms_empty():
    alarms = parsers.parse_alarms(load_fixture("alarms.txt"))
    assert alarms == {"minor": [], "major": []}


def test_parse_cpu():
    cpu = parsers.parse_cpu(load_fixture("cpu.txt"))
    assert cpu["overall"] == {"5sec": 13.82, "1min": 21.98, "5min": 21.65}
    assert cpu["cores"]["0"] == {"5sec": 11.33, "1min": 23.33, "5min": 20.79}
    assert cpu["cores"]["1"] == {"5sec": 16.3, "1min": 20.63, "5min": 22.52}


def test_parse_memory():
    mem = parsers.parse_memory(load_fixture("memory.txt"))
    assert mem == {
        "total": 3203182592, "used": 2687662, "free": 3200494930,
        "lowest": 3200130522, "largest": 3200494930,
    }
