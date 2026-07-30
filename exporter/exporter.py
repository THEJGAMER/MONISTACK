import logging
import os
import time

from prometheus_client import Gauge, start_http_server

import parsers
from ssh_client import SwitchSSH, SwitchSSHError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("exporter")

HOST = os.environ["SWITCH_HOST"]
USERNAME = os.environ["SWITCH_USER"]
PASSWORD = os.environ["SWITCH_PASS"]
ENABLE_PASSWORD = os.environ.get("SWITCH_ENABLE_PASS", PASSWORD)
LISTEN_PORT = int(os.environ.get("EXPORTER_PORT", "9101"))
FAST_INTERVAL = int(os.environ.get("FAST_POLL_INTERVAL", "30"))
TRANSCEIVER_INTERVAL = int(os.environ.get("TRANSCEIVER_POLL_INTERVAL", "300"))

# --- Prometheus metrics -------------------------------------------------

up = Gauge("s4048_up", "1 if the last SSH poll succeeded, else 0")
scrape_duration = Gauge(
    "s4048_scrape_duration_seconds", "How long the last poll cycle took", ["section"]
)

cpu_util = Gauge(
    "s4048_cpu_utilization_percent", "CPU utilization", ["core", "window"]
)
mem_bytes = Gauge("s4048_memory_bytes", "Control-plane memory", ["type"])

fan_status = Gauge("s4048_fan_status", "1=up 0=down", ["unit", "bay", "fan"])
fan_rpm = Gauge("s4048_fan_speed_rpm", "Fan speed in RPM", ["unit", "bay", "fan"])
psu_status = Gauge("s4048_psu_status", "1=up 0=down", ["unit", "bay"])
psu_power_watts = Gauge(
    "s4048_psu_power_watts", "PSU power draw", ["unit", "bay", "kind"]
)
unit_temp_c = Gauge("s4048_unit_temperature_celsius", "Chassis temperature", ["unit"])
sensor_temp_c = Gauge(
    "s4048_sensor_temperature_celsius", "Thermal sensor reading", ["sensor"]
)

iface_up = Gauge("s4048_interface_up", "1=up 0=down", ["port", "description"])
iface_speed_mbps = Gauge("s4048_interface_speed_mbps", "Negotiated speed", ["port"])

xcvr_present = Gauge("s4048_transceiver_present", "1=present 0=absent", ["port"])
xcvr_temp_c = Gauge("s4048_transceiver_temperature_celsius", "Optic temperature", ["port"])
xcvr_voltage_v = Gauge("s4048_transceiver_voltage_volts", "Optic supply voltage", ["port"])
xcvr_bias_ma = Gauge("s4048_transceiver_tx_bias_ma", "Laser bias current", ["port"])
xcvr_tx_dbm = Gauge("s4048_transceiver_tx_power_dbm", "Tx optical power", ["port"])
xcvr_rx_dbm = Gauge("s4048_transceiver_rx_power_dbm", "Rx optical power", ["port"])
xcvr_alarm = Gauge("s4048_transceiver_alarm", "1=alarm active", ["port", "flag"])

INTERFACE_PREFIXES = [("Te", "TenGigabitEthernet"), ("Fo", "fortyGigE")]


def _speed_to_mbps(speed):
    if not speed or speed == "Auto" or speed == "--":
        return None
    parts = speed.split()
    try:
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def poll_fast(switch):
    start = time.time()

    cpu = parsers.parse_cpu(switch.run("show processes cpu"))
    for core, vals in cpu.get("cores", {}).items():
        for window, v in vals.items():
            cpu_util.labels(core=core, window=window).set(v)
    for window, v in cpu.get("overall", {}).items():
        cpu_util.labels(core="overall", window=window).set(v)

    mem = parsers.parse_memory(switch.run("show memory"))
    for k, v in mem.items():
        mem_bytes.labels(type=k).set(v)

    env = parsers.parse_environment(switch.run("show environment"))
    for f in env.get("fans", []):
        for fan_name in ("fan1", "fan2"):
            fan_status.labels(unit=f["unit"], bay=f["bay"], fan=fan_name).set(
                1 if f[f"{fan_name}_status"] == "up" else 0
            )
            fan_rpm.labels(unit=f["unit"], bay=f["bay"], fan=fan_name).set(
                f[f"{fan_name}_rpm"]
            )
    for p in env.get("psus", []):
        psu_status.labels(unit=p["unit"], bay=p["bay"]).set(
            1 if p["status"] == "up" else 0
        )
        psu_power_watts.labels(unit=p["unit"], bay=p["bay"], kind="instant").set(
            p["power_watts"]
        )
        psu_power_watts.labels(unit=p["unit"], bay=p["bay"], kind="average").set(
            p["avg_power_watts"]
        )
    for u in env.get("units", []):
        unit_temp_c.labels(unit=u["unit"]).set(u["temp_c"])
    for sensor, val in env.get("sensors", {}).items():
        sensor_temp_c.labels(sensor=sensor).set(val)

    interfaces = parsers.parse_interfaces_status(switch.run("show interfaces status"))
    for i in interfaces:
        iface_up.labels(port=i["port"], description=i["description"]).set(
            1 if i["status"] == "Up" else 0
        )
        mbps = _speed_to_mbps(i["speed"])
        if mbps is not None:
            iface_speed_mbps.labels(port=i["port"]).set(mbps)

    scrape_duration.labels(section="fast").set(time.time() - start)
    return interfaces


def poll_transceivers(switch, interfaces):
    start = time.time()
    for i in interfaces:
        port = i["port"]  # e.g. "Te 1/37"
        diag = parsers.parse_transceiver(switch.run(f"show interfaces {port} transceiver"))
        present = diag.get("present", False)
        xcvr_present.labels(port=port).set(1 if present else 0)
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
                gauge.labels(port=port).set(val)
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
                xcvr_alarm.labels(port=port, flag=flag).set(1 if diag[flag] else 0)
    scrape_duration.labels(section="transceivers").set(time.time() - start)


def main():
    start_http_server(LISTEN_PORT)
    log.info("metrics server listening on :%d", LISTEN_PORT)

    switch = SwitchSSH(
        HOST, USERNAME, PASSWORD, enable_password=ENABLE_PASSWORD
    )

    last_transceiver_poll = 0.0
    last_interfaces = []

    while True:
        cycle_start = time.time()
        try:
            if not switch.connected():
                switch.connect()
            last_interfaces = poll_fast(switch)

            if cycle_start - last_transceiver_poll >= TRANSCEIVER_INTERVAL:
                poll_transceivers(switch, last_interfaces)
                last_transceiver_poll = cycle_start

            up.set(1)
        except SwitchSSHError as e:
            log.warning("poll failed: %s", e)
            up.set(0)
            switch.close()
        except Exception:
            log.exception("unexpected error during poll")
            up.set(0)
            switch.close()

        elapsed = time.time() - cycle_start
        time.sleep(max(1.0, FAST_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
