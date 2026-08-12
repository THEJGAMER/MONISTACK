"""Tests for common/junos_parsers.py.

Previously untested despite being shared by *both* the webui and the
exporter and running against a real EX3300 in production - a silent
regression here corrupts CPU/memory/fan/PSU/interface data in two
services at once, with nothing to catch it.

Every fixture in tests/fixtures/junos/ is **real output captured from the
live EX3300 at 192.168.4.1**, not hand-written. That matters more than
usual here: the things that break screen-scrapers are exactly the details
nobody thinks to invent - the trailing `{master:0}` prompt on every
response, an empty System Name column, a port whose LLDP "port info" is
just a MAC address, the same local port appearing twice with two
neighbours, and fan status being the words "Spinning at normal speed"
rather than an RPM number.

Where a case can't be captured (a failed PSU, an absent fan tray) it is
constructed and labelled as such, rather than left untested or quietly
presented as real.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import junos_parsers as jp

FIXTURES = Path(__file__).parent / "fixtures" / "junos"


def fx(name):
    return (FIXTURES / f"{name}.txt").read_text()


# --- the artifact every one of these has to survive ------------------

def test_every_fixture_carries_the_junos_prompt_artifact():
    """Junos appends `{master:0}` to interactive output. It is in every
    captured fixture, so if any parser were written against cleaned-up
    text this suite would be testing something the device never sends."""
    assert "{master:0}" in fx("environment")
    assert "{master:0}" in fx("interfaces_terse")
    assert "{master:0}" in fx("chassis_alarms")


# --- alarms ----------------------------------------------------------

def test_no_alarms_parses_as_no_alarms():
    """Real captured healthy state. "No alarms currently active" must not
    be mistaken for an alarm entry - a false major alarm here would page
    someone."""
    result = jp.parse_junos_alarms(fx("chassis_alarms"), fx("system_alarms"))

    assert result == {"minor": [], "major": []}


def test_a_real_alarm_is_classified_by_severity():
    """Constructed, not captured - the device has been healthy throughout.
    Shape taken from Junos's documented `show chassis alarms` columns."""
    chassis = (
        "1 alarms currently active\n"
        "Alarm time               Class  Description\n"
        "2026-08-12 09:14:22 UTC  Major  PEM 0 Not OK\n"
        "2026-08-12 09:14:22 UTC  Minor  Fan 1 not spinning\n"
        "\n{master:0}"
    )
    result = jp.parse_junos_alarms(chassis, "")

    assert any("PEM 0" in e["text"] for e in result["major"])
    assert any("Fan 1" in e["text"] for e in result["minor"])


# --- environment (fans / PSUs / sensors) ------------------------------

def test_environment_separates_fans_psus_and_sensors():
    env = jp.parse_junos_environment(fx("environment"))

    assert [f["item"] for f in env["fans"]] == ["FPC 0 Fan 1", "FPC 0 Fan 2"]
    assert [p["item"] for p in env["psus"]] == ["FPC 0 Power Supply 0"]
    assert env["sensors"]["FPC 0 CPU"].startswith("35 degrees C")


def test_fan_health_is_words_not_a_number():
    """This hardware reports "Spinning at normal speed" with no RPM at
    all. Anything downstream expecting a number has to cope, and the
    exporter's own docstring records this - so it is pinned here."""
    env = jp.parse_junos_environment(fx("environment"))

    assert env["fans"][0]["measurement"] == "Spinning at normal speed"
    assert env["fans"][0]["status"] == "OK"


def test_a_psu_with_no_measurement_still_parses():
    """Real captured: the PSU row has a status but an empty Measurement
    column. A parser assuming three populated columns would drop the only
    power supply this switch has."""
    env = jp.parse_junos_environment(fx("environment"))

    assert env["psus"][0]["status"] == "OK"
    assert env["psus"][0]["measurement"] == ""


def test_a_failed_component_keeps_its_real_status_string():
    """Constructed - the device has stayed healthy. Everything downstream
    decides "faulted" by comparing against "OK" (see status_poller's Junos
    mapping), so the parser must pass the real word through rather than
    normalising it."""
    text = (
        "Class Item                           Status     Measurement\n"
        "Power FPC 0 Power Supply 0           Failed    \n"
        "Fans  FPC 0 Fan 1                    Check      Spinning at normal speed\n"
        "\n{master:0}"
    )
    env = jp.parse_junos_environment(text)

    assert env["psus"][0]["status"] == "Failed"
    assert env["fans"][0]["status"] == "Check"
    assert all(x["status"] != "OK" for x in env["psus"] + env["fans"])


# --- routing engine (CPU / memory / model) ---------------------------

def test_routing_engine_fields():
    re_data = jp.parse_junos_routing_engine(fx("routing_engine"))

    assert re_data["model"] == "EX3300 48-Port POE+"
    assert re_data["dram_mb"] == 1024
    assert 0 <= re_data["memory_pct"] <= 100
    assert re_data["cpu"]["idle"] + re_data["cpu"]["user"] <= 100


def test_cpu_utilisation_is_derived_from_idle():
    """The exporter and status_poller both compute utilisation as
    100 - idle, so `idle` has to be present and sane or CPU graphs are
    silently wrong rather than missing."""
    cpu = jp.parse_junos_routing_engine(fx("routing_engine"))["cpu"]

    assert isinstance(cpu["idle"], int)
    assert 0 <= 100 - cpu["idle"] <= 100


# --- interfaces -------------------------------------------------------

def test_interfaces_terse_parses_every_port():
    ifaces = jp.parse_junos_interfaces_terse(fx("interfaces_terse"))

    assert len(ifaces) == 51
    by_name = {i["interface"]: i for i in ifaces}
    assert by_name["ge-0/0/0"]["link"] == "up"
    assert by_name["ge-0/0/1"]["link"] == "down"
    assert all(i["admin"] in ("up", "down") for i in ifaces)


def test_only_interfaces_with_a_description_are_returned():
    """Real captured: just two of 51 ports carry one. The Console merges
    these onto the terse list by name, so extra or missing rows here show
    up as wrong labels in the UI."""
    rows = jp.parse_junos_interfaces_descriptions(fx("interfaces_descriptions"))

    assert {r["interface"]: r["description"] for r in rows} == {
        "ge-0/0/7": "LOUNGE-ROOM-AP",
        "vlan.900": "MANAGEMENT",
    }


# --- LLDP -------------------------------------------------------------

def test_lldp_strips_the_unit_suffix_from_port_names():
    """LLDP reports `ge-0/0/46.0` while every other command says
    `ge-0/0/46`. Topology joins these two together by name, so without
    the strip the neighbour never matches the port."""
    neighbours = jp.parse_junos_lldp(fx("lldp_neighbors"))

    assert all("." not in n["local_port"] for n in neighbours)
    assert any(n["local_port"] == "ge-0/0/46" for n in neighbours)


def test_lldp_handles_a_missing_system_name():
    """Real captured: the System Name column is simply absent on several
    rows. Positional parsing would shift every field left."""
    by_port = {n["local_port"]: n for n in jp.parse_junos_lldp(fx("lldp_neighbors"))}

    assert by_port["xe-0/1/3"]["remote_system_name"] is None
    assert by_port["xe-0/1/3"]["remote_port_description"] == "TenGigabitEthernet 1/47"
    # ...while a row that does have one keeps it.
    assert by_port["ge-0/0/9"]["remote_system_name"] == "PROX5.mung.win"


def test_lldp_keeps_the_parent_interface_for_a_lag_member():
    """xe-0/1/2 and xe-0/1/3 are both in ae1 - the LAG membership is what
    makes the topology view draw one logical link instead of two."""
    by_port = {n["local_port"]: n for n in jp.parse_junos_lldp(fx("lldp_neighbors"))}

    assert by_port["xe-0/1/3"]["parent_interface"] == "ae1"
    assert by_port["ge-0/0/9"]["parent_interface"] is None


def test_lldp_allows_two_neighbours_on_one_local_port():
    """Real captured: ge-0/0/46 genuinely reports two different chassis
    ids. Keying results by local port would silently discard one."""
    neighbours = jp.parse_junos_lldp(fx("lldp_neighbors"))

    on_46 = [n for n in neighbours if n["local_port"] == "ge-0/0/46"]
    assert len(on_46) == 2
    assert len({n["remote_chassis_id"] for n in on_46}) == 2


# --- ARP / switching table -------------------------------------------

def test_arp_entries_parse():
    entries = jp.parse_arp(fx("arp"))

    assert len(entries) == 55
    first = entries[0]
    assert first["ip"].count(".") == 3
    assert first["mac"].count(":") == 5
    assert all(e["interface"] for e in entries)


def test_ethernet_switching_table_parses():
    entries = jp.parse_ethernet_switching_table(fx("ethernet_switching_table"))

    assert len(entries) == 58
    assert all(e["mac"].count(":") == 5 for e in entries)
    assert any(e["interface"] == "ae1" for e in entries)


# --- empty / unexpected input ----------------------------------------

@pytest.mark.parametrize("fn", [
    jp.parse_junos_environment,
    jp.parse_junos_routing_engine,
    jp.parse_junos_interfaces_terse,
    jp.parse_junos_interfaces_descriptions,
    jp.parse_junos_lldp,
    jp.parse_arp,
    jp.parse_ethernet_switching_table,
])
def test_parsers_survive_empty_output(fn):
    """An SSH read that returns nothing (session hiccup, command
    unsupported on a model) must yield an empty result, not raise into the
    poll loop - status_poller catches broadly, but a parser raising means
    the whole device's status is lost rather than one field."""
    result = fn("")
    assert result in ([], {}) or isinstance(result, dict)


def test_a_truncated_row_does_not_become_a_fabricated_fault():
    """The bug this file found. Column-slicing a line that ends early gave
    the component an empty Status, and everything downstream decides
    "faulted" by comparing against the literal "OK" - so a garbled read
    turned into `fan1_status="down"`, i.e. an invented fan failure, which
    now pages within ~10s via hardware_alerting."""
    env = jp.parse_junos_environment("Class Item          Status\nFans  FPC 0 Fan\n")

    assert env["fans"] == [], "a status-less row must be dropped, not reported as down"


def test_a_status_less_psu_row_is_dropped_too():
    text = "Class Item                Status     Measurement\nPower FPC 0 Power Supply 0\n"

    assert jp.parse_junos_environment(text)["psus"] == []


def test_dropping_status_less_rows_does_not_affect_real_output():
    """The guard must not cost anything on healthy captured output - all
    three components still parse."""
    env = jp.parse_junos_environment(fx("environment"))

    assert len(env["fans"]) == 2 and len(env["psus"]) == 1


def test_a_sensor_without_a_reading_is_still_kept():
    """Sensors are exempt from the guard: nothing alerts on them, and a
    temperature with no measurement is harmless rather than a fault."""
    text = "Class Item                Status     Measurement\nTemp  FPC 0 CPU\n"

    assert "FPC 0 CPU" in jp.parse_junos_environment(text)["sensors"]
