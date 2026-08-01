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

import junos_parsers
import opnsense_parsers
import parsers
import trending
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


def _merge_errors(interfaces, errors):
    """Adds cumulative error/discard counters to each interface dict, from
    the same bare `show interfaces` output _merge_activity's rates already
    came from (see parsers.parse_interfaces_errors) - used for trend
    sampling (see trending.py), not shown live anywhere today."""
    for iface in interfaces:
        e = errors.get(iface["port"])
        if e is None:
            iface["input_errors"] = None
            iface["input_discards"] = None
            iface["output_errors"] = None
            iface["output_discards"] = None
            continue
        iface["input_errors"] = e["input_errors"]
        iface["input_discards"] = e["input_discards"]
        iface["output_errors"] = e["output_errors"]
        iface["output_discards"] = e["output_discards"]
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

    def __init__(self, get_session, lock_for, get_db=lambda: None, interval=30, transceiver_interval=300):
        self._get_session = get_session
        self._lock_for = lock_for
        # A callable, not a fixed reference: DB is None at import time and
        # only set once settings load/change (see app.py's _load_database)
        # - this background thread outlives any one Database instance, so
        # it needs to ask for the current one on every trend-sample write
        # rather than capturing a possibly-stale (or still-None) object.
        self._get_db = get_db
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
        # Compares thread *identity*, not just id membership: stop()+start()
        # for the same device id (done on device edit, to pick up changed
        # host/credentials) re-adds that id to _threads immediately, so a
        # plain "device.id in self._threads" check would let the old,
        # stale-credentialed thread keep looping right alongside the new
        # one instead of exiting.
        while not self._stop.is_set() and self._threads.get(device.id) is threading.current_thread():
            self._poll_once(device)
            now = time.time()
            if now - last_transceiver_poll >= self.transceiver_interval:
                self._poll_transceivers_once(device)
                self._record_trend_samples(device)
                last_transceiver_poll = now
            self._stop.wait(self.interval)

    def _poll_once(self, device):
        if device.platform == "junos":
            self._poll_once_junos(device)
        elif device.platform == "opnsense":
            self._poll_once_opnsense(device)
        else:
            self._poll_once_os9(device)

    def _poll_once_opnsense(self, device):
        """OPNsense equivalent of _poll_once_os9/_poll_once_junos - same
        DeviceStatus fields, populated from opnsense_parsers instead. No
        env (fans/PSUs/sensors) at all - a firewall appliance doesn't have
        Dell/Junos's chassis environment monitoring, and this app doesn't
        fabricate fields for hardware that isn't there. `device_alarms` is
        likewise always empty - OPNsense has no equivalent concept of
        active hardware alarms to poll."""
        status = self._status.setdefault(device.id, DeviceStatus())
        try:
            with self._lock_for(device.id):
                switch = self._get_session(device)
                top_stats = opnsense_parsers.parse_top(switch.run("top -b -d 1"))
                ifaces = opnsense_parsers.parse_ifconfig(switch.run("ifconfig -a"))

            interfaces = [
                {
                    "port": i["interface"],
                    "description": i["description"] or "",
                    "status": "Up" if i["status"] == "active" else ("Down" if i["status"] else "Unknown"),
                    "port_state": "up" if i["status"] == "active" else ("down" if i["status"] else "unknown"),
                    "admin_status": None,
                    "protocol_status": None,
                    "activity": False,
                }
                for i in ifaces
            ]
            up_count = sum(1 for i in interfaces if i["status"] == "Up")
            # Only interfaces that report a carrier state at all count
            # toward the total - loopback/pfsync/pflog/enc never do, and
            # counting them as "down" out of some denominator would be
            # misleading busy-work, not a real fact about the device.
            countable = [i for i in ifaces if i["status"] is not None]

            cpu_pct = top_stats.get("cpu_pct")
            cpu = {"overall": {"5sec": cpu_pct, "1min": cpu_pct, "5min": cpu_pct}, "cores": {}} if cpu_pct is not None else {}

            memory = {}
            if "mem_active_mb" in top_stats:
                used = round((top_stats["mem_active_mb"] + top_stats["mem_wired_mb"]) * 1_000_000)
                free = round(top_stats["mem_free_mb"] * 1_000_000)
                memory = {"total": used + free, "used": used, "free": free, "lowest": None, "largest": None}

            status.state = STATE_UP
            status.alarms = []
            status.interfaces = interfaces
            status.interfaces_up = up_count
            status.interfaces_total = len(countable)
            status.env = {}
            status.cpu = cpu
            status.memory = memory
            status.device_alarms = {"minor": [], "major": []}
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

    def _poll_once_junos(self, device):
        """Junos equivalent of _poll_once_os9 - same DeviceStatus fields,
        populated from junos_parsers instead. Some fields are approximated
        rather than exact, since Junos doesn't expose the same data:
        `cpu.overall` only has a single current utilization (Junos'
        `show chassis routing-engine` gives one point-in-time
        user/background/kernel/interrupt/idle breakdown, not Dell's
        rolling 5sec/1min/5min averages) - reported as 1min/5min too since
        it's the only real number available, not fabricated smoothing.
        `memory.used`/`free` are derived from the real `memory_pct` against
        total DRAM (Junos reports a percentage, not exact byte counts)."""
        status = self._status.setdefault(device.id, DeviceStatus())
        try:
            with self._lock_for(device.id):
                switch = self._get_session(device)
                re_data = junos_parsers.parse_junos_routing_engine(switch.run("show chassis routing-engine"))
                env_raw = junos_parsers.parse_junos_environment(switch.run("show chassis environment"))
                interfaces = junos_parsers.parse_junos_interfaces_terse(switch.run("show interfaces terse"))
                desc_rows = junos_parsers.parse_junos_interfaces_descriptions(switch.run("show interfaces descriptions"))
                device_alarms = junos_parsers.parse_junos_alarms(
                    switch.run("show chassis alarms"), switch.run("show system alarms")
                )

            desc_by_iface = {d["interface"]: d["description"] for d in desc_rows}
            for iface in interfaces:
                iface["port"] = iface["interface"]
                iface["status"] = "Up" if iface["link"] == "up" else "Down"
                iface["port_state"] = "up" if iface["link"] == "up" else ("admin_down" if iface["admin"] == "down" else "down")
                iface["description"] = desc_by_iface.get(iface["interface"], "")
                iface["activity"] = False  # not available from `show interfaces terse`

            fans = [
                {
                    "unit": "0", "bay": str(i),
                    "fan1_status": "up" if f["status"] == "OK" else "down", "fan1_rpm": None,
                    "fan2_status": None, "fan2_rpm": None,
                }
                for i, f in enumerate(env_raw["fans"])
            ]
            psus = [
                {
                    "unit": "0", "bay": str(i), "status": "up" if p["status"] == "OK" else "down", "type": "-",
                    "fan_status": None, "fan_speed_rpm": None, "power_watts": None, "avg_power_watts": None,
                }
                for i, p in enumerate(env_raw["psus"])
            ]
            env = {"fans": fans, "psus": psus, "units": [], "sensors": env_raw["sensors"]}

            cpu_pct = 100 - re_data.get("cpu", {}).get("idle", 100)
            cpu = {"overall": {"5sec": cpu_pct, "1min": cpu_pct, "5min": cpu_pct}, "cores": {}}

            memory = {}
            if re_data.get("dram_mb") is not None and re_data.get("memory_pct") is not None:
                total = re_data["dram_mb"] * 1_000_000
                used = round(total * re_data["memory_pct"] / 100)
                memory = {"total": total, "used": used, "free": total - used, "lowest": None, "largest": None}

            up_count = sum(1 for i in interfaces if i["link"] == "up")
            total = len(interfaces)
            state = STATE_ALARM if device_alarms.get("major") else STATE_UP
            alarms = [f"Major alarm: {e['text']}" for e in device_alarms.get("major", [])]
            alarms += [f"Minor alarm: {e['text']}" for e in device_alarms.get("minor", [])]

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

    def _poll_once_os9(self, device):
        status = self._status.setdefault(device.id, DeviceStatus())
        try:
            with self._lock_for(device.id):
                switch = self._get_session(device)
                env = parsers.parse_environment(switch.run("show environment"))
                interfaces = parsers.parse_interfaces_status(switch.run("show interfaces status"))
                desc_rows = parsers.parse_interfaces_description(switch.run("show interfaces description"))
                iface_raw = switch.run("show interfaces")
                rates = parsers.parse_interfaces_rates(iface_raw)
                errors = parsers.parse_interfaces_errors(iface_raw)
                cpu = parsers.parse_cpu(switch.run("show processes cpu"))
                memory = parsers.parse_memory(switch.run("show memory"))
                device_alarms = parsers.parse_alarms(switch.run("show alarms"))
            _merge_port_state(interfaces, desc_rows)
            _merge_activity(interfaces, rates)
            _merge_errors(interfaces, errors)
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
        # OPNsense has no SFP/transceiver diagnostics concept at all - it's
        # a firewall appliance, not a switch with pluggable optics.
        if device.platform == "opnsense":
            return
        status = self._status.get(device.id)
        if status is None:
            return
        transceivers = {}
        try:
            if device.platform == "junos":
                # One bulk round trip covers every optics-capable port
                # (confirmed live) - unlike Dell OS9, which has no
                # per-device "all ports" transceiver command and needs the
                # per-port loop below.
                with self._lock_for(device.id):
                    switch = self._get_session(device)
                    output = switch.run("show interfaces diagnostics optics")
                transceivers = junos_parsers.parse_junos_optics_diagnostics(output)
            else:
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
        except SwitchSSHError:
            pass
        except Exception:
            log.exception("unexpected error polling transceivers for %s", device.id)
        status.transceivers = transceivers
        status.transceivers_polled = datetime.now(timezone.utc)

        db = self._get_db()
        rows = []
        for port, t in transceivers.items():
            # Only a real optical reading is trend-worthy - AOC/DAC/absent
            # ports report None for these even when `present` is True (see
            # parsers.parse_transceiver / parse_junos_optics_diagnostics),
            # and trending a stream of nulls would just be noise.
            if not t or not t.get("present") or not t.get("dom_supported"):
                continue
            rows.append((device.id, "optic_rx_power_dbm", port, t.get("rx_power_dbm")))
            rows.append((device.id, "optic_tx_power_dbm", port, t.get("tx_power_dbm")))
            rows.append((device.id, "optic_temp_c", port, t.get("temperature_c")))
        trending.record_samples(db, rows)

    def _record_junos_trend_samples(self, device):
        """Junos equivalent of _record_trend_samples: `show interfaces
        terse` (the Junos fast poll's only interface command) has no
        traffic-rate or error-counter data at all, so unlike Dell OS9
        there's nothing already sitting in DeviceStatus to snapshot - this
        does its own single bulk `show interfaces extensive` round trip,
        on the slow cadence only (same reasoning as everywhere else in
        this module: trend data doesn't need 30s freshness, and this
        command's output is large). PSU wattage isn't recorded for Junos -
        confirmed live this platform (EX3300) has no CLI command exposing
        it at all (`show chassis power`/`show chassis environment pem`
        both error "not valid on the ex3300-48p"), nothing to sample."""
        db = self._get_db()
        if db is None:
            return
        try:
            with self._lock_for(device.id):
                switch = self._get_session(device)
                raw = switch.run("show interfaces extensive")
        except Exception:
            log.warning("could not fetch interface stats from %s for trending", device.id, exc_info=True)
            return
        errors = junos_parsers.parse_junos_interfaces_errors(raw)
        traffic = junos_parsers.parse_junos_interfaces_traffic_mbps(raw)
        rows = []
        for port, e in errors.items():
            rows.append((device.id, "iface_input_errors", port, e["input_errors"]))
            rows.append((device.id, "iface_output_errors", port, e["output_errors"]))
        for port, t in traffic.items():
            rows.append((device.id, "iface_input_mbps", port, t["input_mbps"]))
            rows.append((device.id, "iface_output_mbps", port, t["output_mbps"]))
        trending.record_samples(db, rows)

    def _record_trend_samples(self, device):
        """Persists a snapshot of PSU power draw and interface utilization/
        error counters already sitting in this device's DeviceStatus (from
        the most recent fast poll) - called on the slow cadence alongside
        _poll_transceivers_once so trend rows accumulate at ~288/day rather
        than every 30s, with no extra SSH round trips of its own. Junos
        has its own path (_record_junos_trend_samples) since its fast poll
        doesn't carry this data at all (see there)."""
        if device.platform == "junos":
            self._record_junos_trend_samples(device)
            return
        db = self._get_db()
        if db is None:
            return
        status = self._status.get(device.id)
        if status is None:
            return
        rows = []
        for psu in (status.env or {}).get("psus", []):
            if psu.get("removed"):
                continue
            bay = f"{psu['unit']}/{psu['bay']}"
            rows.append((device.id, "psu_power_watts", bay, psu.get("power_watts")))
        for iface in status.interfaces:
            port = iface.get("port")
            rows.append((device.id, "iface_input_mbps", port, iface.get("input_mbps")))
            rows.append((device.id, "iface_output_mbps", port, iface.get("output_mbps")))
            rows.append((device.id, "iface_input_errors", port, iface.get("input_errors")))
            rows.append((device.id, "iface_output_errors", port, iface.get("output_errors")))
        trending.record_samples(db, rows)
