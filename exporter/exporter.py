import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

from prometheus_client import Gauge, start_http_server

import junos_parsers
import parsers
from db import Database
from devices import DeviceConfigError, load_devices
from ssh_client import SwitchSSH, SwitchSSHError
from store import DeviceStore

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("exporter")

# In a PyInstaller onefile binary (packaging/build_binary.sh), __file__
# resolves to a temp extraction dir, not where the binary actually sits on
# disk - so a devices.yaml placed next to the binary (as install-binary.sh
# does) would silently never be found without this check. Docker/plain
# `python exporter.py` runs aren't frozen, so BASE_DIR is just this file's
# directory as before.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
DEVICES_PATH = os.environ.get("DEVICES_FILE", str(BASE_DIR / "devices.yaml"))
DATABASE_URL = os.environ.get("DATABASE_URL")
LISTEN_PORT = int(os.environ.get("EXPORTER_PORT", "9101"))
FAST_INTERVAL = int(os.environ.get("FAST_POLL_INTERVAL", "30"))
TRANSCEIVER_INTERVAL = int(os.environ.get("TRANSCEIVER_POLL_INTERVAL", "300"))
# How often the device registry (devices.yaml + Postgres) is re-read - this
# is what makes a device added/edited/removed in the webui's Devices page
# take effect here without restarting the exporter. 60s balances "add a
# device and see it show up quickly" against not hammering Postgres with a
# full registry re-query every poll cycle.
REGISTRY_REFRESH_INTERVAL = int(os.environ.get("REGISTRY_REFRESH_INTERVAL", "60"))

# --- Prometheus metrics -------------------------------------------------
#
# Every metric carries a `device_id` label now (matching the id used
# throughout the rest of this app - devices.py, alert_rules.py,
# interface_alerting.py all key on the same field) - previously this
# process only ever polled one hardcoded device, so no such label existed.
# Metric *names* keep their `s4048_*` prefix unchanged even though this now
# polls more than one Dell S4048 potentially - existing Grafana dashboards
# and Prometheus alert rules reference these names directly, and adding a
# label is additive (doesn't break a query with no label selector), while
# renaming every metric would be a separate, much bigger breaking change
# not part of this pass.

up = Gauge("s4048_up", "1 if the last SSH poll succeeded, else 0", ["device_id"])
scrape_duration = Gauge(
    "s4048_scrape_duration_seconds", "How long the last poll cycle took", ["device_id", "section"]
)

cpu_util = Gauge(
    "s4048_cpu_utilization_percent", "CPU utilization", ["device_id", "core", "window"]
)
mem_bytes = Gauge("s4048_memory_bytes", "Control-plane memory", ["device_id", "type"])

fan_status = Gauge("s4048_fan_status", "1=up 0=down", ["device_id", "unit", "bay", "fan"])
fan_rpm = Gauge("s4048_fan_speed_rpm", "Fan speed in RPM", ["device_id", "unit", "bay", "fan"])
psu_status = Gauge("s4048_psu_status", "1=up 0=down", ["device_id", "unit", "bay"])
psu_power_watts = Gauge(
    "s4048_psu_power_watts", "PSU power draw", ["device_id", "unit", "bay", "kind"]
)
unit_temp_c = Gauge("s4048_unit_temperature_celsius", "Chassis temperature", ["device_id", "unit"])
sensor_temp_c = Gauge(
    "s4048_sensor_temperature_celsius", "Thermal sensor reading", ["device_id", "sensor"]
)

iface_up = Gauge("s4048_interface_up", "1=up 0=down", ["device_id", "port", "description"])
iface_speed_mbps = Gauge("s4048_interface_speed_mbps", "Negotiated speed", ["device_id", "port"])

xcvr_present = Gauge("s4048_transceiver_present", "1=present 0=absent", ["device_id", "port"])
xcvr_temp_c = Gauge("s4048_transceiver_temperature_celsius", "Optic temperature", ["device_id", "port"])
xcvr_voltage_v = Gauge("s4048_transceiver_voltage_volts", "Optic supply voltage", ["device_id", "port"])
xcvr_bias_ma = Gauge("s4048_transceiver_tx_bias_ma", "Laser bias current", ["device_id", "port"])
xcvr_tx_dbm = Gauge("s4048_transceiver_tx_power_dbm", "Tx optical power", ["device_id", "port"])
xcvr_rx_dbm = Gauge("s4048_transceiver_rx_power_dbm", "Rx optical power", ["device_id", "port"])
xcvr_alarm = Gauge("s4048_transceiver_alarm", "1=alarm active", ["device_id", "port", "flag"])

INTERFACE_PREFIXES = [("Te", "TenGigabitEthernet"), ("Fo", "fortyGigE")]

# Junos environment items look like "FPC 0 Fan 1" / "FPC 0 Power Supply 0"
# (confirmed live on a real EX3300-48P) - a flat FPC-level list, not OS9's
# tray/bay hierarchy. See poll_fast_junos for how these map onto the
# shared unit/bay/fan Gauge labels.
_FPC_FAN_RE = re.compile(r"FPC (\d+) Fan (\d+)")
_FPC_PSU_RE = re.compile(r"FPC (\d+) Power Supply (\d+)")
_DEGREES_C_RE = re.compile(r"(\d+) degrees C")

# Platforms this exporter knows how to parse `show` output for today - see
# parsers.py (Dell OS9) and junos_parsers.py (Juniper Junos, shared with
# the webui's Console - same live-verified parsers, not a second guess at
# the output format). OPNsense is still listed in the registry same as any
# other device, but skipped here rather than guessed at - ROADMAP
# follow-up, not silently mis-parsed.
SUPPORTED_PLATFORMS = {"os9", "junos"}


def _speed_to_mbps(speed):
    if not speed or speed == "Auto" or speed == "--":
        return None
    parts = speed.split()
    try:
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def poll_fast(device_id, switch):
    start = time.time()

    cpu = parsers.parse_cpu(switch.run("show processes cpu"))
    for core, vals in cpu.get("cores", {}).items():
        for window, v in vals.items():
            cpu_util.labels(device_id=device_id, core=core, window=window).set(v)
    for window, v in cpu.get("overall", {}).items():
        cpu_util.labels(device_id=device_id, core="overall", window=window).set(v)

    mem = parsers.parse_memory(switch.run("show memory"))
    for k, v in mem.items():
        mem_bytes.labels(device_id=device_id, type=k).set(v)

    env = parsers.parse_environment(switch.run("show environment"))
    for f in env.get("fans", []):
        for fan_name in ("fan1", "fan2"):
            fan_status.labels(device_id=device_id, unit=f["unit"], bay=f["bay"], fan=fan_name).set(
                1 if f[f"{fan_name}_status"] == "up" else 0
            )
            fan_rpm.labels(device_id=device_id, unit=f["unit"], bay=f["bay"], fan=fan_name).set(
                f[f"{fan_name}_rpm"]
            )
    for p in env.get("psus", []):
        psu_status.labels(device_id=device_id, unit=p["unit"], bay=p["bay"]).set(
            1 if p["status"] == "up" else 0
        )
        psu_power_watts.labels(device_id=device_id, unit=p["unit"], bay=p["bay"], kind="instant").set(
            p["power_watts"]
        )
        psu_power_watts.labels(device_id=device_id, unit=p["unit"], bay=p["bay"], kind="average").set(
            p["avg_power_watts"]
        )
    for u in env.get("units", []):
        unit_temp_c.labels(device_id=device_id, unit=u["unit"]).set(u["temp_c"])
    for sensor, val in env.get("sensors", {}).items():
        sensor_temp_c.labels(device_id=device_id, sensor=sensor).set(val)

    interfaces = parsers.parse_interfaces_status(switch.run("show interfaces status"))
    for i in interfaces:
        iface_up.labels(device_id=device_id, port=i["port"], description=i["description"]).set(
            1 if i["status"] == "Up" else 0
        )
        mbps = _speed_to_mbps(i["speed"])
        if mbps is not None:
            iface_speed_mbps.labels(device_id=device_id, port=i["port"]).set(mbps)

    scrape_duration.labels(device_id=device_id, section="fast").set(time.time() - start)
    return interfaces


def poll_transceivers(device_id, switch, interfaces):
    start = time.time()
    for i in interfaces:
        port = i["port"]  # e.g. "Te 1/37"
        diag = parsers.parse_transceiver(switch.run(f"show interfaces {port} transceiver"))
        present = diag.get("present", False)
        xcvr_present.labels(device_id=device_id, port=port).set(1 if present else 0)
        if not present:
            continue
        for metric, gauge in (
            ("temperature_c", xcvr_temp_c),
            ("voltage_v", xcvr_voltage_v),
            ("tx_bias_ma", xcvr_bias_ma),
            ("tx_power_dbm", xcvr_tx_dbm),
            ("rx_power_dbm", xcvr_rx_dbm),
        ):
            val = diag.get(metric)
            if val is not None:
                gauge.labels(device_id=device_id, port=port).set(val)
        for flag in (
            "rx_los_state",
            "tx_fault_state",
            "temperature_high_alarm_flag",
            "temperature_low_alarm_flag",
            "rx_power_high_alarm_flag",
            "rx_power_low_alarm_flag",
            "tx_power_high_alarm_flag",
            "tx_power_low_alarm_flag",
        ):
            if flag in diag:
                xcvr_alarm.labels(device_id=device_id, port=port, flag=flag).set(1 if diag[flag] else 0)
    scrape_duration.labels(device_id=device_id, section="transceivers").set(time.time() - start)


def poll_fast_junos(device_id, switch):
    """Junos equivalent of poll_fast - uses junos_parsers.py (shared with
    the webui's Console, same live-verified parsers, not a second guess at
    Junos's output format). All four commands here run in ~5s total
    against a real 48-port EX3300 (confirmed live), fitting comfortably in
    a 10-30s fast cycle - unlike `show interfaces extensive` (~19s on the
    same device), which is why interface speed is on the slow cycle
    instead (see poll_slow_junos)."""
    start = time.time()

    re_info = junos_parsers.parse_junos_routing_engine(switch.run("show chassis routing-engine"))
    cpu = re_info.get("cpu", {})
    if "idle" in cpu:
        # Junos reports one instantaneous CPU snapshot (User/Background/
        # Kernel/Interrupt/Idle percentages), not OS9's per-core/
        # 5sec/1min/5min breakdown - core="re" and window="current" are
        # placeholders to fit the same Gauge label shape, not a real
        # multi-core or multi-window reading.
        cpu_util.labels(device_id=device_id, core="re", window="current").set(100 - cpu["idle"])
    if "dram_mb" in re_info and "memory_pct" in re_info:
        # Junos reports a percentage + total DRAM, not OS9's real
        # used/free/lowest/largest byte counts - used/free here are
        # derived (total * pct), not independently measured.
        total = re_info["dram_mb"] * 1024 * 1024
        used = total * re_info["memory_pct"] / 100
        mem_bytes.labels(device_id=device_id, type="total").set(total)
        mem_bytes.labels(device_id=device_id, type="used").set(used)
        mem_bytes.labels(device_id=device_id, type="free").set(total - used)
    if "temp_c" in re_info:
        unit_temp_c.labels(device_id=device_id, unit="0").set(re_info["temp_c"])

    env = junos_parsers.parse_junos_environment(switch.run("show chassis environment"))
    for f in env.get("fans", []):
        m = _FPC_FAN_RE.match(f["item"])
        if not m:
            continue
        fpc, fan_num = m.groups()
        # bay="0" is a structural placeholder - this hardware has no
        # tray/bay concept, just a flat per-FPC fan list (confirmed live).
        # fan_rpm is deliberately never set: Junos reports fan health
        # qualitatively ("Spinning at normal speed"), never a number.
        fan_status.labels(device_id=device_id, unit=fpc, bay="0", fan=fan_num).set(
            1 if f["status"] == "OK" else 0
        )
    for p in env.get("psus", []):
        m = _FPC_PSU_RE.match(p["item"])
        if not m:
            continue
        fpc, psu_num = m.groups()
        # psu_power_watts is deliberately never set: this hardware's PSU
        # rows report a blank Measurement column (confirmed live) - no
        # wattage reading exists to report.
        psu_status.labels(device_id=device_id, unit=fpc, bay=psu_num).set(1 if p["status"] == "OK" else 0)
    for sensor, measurement in env.get("sensors", {}).items():
        m = _DEGREES_C_RE.search(measurement)
        if m:
            sensor_temp_c.labels(device_id=device_id, sensor=sensor).set(int(m.group(1)))

    terse = junos_parsers.parse_junos_interfaces_terse(switch.run("show interfaces terse"))
    descriptions = {
        d["interface"]: d["description"]
        for d in junos_parsers.parse_junos_interfaces_descriptions(switch.run("show interfaces descriptions"))
    }
    for i in terse:
        iface_up.labels(
            device_id=device_id, port=i["interface"], description=descriptions.get(i["interface"], "")
        ).set(1 if i["admin"] == "up" and i["link"] == "up" else 0)

    scrape_duration.labels(device_id=device_id, section="fast").set(time.time() - start)
    return terse


def poll_slow_junos(device_id, switch):
    """Junos equivalent of poll_transceivers, run on the same
    TRANSCEIVER_INTERVAL cadence - `show interfaces extensive` (the only
    way to get negotiated per-port speed; the `Speed:` field on the
    Link-level line just reports "Auto", the port's configured mode, not
    the actual rate - see junos_parsers.parse_junos_interfaces_speed's
    docstring) took ~19s against a real 48-port EX3300 (confirmed live) -
    far too slow for a 10-30s fast cycle."""
    start = time.time()

    speeds = junos_parsers.parse_junos_interfaces_speed(switch.run("show interfaces extensive"))
    for iface, mbps in speeds.items():
        iface_speed_mbps.labels(device_id=device_id, port=iface).set(mbps)

    optics = junos_parsers.parse_junos_optics_diagnostics(switch.run("show interfaces diagnostics optics"))
    for iface, diag in optics.items():
        xcvr_present.labels(device_id=device_id, port=iface).set(1 if diag.get("present") else 0)
        # temperature_c/rx_power_dbm/tx_power_dbm only ever populate once a
        # real DOM-capable optic is confirmed live on some Junos device in
        # this fleet - see parse_junos_optics_diagnostics' docstring.
        # Every port here today is present=False (DAC/copper, confirmed
        # live), so this loop body is currently a no-op in production, not
        # dead code - it activates the moment real optics data exists.
        for metric, gauge in (
            ("temperature_c", xcvr_temp_c),
            ("rx_power_dbm", xcvr_rx_dbm),
            ("tx_power_dbm", xcvr_tx_dbm),
        ):
            val = diag.get(metric)
            if val is not None:
                gauge.labels(device_id=device_id, port=iface).set(val)

    scrape_duration.labels(device_id=device_id, section="transceivers").set(time.time() - start)


def _poll_loop(device, stop_event):
    """One thread per device, each with its own persistent SwitchSSH
    session - mirrors the webui's own "one session per device, reused
    across requests" model (ssh_client.py's connect-throttling semaphore
    is shared process-wide, so this doesn't open a connection storm even
    with many devices). Blocking paramiko I/O is the reason this is
    threads and not a single asyncio loop - a naive sequential loop over N
    devices would make the fast-poll cadence degrade linearly with fleet
    size."""
    device_id = device.id
    switch = SwitchSSH(
        device.host, device.username, device.password,
        enable_password=device.enable_password,
        private_key=device.private_key, passphrase=device.passphrase,
        platform=device.platform,
    )
    log.info("device_id=%s host=%s: starting poll loop", device_id, device.host)
    last_transceiver_poll = 0.0
    last_interfaces = []

    fast_fn, slow_fn = (poll_fast_junos, poll_slow_junos) if device.platform == "junos" else (poll_fast, poll_transceivers)

    while not stop_event.is_set():
        cycle_start = time.time()
        try:
            if not switch.connected():
                switch.connect()
            last_interfaces = fast_fn(device_id, switch)

            if cycle_start - last_transceiver_poll >= TRANSCEIVER_INTERVAL:
                if device.platform == "junos":
                    slow_fn(device_id, switch)
                else:
                    slow_fn(device_id, switch, last_interfaces)
                last_transceiver_poll = cycle_start

            up.labels(device_id=device_id).set(1)
        except SwitchSSHError as e:
            log.warning("device_id=%s poll failed: %s", device_id, e)
            up.labels(device_id=device_id).set(0)
            switch.close()
        except Exception:
            log.exception("device_id=%s unexpected error during poll", device_id)
            up.labels(device_id=device_id).set(0)
            switch.close()

        elapsed = time.time() - cycle_start
        stop_event.wait(max(1.0, FAST_INTERVAL - elapsed))

    switch.close()
    log.info("device_id=%s: poll loop stopped (removed from registry)", device_id)


class _NullStore:
    """Stand-in for DeviceStore when DATABASE_URL isn't set - a
    deployment that only ever uses devices.yaml (the original,
    single-device SWITCH_HOST/USER/PASS shape) shouldn't require Postgres
    just to boot the exporter."""

    def load(self):
        return []


def _build_store():
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set - only devices.yaml (if any) will be polled, no UI-added devices")
        return _NullStore()
    return DeviceStore(Database(DATABASE_URL))


def main():
    start_http_server(LISTEN_PORT)
    log.info("metrics server listening on :%d", LISTEN_PORT)

    store = _build_store()
    threads = {}  # device_id -> (Thread, stop_event)
    warned_unsupported = set()

    while True:
        try:
            devices = load_devices(DEVICES_PATH, store)
        except (DeviceConfigError, Exception) as e:
            log.error("could not load device registry, keeping existing poll loops running: %s", e)
            time.sleep(REGISTRY_REFRESH_INTERVAL)
            continue

        seen = set()
        for device in devices:
            if device.platform not in SUPPORTED_PLATFORMS:
                if device.id not in warned_unsupported:
                    log.info(
                        "device_id=%s platform=%s: not yet supported by the exporter (OS9 only today) - skipping",
                        device.id, device.platform,
                    )
                    warned_unsupported.add(device.id)
                continue
            seen.add(device.id)
            if device.id not in threads or not threads[device.id][0].is_alive():
                stop_event = threading.Event()
                t = threading.Thread(target=_poll_loop, args=(device, stop_event), daemon=True, name=f"poll-{device.id}")
                threads[device.id] = (t, stop_event)
                t.start()

        for device_id in list(threads):
            if device_id not in seen:
                log.info("device_id=%s: no longer in the registry, stopping its poll loop", device_id)
                _, stop_event = threads.pop(device_id)
                stop_event.set()
                # Deliberately not clearing this device's Gauge label
                # combinations here - Prometheus will just keep serving
                # its last-known values until this process restarts. Fine
                # for how rarely devices are removed; not worth the
                # bookkeeping of tracking every label combination emitted
                # per device just to un-set them.

        time.sleep(REGISTRY_REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
