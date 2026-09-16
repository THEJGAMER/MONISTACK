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
"""
import logging
from datetime import datetime, timezone

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
        self.acted = 0

    def reconcile(self, device_id, device, status):
        """`status` = StatusPoller's to_dict(include_interfaces=True)."""
        if status is None:
            return 0
        acted = self._reachability(device_id, device, status)
        polled_at = status.get("last_polled")
        if not polled_at or self._seen_polled_at.get(device_id) == polled_at:
            self.acted += acted
            return acted
        self._seen_polled_at[device_id] = polled_at
        polled_dt = _dt(polled_at)
        acted += self._ports(device_id, device, status.get("interfaces") or [], polled_dt)
        acted += self._environment(device_id, device, status.get("env") or {}, polled_dt)
        acted += self._compute(device_id, device, status)
        self.acted += acted
        return acted

    # --- helpers ---------------------------------------------------------------

    def _raise(self, kind, device_id, device, subject, title, detail=None, severity=None):
        sev = severity or self.settings.severity_for(kind)
        if sev == "ignore":
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
                                         detail="seen by the SSH poll" + (" (down at first poll)" if first_sight else ""), severity=sev)
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
            acted += self._raise(kind, device_id, device, subject, f"{name}: {subject} on {device}", detail=f"show environment reports {state}")
        return acted

    def _compute(self, device_id, device, status):
        acted = 0
        cpu = ((status.get("cpu") or {}).get("overall") or {}).get("1min")
        if cpu is not None:
            p = self.settings.params_for("compute.cpu_high")
            if float(cpu) >= float(p.get("raise_percent", 90)):
                n = self._cpu_passes.get(device_id, 0) + 1
                self._cpu_passes[device_id] = n
                if n >= max(1, int(p.get("polls", 3))):
                    acted += self._raise("compute.cpu_high", device_id, device, "cpu", f"High CPU on {device}: {float(cpu):.0f}%",
                                         detail=f"{float(cpu):.0f}% for {n} polls (threshold {p.get('raise_percent', 90)}%)")
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
                                         detail=f"{pct:.0f}% used for {n} polls (threshold {p.get('raise_percent', 90)}%)")
            else:
                self._mem_passes[device_id] = 0
                if pct <= float(p.get("clear_percent", 85)):
                    open_ev = self.store.open_kind("compute.memory_high", device_id, "memory")
                    if open_ev:
                        acted += 1 if self.store.resolve(open_ev["signature"], by="ssh", detail=f"memory {pct:.0f}%") else 0
        return acted
