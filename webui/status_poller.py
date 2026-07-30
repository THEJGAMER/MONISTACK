"""Live device status, polled the same way the Prometheus exporter does -
over SSH, reusing the exporter's own parsing (parsers.py) - but as its own
lightweight background poller inside the webui process.

This is deliberately NOT a dependency on the actual s4048-exporter
container or Prometheus: the exporter keeps running completely on its own
(own SSH session, own poll loop, own metrics) and this module doesn't talk
to it. It exists so the webui can show "is this device up, and how stale
is that answer" for *any* configured device, including ones the exporter
was never pointed at, without requiring the full Prometheus/Grafana stack
to be reachable from the webui.

Two poll cadences per device, both on the same background thread:
- fast (every `interval` seconds): environment, interface status +
  admin/protocol state, CPU, memory.
- slow (every `transceiver_interval` seconds, default 300s like the
  exporter): per-port transceiver diagnostics - 54 sequential SSH round
  trips is too slow to do every 30s, same reasoning the exporter already
  uses for its own transceiver poll.

One background thread per device, taking the device's own lock (shared
with app.py's command-run path) around each individual SSH command so a
status poll and a user-triggered command never race on the same channel,
and neither one blocks the other for longer than a single command.
"""
import logging
import threading
import time
from datetime import datetime, timezone

import parsers
from ssh_client import SwitchSSHError

log = logging.getLogger("webui.status")

STATE_UP = "up"
STATE_ALARM = "alarm"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"


def _merge_port_state(interfaces, desc_rows):
    """Adds admin_status/protocol_status/port_state to each interface dict,
    using `show interfaces description` - the only command that
    distinguishes administratively-shutdown ports from ports that are
    simply enabled-but-linkless (`show interfaces status` alone shows both
    as plain "Down")."""
    by_port = {d["port"]: d for d in desc_rows}
    for iface in interfaces:
        d = by_port.get(iface["port"])
        if d is None:
            iface["admin_status"] = None
            iface["protocol_status"] = None
            iface["port_state"] = "up" if iface["status"] == "Up" else "down"
            continue
        iface["admin_status"] = d["admin_status"]
        iface["protocol_status"] = d["protocol_status"]
        if d["admin_status"] == "admin down":
            iface["port_state"] = "admin_down"
        else:
            iface["port_state"] = "up" if d["protocol_status"] == "up" else "down"
    return interfaces


def _merge_activity(interfaces, rates):
    """Adds real traffic activity to each interface dict, from the switch's
    own device-computed Rate info (a rolling ~299-second average) - not a
    fabricated blink. `activity` is true when either direction currently
    has nonzero packets/sec."""
    for iface in interfaces:
        r = rates.get(iface["port"])
        if r is None:
            iface["activity"] = False
            iface["input_mbps"] = None
            iface["output_mbps"] = None
            continue
        iface["activity"] = r["input_pps"] > 0 or r["output_pps"] > 0
        iface["input_mbps"] = r["input_mbps"]
        iface["output_mbps"] = r["output_mbps"]
    return interfaces


def _fan_placeholder(unit, bay):
    return {
        "unit": unit, "bay": bay, "tray_status": "down",
        "fan1_status": "down", "fan1_rpm": 0,
        "fan2_status": "down", "fan2_rpm": 0,
        "removed": True,
    }


def _psu_placeholder(unit, bay):
    return {
        "unit": unit, "bay": bay, "status": "down", "type": "-",
        "fan_status": "down", "fan_speed_rpm": 0,
        "power_watts": 0, "avg_power_watts": 0,
        "removed": True,
    }


def _fill_missing_bays(current_items, known_keys, placeholder_fn):
    """`show environment` simply stops listing a fan tray or PSU bay once
    it's physically removed - the row just disappears, which reads as
    "nothing to see here" instead of "something is missing". `known_keys`
    (a set persisted on the DeviceStatus across polls) remembers every
    (unit, bay) ever seen; any key that's gone missing this cycle gets a
    synthetic "down" row instead of silently vanishing from the table."""
    current_keys = set()
    for item in current_items:
        key = (item["unit"], item["bay"])
        current_keys.add(key)
        known_keys.add(key)
    missing = known_keys - current_keys
    result = list(current_items)
    for unit, bay in sorted(missing):
        result.append(placeholder_fn(unit, bay))
    return result


def _evaluate(env, interfaces):
    """Turn parsed 'show environment' / 'show interfaces status' output into
    an overall state plus a list of human-readable alarm strings."""
    alarms = []

    for fan in env.get("fans", []):
        if fan["fan1_status"] != "up" or fan["fan2_status"] != "up":
            suffix = " (removed)" if fan.get("removed") else ""
            alarms.append(f"Fan alarm: unit {fan['unit']} bay {fan['bay']}{suffix}")
    for psu in env.get("psus", []):
        if psu["status"] != "up":
            suffix = " (removed)" if psu.get("removed") else ""
            alarms.append(f"PSU alarm: unit {psu['unit']} bay {psu['bay']}{suffix}")
    for unit in env.get("units", []):
        if unit["status"] != "online":
            alarms.append(f"Unit {unit['unit']} status: {unit['status']}")
        if not unit["voltage_ok"]:
            alarms.append(f"Unit {unit['unit']} voltage check failed")

    up_count = sum(1 for i in interfaces if i["status"] == "Up")
    total = len(interfaces)

    state = STATE_ALARM if alarms else STATE_UP
    return state, alarms, up_count, total


class DeviceStatus:
    __slots__ = (
        "state", "alarms", "interfaces", "interfaces_up", "interfaces_total",
        "env", "cpu", "memory", "transceivers", "transceivers_polled",
        "device_alarms", "known_fan_bays", "known_psu_bays",
        "last_polled", "last_error",
    )

    def __init__(self):
        self.state = STATE_UNKNOWN
        self.alarms = []
        self.interfaces = []  # [{port, description, status, speed, duplex, vlan, admin_status, protocol_status, port_state}]
        self.interfaces_up = None
        self.interfaces_total = None
        self.env = {}  # raw parse_environment() output: fans/psus/units/sensors
        self.cpu = {}
        self.memory = {}
        self.transceivers = {}  # port -> parse_transceiver() output
        self.transceivers_polled = None
        self.device_alarms = {"minor": [], "major": []}  # from `show alarms` - the switch's own authoritative list
        self.known_fan_bays = set()  # (unit, bay) ever seen - so a removed bay shows "down", not gone
        self.known_psu_bays = set()
        self.last_polled = None  # datetime, UTC
        self.last_error = None

    def to_dict(self, include_interfaces=False):
        age_seconds = None
        if self.last_polled is not None:
            age_seconds = max(0.0, (datetime.now(timezone.utc) - self.last_polled).total_seconds())
        out = {
            "state": self.state,
            "alarms": self.alarms,
            "interfaces_up": self.interfaces_up,
            "interfaces_total": self.interfaces_total,
            "env": self.env,
            "cpu": self.cpu,
            "memory": self.memory,
            "device_alarms": self.device_alarms,
            "last_polled": self.last_polled.isoformat() if self.last_polled else None,
            "age_seconds": age_seconds,
            "last_error": self.last_error,
            "transceivers_polled": self.transceivers_polled.isoformat() if self.transceivers_polled else None,
        }
        if include_interfaces:
            merged = []
            for iface in self.interfaces:
                item = dict(iface)
                item["transceiver"] = self.transceivers.get(iface["port"])
                merged.append(item)
            out["interfaces"] = merged
        return out


class StatusPoller:
    """Polls every registered device on its own background thread.

    get_session(device) / lock_for(device_id) are injected so this reuses
    app.py's existing persistent-SSH-session-per-device machinery rather
    than opening a second, competing session per device.
    """

    def __init__(self, get_session, lock_for, interval=30, transceiver_interval=300):
        self._get_session = get_session
        self._lock_for = lock_for
        self.interval = interval
        self.transceiver_interval = transceiver_interval
        self._status: dict[str, DeviceStatus] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()

    def start(self, device):
        if device.id in self._threads:
            return
        self._status[device.id] = DeviceStatus()
        t = threading.Thread(target=self._run, args=(device,), name=f"status-{device.id}", daemon=True)
        self._threads[device.id] = t
        t.start()

    def stop(self, device_id):
        self._threads.pop(device_id, None)
        self._status.pop(device_id, None)

    def get(self, device_id, include_interfaces=False):
        status = self._status.get(device_id)
        return status.to_dict(include_interfaces=include_interfaces) if status else None

    def refresh_now(self, device):
        """Forces an immediate fast poll, for a UI "Refresh" button - runs
        on the caller's (request) thread, not the background one, but goes
        through the same per-device lock so it can't race the poller."""
        self._poll_once(device)
        return self.get(device.id, include_interfaces=True)

    def _run(self, device):
        last_transceiver_poll = 0.0
        while not self._stop.is_set() and device.id in self._threads:
            self._poll_once(device)
            now = time.time()
            if now - last_transceiver_poll >= self.transceiver_interval:
                self._poll_transceivers_once(device)
                last_transceiver_poll = now
            self._stop.wait(self.interval)

    def _poll_once(self, device):
        status = self._status.setdefault(device.id, DeviceStatus())
        try:
            with self._lock_for(device.id):
                switch = self._get_session(device)
                env = parsers.parse_environment(switch.run("show environment"))
                interfaces = parsers.parse_interfaces_status(switch.run("show interfaces status"))
                desc_rows = parsers.parse_interfaces_description(switch.run("show interfaces description"))
                rates = parsers.parse_interfaces_rates(switch.run("show interfaces"))
                cpu = parsers.parse_cpu(switch.run("show processes cpu"))
                memory = parsers.parse_memory(switch.run("show memory"))
                device_alarms = parsers.parse_alarms(switch.run("show alarms"))
            _merge_port_state(interfaces, desc_rows)
            _merge_activity(interfaces, rates)
            env["fans"] = _fill_missing_bays(env.get("fans", []), status.known_fan_bays, _fan_placeholder)
            env["psus"] = _fill_missing_bays(env.get("psus", []), status.known_psu_bays, _psu_placeholder)
            state, alarms, up_count, total = _evaluate(env, interfaces)
            # The switch's own `show alarms` is authoritative - fold its
            # Minor/Major entries into the same alarm state/list our own
            # environment-derived checks produce, rather than replacing
            # them (the two sources don't perfectly overlap: `show alarms`
            # may catch things `show environment` doesn't, and vice versa).
            for entry in device_alarms.get("major", []):
                alarms.append(f"Major alarm: {entry['text']}")
            for entry in device_alarms.get("minor", []):
                alarms.append(f"Minor alarm: {entry['text']}")
            if device_alarms.get("major") or device_alarms.get("minor"):
                state = STATE_ALARM
            status.state = state
            status.alarms = alarms
            status.interfaces = interfaces
            status.interfaces_up = up_count
            status.interfaces_total = total
            status.env = env
            status.cpu = cpu
            status.memory = memory
            status.device_alarms = device_alarms
            status.last_error = None
        except SwitchSSHError as e:
            status.state = STATE_DOWN
            status.alarms = []
            status.last_error = str(e)
        except Exception:
            log.exception("unexpected error polling status for %s", device.id)
            status.state = STATE_DOWN
            status.last_error = "internal error"
        finally:
            status.last_polled = datetime.now(timezone.utc)

    def _poll_transceivers_once(self, device):
        status = self._status.get(device.id)
        if status is None:
            return
        transceivers = {}
        for port in device.valid_ports:
            try:
                with self._lock_for(device.id):
                    switch = self._get_session(device)
                    output = switch.run(f"show interfaces {port} transceiver")
                transceivers[port] = parsers.parse_transceiver(output)
            except SwitchSSHError:
                continue
            except Exception:
                log.exception("unexpected error polling transceiver %s on %s", port, device.id)
                continue
        status.transceivers = transceivers
        status.transceivers_polled = datetime.now(timezone.utc)
