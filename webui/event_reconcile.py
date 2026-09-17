"""The SSH fallback: events for what the poll sees that syslog never said.

status_poller.py already polls every device over SSH (interfaces,
environment, CPU, memory); this reads its cached state each cycle and
reconciles it with the open events - never an extra SSH round trip.

Rules of the fallback, learned the hard way in the alarm era:
- Ports raise on a *transition this reconciler observed* (up last poll,
  down now), not on absolute state: a switch has dozens of unused,
  linkless ports, and "every down port is an event" at startup is a
  storm. The one exception is a port someone explicitly classified on
  the Ports tab: if it is down on first sight, that is an event.
- A resolve from the poll must postdate the event: the poller's snapshot
  can be older than a syslog "down" that arrived seconds ago, and a stale
  "up" must not close a real outage (confirmed live, once).
- Fans/PSUs are few and named, so their state is reconciled directly.
- Reachability: consecutive failed polls raise; the next good poll resolves.
- CPU/memory: thresholds from the catalogue, held for N polls.
- Optics are only judged on a link that is **up**. Measured live on the
  S4048: 8 of its 12 DOM optics sit at -40 dBm with Rx-LOS and low-power
  alarms set, every one of them on an unused or shut port. That is dark
  fibre, not a fault, and alarming on it would have produced 8 immediate
  criticals. A live link reading low is the signal worth having.
- Error counters are cumulative, so only a *rise* is a fault: a port that
  logged errors once a year ago and never again is fine. A counter going
  backwards is a device reboot, not a negative error rate.
- **Staleness cuts both ways.** The poll reads a cache that can be most of
  a cycle old; syslog is immediate. A poll must not resolve something
  raised after the snapshot was taken, and equally must not *raise*
  something that syslog has already reported fixed since. Seen in
  production: syslog resolved Te 1/41 at 09:54:56 and the poll re-raised
  it 8.7 seconds later from a snapshot taken while it was still down.
"""
import logging
from datetime import datetime, timezone

from eventstore import signature_for

log = logging.getLogger("webui.events.reconcile")


def _dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SshReconciler:
    def __init__(self, store, settings, ports):
        self.store = store
        self.settings = settings
        self.ports = ports
        self._seen_polled_at = {}    # device_id -> last_polled considered
        self._prev_port = {}         # (device_id, port) -> port_state
        self._fail_passes = {}       # device_id -> consecutive passes with last_error
        self._cpu_passes = {}        # device_id -> consecutive polls over threshold
        self._mem_passes = {}
        self._seen_optics_at = {}    # device_id -> last transceivers_polled considered
        self._counters = {}          # (device_id, port) -> last error/discard counters
        self._optic_present = {}     # (device_id, port) -> was a transceiver in it
        self.acted = 0

    def reconcile(self, device_id, device, status):
        """`status` = StatusPoller's to_dict(include_interfaces=True)."""
        if status is None:
            return 0
        acted = self._reachability(device_id, device, status)
        interfaces = status.get("interfaces") or []
        polled_at = status.get("last_polled")
        if polled_at and self._seen_polled_at.get(device_id) != polled_at:
            self._seen_polled_at[device_id] = polled_at
            polled_dt = _dt(polled_at)
            acted += self._ports(device_id, device, interfaces, polled_dt)
            acted += self._environment(device_id, device, status.get("env") or {}, polled_dt)
            acted += self._compute(device_id, device, status, polled_dt)
            acted += self._errors(device_id, device, interfaces, polled_dt)
        # Optics ride the poller's slow cycle (every ~5 min), so they have
        # their own freshness stamp - checking them on the fast tick would
        # re-count the same reading every 15 seconds.
        optics_at = status.get("transceivers_polled")
        if optics_at and self._seen_optics_at.get(device_id) != optics_at:
            self._seen_optics_at[device_id] = optics_at
            acted += self._optics(device_id, device, interfaces, _dt(optics_at))
        self.acted += acted
        return acted

    # --- helpers ---------------------------------------------------------------

    def _port_ignored(self, device_id, port):
        """A port someone marked `ignore` is uninteresting entirely - its
        optics and its error counters too, not only its link state."""
        return self.ports.severity_for(device_id, port, None) == "ignore"

    def _superseded(self, kind, device_id, device, subject, fresh_as_of):
        """Has anything newer than this snapshot already closed this?

        The mirror of _resolve_if_stale_safe. Without it, a fault that
        came and went inside one poll interval is re-raised from the
        snapshot taken while it was down - and because a returning fault
        re-opens its own event, that also drags the episode out."""
        if fresh_as_of is None:
            return False
        last = self.store.latest_for(signature_for(kind, device_id or device, subject))
        if not last or not last.get("resolved_at"):
            return False
        resolved = _dt(last["resolved_at"])
        return resolved is not None and resolved > fresh_as_of

    def _raise(self, kind, device_id, device, subject, title, detail=None, severity=None, fresh_as_of=None):
        sev = severity or self.settings.severity_for(kind)
        if sev == "ignore":
            return 0
        if self._superseded(kind, device_id, device, subject, fresh_as_of):
            return 0
        _, created = self.store.raise_event(kind, sev, device_id, device, subject, title, detail=detail, source="ssh")
        return 1 if created else 0

    def _resolve_if_stale_safe(self, kind, device_id, device, subject, polled_dt, detail):
        """Resolve only when the poll is newer than the event it would close.

        Against `reopened_at` when there is one, not `raised_at`: a
        re-opened episode keeps its original raise time (that is the point
        of re-opening), so comparing against it would let a poll taken
        before the fault came back close it again."""
        open_ev = self.store.open_kind(kind, device_id, subject)
        if open_ev is None:
            return 0
        raised = _dt(open_ev.get("reopened_at") or open_ev["raised_at"])
        if polled_dt is not None and raised is not None and polled_dt <= raised:
            return 0
        return 1 if self.store.resolve(open_ev["signature"], by="ssh", detail=detail) else 0

    # --- the checks ---------------------------------------------------------------

    def _reachability(self, device_id, device, status):
        params = self.settings.params_for("device.unreachable")
        threshold = max(1, int(params.get("polls", 3)))
        err = status.get("last_error")
        if err:
            n = self._fail_passes.get(device_id, 0) + 1
            self._fail_passes[device_id] = n
            if n >= threshold:
                return self._raise("device.unreachable", device_id, device, "ssh",
                                   f"{device} unreachable over SSH", detail=str(err)[:500])
            return 0
        self._fail_passes[device_id] = 0
        open_ev = self.store.open_kind("device.unreachable", device_id, "ssh")
        if open_ev and status.get("last_polled"):
            return 1 if self.store.resolve(open_ev["signature"], by="ssh", detail="poll succeeded") else 0
        return 0

    def _ports(self, device_id, device, interfaces, polled_dt):
        acted = 0
        default = self.settings.severity_for("port.link_down")
        for iface in interfaces:
            port = iface.get("port")
            state = iface.get("port_state")
            if not port or state is None:
                continue
            key = (device_id, port)
            prev = self._prev_port.get(key)
            self._prev_port[key] = state
            if state == "down":
                sev = self.ports.severity_for(device_id, port, default)
                if sev == "ignore":
                    continue
                first_sight = prev is None
                if prev == "up" or (first_sight and self.ports.has_override(device_id, port)):
                    acted += self._raise("port.link_down", device_id, device, port, f"Link down: {port} on {device}",
                                         detail="The SSH poll found it down" + (", on the first look at this port" if first_sight else ""),
                                         severity=sev, fresh_as_of=polled_dt)
            elif state == "up":
                acted += self._resolve_if_stale_safe("port.link_down", device_id, device, port, polled_dt, "SSH poll shows the port up")
        return acted

    def _environment(self, device_id, device, env, polled_dt):
        acted = 0
        faulted = {}
        for fan in env.get("fans", []) or []:
            statuses = [s for s in (fan.get("fan1_status"), fan.get("fan2_status")) if s is not None]
            subject = f"Fan tray {fan.get('bay')} (unit {fan.get('unit')})"
            if statuses and any(s != "up" for s in statuses):
                faulted[("env.fan", subject)] = "removed" if fan.get("removed") else "down"
            else:
                acted += self._resolve_if_stale_safe("env.fan", device_id, device, subject, polled_dt, "SSH poll shows it up")
        for psu in env.get("psus", []) or []:
            subject = f"PSU {psu.get('bay')} (unit {psu.get('unit')})"
            if psu.get("status") != "up":
                faulted[("env.psu", subject)] = "removed" if psu.get("removed") else str(psu.get("status"))
            else:
                acted += self._resolve_if_stale_safe("env.psu", device_id, device, subject, polled_dt, "SSH poll shows it up")
        for (kind, subject), state in faulted.items():
            name = "Power supply fault" if kind == "env.psu" else "Fan fault"
            acted += self._raise(kind, device_id, device, subject, f"{name}: {subject} on {device}",
                                 detail=f"show environment reports it {state}", fresh_as_of=polled_dt)
        return acted

    # --- optics and error counters -------------------------------------------

    def _optics(self, device_id, device, interfaces, fresh_as_of=None):
        acted = 0
        ceiling = float(self.settings.params_for("optic.temperature").get("ceiling_c", 70))
        floor_dbm = float(self.settings.params_for("optic.rx_power_low").get("floor_dbm", -12))
        for iface in interfaces:
            port = iface.get("port")
            t = iface.get("transceiver") or {}
            if not port or self._port_ignored(device_id, port):
                continue

            # A module that was there and is not any more, whatever the link is doing.
            was = self._optic_present.get((device_id, port))
            now = bool(t.get("present"))
            self._optic_present[(device_id, port)] = now
            if was and not now:
                acted += self._raise("optic.removed", device_id, device, port,
                                     f"Transceiver removed: {port} on {device}",
                                     detail="The SSH poll no longer sees a module", fresh_as_of=fresh_as_of)
            elif now and not was:
                acted += self._resolve_now("optic.removed", device_id, port, "a transceiver is in the port again")

            # Everything below is a reading, and a reading only means
            # something on a link that is carrying light.
            if not (now and t.get("dom_supported")) or iface.get("port_state") != "up":
                continue
            rx, tx, temp = t.get("rx_power_dbm"), t.get("tx_power_dbm"), t.get("temperature_c")

            rx_txt = f" at {rx:.1f} dBm" if rx is not None else ""
            low = t.get("rx_power_low_alarm_flag") or (rx is not None and rx <= floor_dbm)
            acted += self._set("optic.rx_power_low", low, device_id, device, port,
                               f"Low receive power: {port} on {device}{rx_txt}",
                               "The module raised its own low-power alarm" if t.get("rx_power_low_alarm_flag")
                               else f"Receiving {rx:.1f} dBm, against a floor of {floor_dbm:.0f} dBm",
                               "receive power is back within limits", fresh_as_of=fresh_as_of)
            acted += self._set("optic.rx_power_high", bool(t.get("rx_power_high_alarm_flag")), device_id, device, port,
                               f"High receive power: {port} on {device}{rx_txt}",
                               "The module raised its own high-power alarm", "receive power is back within limits", fresh_as_of=fresh_as_of)
            fault = t.get("tx_fault_state") or t.get("tx_power_low_alarm_flag")
            acted += self._set("optic.tx_fault", bool(fault), device_id, device, port,
                               f"Transmit fault: {port} on {device}",
                               "The module reports a transmit fault" if t.get("tx_fault_state")
                               else f"Transmit power has fallen to {tx:.1f} dBm",
                               "the module no longer reports a transmit fault", fresh_as_of=fresh_as_of)
            hot = t.get("temperature_high_alarm_flag") or (temp is not None and temp >= ceiling)
            acted += self._set("optic.temperature", bool(hot), device_id, device, port,
                               f"Optic temperature: {port} on {device}" + (f" at {temp:.0f} C" if temp is not None else ""),
                               "The module raised its own temperature alarm" if t.get("temperature_high_alarm_flag")
                               else f"Running at {temp:.0f} C, against a ceiling of {ceiling:.0f} C",
                               "the optic has cooled", fresh_as_of=fresh_as_of)
        return acted

    def _set(self, kind, faulted, device_id, device, subject, title, detail, cleared_detail, fresh_as_of=None):
        """Raise while the condition holds, resolve the moment it stops."""
        if faulted:
            return self._raise(kind, device_id, device, subject, title, detail=detail, fresh_as_of=fresh_as_of)
        return self._resolve_now(kind, device_id, subject, cleared_detail)

    def _resolve_now(self, kind, device_id, subject, detail):
        open_ev = self.store.open_kind(kind, device_id, subject)
        if open_ev is None:
            return 0
        return 1 if self.store.resolve(open_ev["signature"], by="ssh", detail=detail) else 0

    COUNTERS = (
        ("port.input_errors", "input_errors", "Input errors"),
        ("port.output_errors", "output_errors", "Output errors"),
        ("port.discards", ("input_discards", "output_discards"), "Discards"),
    )

    def _errors(self, device_id, device, interfaces, fresh_as_of=None):
        acted = 0
        for iface in interfaces:
            port = iface.get("port")
            if not port or self._port_ignored(device_id, port):
                continue
            key = (device_id, port)
            previous = self._counters.get(key, {})
            current = {}
            for kind, fields, label in self.COUNTERS:
                fields = (fields,) if isinstance(fields, str) else fields
                values = [iface.get(f) for f in fields]
                if any(v is None for v in values):
                    continue   # Junos reports no counters; nothing to compare
                total = sum(int(v) for v in values)
                current[kind] = total
                if kind not in previous or iface.get("port_state") != "up":
                    continue
                delta = total - previous[kind]
                if delta < 0:
                    continue   # counters reset: the device restarted
                threshold = max(1, int(self.settings.params_for(kind).get("per_poll", 10)))
                if delta >= threshold:
                    acted += self._raise(kind, device_id, device, port,
                                         f"{label} rising: {port} on {device} (+{delta})",
                                         detail=f"{delta} more since the last poll, {total} in total",
                                         fresh_as_of=fresh_as_of)
                elif delta == 0:
                    acted += self._resolve_now(kind, device_id, port, f"no further increase ({total} in total)")
            if current:
                self._counters[key] = {**previous, **current}
        return acted

    def _compute(self, device_id, device, status, fresh_as_of=None):
        acted = 0
        cpu = ((status.get("cpu") or {}).get("overall") or {}).get("1min")
        if cpu is not None:
            p = self.settings.params_for("compute.cpu_high")
            if float(cpu) >= float(p.get("raise_percent", 90)):
                n = self._cpu_passes.get(device_id, 0) + 1
                self._cpu_passes[device_id] = n
                if n >= max(1, int(p.get("polls", 3))):
                    acted += self._raise("compute.cpu_high", device_id, device, "cpu", f"High CPU on {device}: {float(cpu):.0f}%",
                                         detail=f"At {float(cpu):.0f}% for {n} consecutive polls, against a threshold of {p.get('raise_percent', 90)}%",
                                         fresh_as_of=fresh_as_of)
            else:
                self._cpu_passes[device_id] = 0
                if float(cpu) <= float(p.get("clear_percent", 80)):
                    open_ev = self.store.open_kind("compute.cpu_high", device_id, "cpu")
                    if open_ev:
                        acted += 1 if self.store.resolve(open_ev["signature"], by="ssh", detail=f"CPU {float(cpu):.0f}%") else 0
        mem = status.get("memory") or {}
        if mem.get("total") and mem.get("used") is not None:
            pct = 100.0 * float(mem["used"]) / float(mem["total"])
            p = self.settings.params_for("compute.memory_high")
            if pct >= float(p.get("raise_percent", 90)):
                n = self._mem_passes.get(device_id, 0) + 1
                self._mem_passes[device_id] = n
                if n >= max(1, int(p.get("polls", 2))):
                    acted += self._raise("compute.memory_high", device_id, device, "memory", f"High memory on {device}: {pct:.0f}%",
                                         detail=f"{pct:.0f}% in use for {n} consecutive polls, against a threshold of {p.get('raise_percent', 90)}%",
                                         fresh_as_of=fresh_as_of)
            else:
                self._mem_passes[device_id] = 0
                if pct <= float(p.get("clear_percent", 85)):
                    open_ev = self.store.open_kind("compute.memory_high", device_id, "memory")
                    if open_ev:
                        acted += 1 if self.store.resolve(open_ev["signature"], by="ssh", detail=f"memory {pct:.0f}%") else 0
        return acted
