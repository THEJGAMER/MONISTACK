"""Insights: what is worth knowing about this network right now.

Not a dashboard of numbers and not a second alarm list. Events say what
*broke*; this says what is worth a look - a link quietly losing light
over three weeks, eight optics sitting in ports nobody uses, the one port
carrying most of the traffic, the live ports nobody labelled.

Three kinds of finding, because a healthy network should still have
something to show:

- **act**   something is wrong or heading that way
- **watch** worth an eye, not an emergency
- **note**  a fact about the network worth knowing

A producer that finds nothing returns None and is reported as a check
that passed, so the page can prove it looked rather than going quiet.

Everything is derived from what is already collected - the trend samples
(`metric_samples`), the SSH poller's live state, and the event store. No
producer talks to a device.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("webui.insights")

DRIFT_DB = 1.0          # dB of loss over the baseline window before it is worth saying
DARK_DBM = -35.0        # below this an optic is not reading light at all, it is dark
UTILISATION_WATCH = 70  # % of link speed at the 95th percentile


def _finding(id, title, level, summary, columns, rows, detail=None, limit=12):
    if not rows:
        return None
    return {"id": id, "title": title, "level": level, "summary": summary, "detail": detail,
            "columns": [{"id": c[0], "header": c[1]} for c in columns],
            "rows": rows[:limit], "total": len(rows)}


def _num(v, places=2):
    return None if v is None else round(float(v), places)


class Insights:
    """`ctx` supplies the live pieces: db, events store, event settings,
    the device list and a status lookup. Kept as a class so the producers
    can share one pass over the poller's state."""

    def __init__(self, db, events, settings, devices, status_for, syslog_seen=None):
        self.db = db
        self.events = events
        self.settings = settings
        self.devices = list(devices or [])
        self.status_for = status_for
        self.syslog_seen = syslog_seen or {}
        self._ids = {d.id for d in self.devices}
        self._name = {d.id: d.name for d in self.devices}

    # --- shared: one pass over the poller's live interface state ------------

    def _live(self):
        """[(device, interface dict)] for every registered device."""
        out = []
        for d in self.devices:
            status = self.status_for(d.id)
            for iface in (status or {}).get("interfaces") or []:
                out.append((d, iface))
        return out

    def run(self):
        producers = [
            self.optics_losing_light, self.optic_margin, self.dark_optics, self.hot_optics,
            self.error_growth, self.busiest_ports, self.undocumented_ports, self.shut_with_optic,
            self.repeat_offenders, self.longest_open, self.event_pattern, self.quiet_syslog,
        ]
        findings, clear, failed = [], [], []
        live = self._live()
        for produce in producers:
            name = produce.__doc__.strip().splitlines()[0] if produce.__doc__ else produce.__name__
            try:
                found = produce(live)
            except Exception:
                log.exception("insight %s failed", produce.__name__)
                failed.append(name)
                continue
            (findings if found else clear).append(found or name)
        order = {"act": 0, "watch": 1, "note": 2}
        findings.sort(key=lambda f: order.get(f["level"], 3))
        return {"findings": findings, "clear": clear, "failed": failed,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "devices": len(self.devices), "interfaces": len(live)}

    # --- optics --------------------------------------------------------------

    def optics_losing_light(self, live):
        """Optics losing light"""
        rows = self.db.query(
            """SELECT device_id, port,
                      avg(value) FILTER (WHERE recorded_at > now() - interval '2 days')  AS recent,
                      avg(value) FILTER (WHERE recorded_at < now() - interval '21 days') AS baseline
                 FROM metric_samples
                WHERE metric = 'optic_rx_power_dbm' AND recorded_at > now() - interval '45 days'
                GROUP BY device_id, port"""
        )
        out = []
        for r in rows:
            if r["device_id"] not in self._ids or r["recent"] is None or r["baseline"] is None:
                continue
            # A dark port reads the module's floor, not a measurement, so
            # its "drift" is meaningless - and it is most of the fleet.
            if r["baseline"] <= DARK_DBM or r["recent"] <= DARK_DBM:
                continue
            change = float(r["recent"]) - float(r["baseline"])
            if change > -DRIFT_DB:
                continue
            out.append({"device": self._name.get(r["device_id"], r["device_id"]), "port": r["port"],
                        "was": _num(r["baseline"]), "now": _num(r["recent"]), "change": _num(change)})
        out.sort(key=lambda x: x["change"])
        return _finding(
            "optic_drift", "Optics losing light", "act",
            f"{len(out)} live link(s) are receiving measurably less light than three weeks ago.",
            [("device", "Device"), ("port", "Port"), ("was", "3 weeks ago (dBm)"), ("now", "Now (dBm)"), ("change", "Change (dB)")],
            out,
            detail="Averages over the last two days against the window more than three weeks back, so a single bad "
                   "reading cannot cause it. Fibre does not recover on its own: this is the one that tells you which "
                   "link to clean or replace before it starts erroring.")

    def _dom_ports(self, live):
        for device, iface in live:
            t = iface.get("transceiver") or {}
            if t.get("present") and t.get("dom_supported"):
                yield device, iface, t

    def optic_margin(self, live):
        """Optical margin on live links"""
        floor = float(self.settings.params_for("optic.rx_power_low").get("floor_dbm", -12))
        out = []
        for device, iface, t in self._dom_ports(live):
            rx = t.get("rx_power_dbm")
            if iface.get("port_state") != "up" or rx is None:
                continue
            out.append({"device": device.name, "port": iface["port"], "description": iface.get("description") or "-",
                        "rx": _num(rx), "margin": _num(float(rx) - floor), "type": t.get("type") or "-"})
        out.sort(key=lambda x: x["margin"])
        return _finding(
            "optic_margin", "How much light each live link has to spare", "note",
            f"{len(out)} live optical link(s), weakest first. The floor is {floor} dBm.",
            [("device", "Device"), ("port", "Port"), ("description", "Description"), ("type", "Type"),
             ("rx", "Rx (dBm)"), ("margin", "Margin (dB)")],
            out,
            detail="Margin is how far the receive power sits above the level that raises a low-light event. The link at "
                   "the top of this list is the one that will fail first.")

    def dark_optics(self, live):
        """Transceivers in ports with no link"""
        out = []
        for device, iface in live:
            t = iface.get("transceiver") or {}
            if not t.get("present") or iface.get("port_state") == "up":
                continue
            out.append({"device": device.name, "port": iface["port"], "state": iface.get("port_state") or "?",
                        "type": t.get("type") or "-", "description": iface.get("description") or "-"})
        return _finding(
            "optic_dark", "Transceivers in ports with nothing on them", "note",
            f"{len(out)} transceiver(s) are installed in ports that have no link.",
            [("device", "Device"), ("port", "Port"), ("state", "Link"), ("type", "Type"), ("description", "Description")],
            out, limit=20,
            detail="Each of these is a module doing nothing: a decommissioned cross-connect, a port shut and forgotten, "
                   "or a spare. Worth reclaiming, and worth knowing about before someone reports a link that was never "
                   "connected. They are also why optical readings are only judged on ports that are up - every one of "
                   "these reads as total light loss.")

    def hot_optics(self, live):
        """Optic temperatures"""
        ceiling = float(self.settings.params_for("optic.temperature").get("ceiling_c", 70))
        out = []
        for device, iface, t in self._dom_ports(live):
            temp = t.get("temperature_c")
            if temp is None:
                continue
            out.append({"device": device.name, "port": iface["port"], "temp": _num(temp, 1),
                        "headroom": _num(ceiling - float(temp), 1), "type": t.get("type") or "-"})
        out.sort(key=lambda x: -x["temp"])
        hottest = out[0]["temp"] if out else 0
        if hottest < ceiling - 20:
            return None   # nothing near the ceiling: not worth a card
        return _finding(
            "optic_hot", "Optics running warm", "watch",
            f"The hottest optic is at {hottest} C, against a {ceiling} C ceiling.",
            [("device", "Device"), ("port", "Port"), ("type", "Type"), ("temp", "Temperature (C)"), ("headroom", "Headroom (C)")],
            out,
            detail="Heat shortens an optic's life and pushes its wavelength around. A module well above its neighbours "
                   "usually means restricted airflow rather than a bad module.")

    # --- interfaces -------------------------------------------------------------

    def error_growth(self, live):
        """Ports with errors climbing"""
        rows = self.db.query(
            """SELECT device_id, port, metric, max(value) - min(value) AS grew, max(value) AS total
                 FROM metric_samples
                WHERE metric IN ('iface_input_errors', 'iface_output_errors')
                  AND recorded_at > now() - interval '7 days'
                GROUP BY device_id, port, metric
               HAVING max(value) > min(value)"""
        )
        out = []
        for r in rows:
            if r["device_id"] not in self._ids:
                continue
            out.append({"device": self._name.get(r["device_id"], r["device_id"]), "port": r["port"],
                        "kind": "input" if r["metric"].endswith("input_errors") else "output",
                        "grew": int(r["grew"]), "total": int(r["total"])})
        out.sort(key=lambda x: -x["grew"])
        return _finding(
            "error_growth", "Ports with errors climbing", "act",
            f"{len(out)} port(s) counted new errors in the last week.",
            [("device", "Device"), ("port", "Port"), ("kind", "Direction"), ("grew", "New this week"), ("total", "Total")],
            out,
            detail="Cumulative counters, so only the increase matters - a port that logged errors once and stopped is "
                   "healthy. Input errors point at the cable, connector or optic; output errors at the far end.")

    def busiest_ports(self, live):
        """Busiest ports"""
        speeds = {}
        for device, iface in live:
            speed = str(iface.get("speed") or "")
            digits = "".join(c for c in speed if c.isdigit())
            if digits:
                speeds[(device.id, iface["port"])] = int(digits)
        rows = self.db.query(
            """SELECT device_id, port, metric, percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95, max(value) AS peak
                 FROM metric_samples
                WHERE metric IN ('iface_input_mbps', 'iface_output_mbps') AND recorded_at > now() - interval '7 days'
                GROUP BY device_id, port, metric HAVING max(value) > 1"""
        )
        best = {}
        for r in rows:
            if r["device_id"] not in self._ids:
                continue
            key = (r["device_id"], r["port"])
            current = best.get(key)
            if current is None or float(r["p95"]) > current["p95_raw"]:
                speed = speeds.get(key)
                best[key] = {"device": self._name.get(r["device_id"], r["device_id"]), "port": r["port"],
                             "direction": "in" if r["metric"].endswith("input_mbps") else "out",
                             "p95": _num(r["p95"], 1), "peak": _num(r["peak"], 1), "p95_raw": float(r["p95"]),
                             "speed": f"{speed} Mbit" if speed else "-",
                             "used": _num(100.0 * float(r["p95"]) / speed, 1) if speed else None}
        out = sorted(best.values(), key=lambda x: -x["p95_raw"])
        for row in out:
            row.pop("p95_raw", None)
        busy = [r for r in out if (r["used"] or 0) >= UTILISATION_WATCH]
        return _finding(
            "busiest", "Where the traffic is", "watch" if busy else "note",
            (f"{len(busy)} port(s) run at or above {UTILISATION_WATCH}% of link speed." if busy
             else "Busiest ports over the last week, by 95th-percentile throughput."),
            [("device", "Device"), ("port", "Port"), ("direction", "Direction"), ("speed", "Link"),
             ("p95", "p95 (Mbps)"), ("peak", "Peak (Mbps)"), ("used", "p95 of link (%)")],
            out, limit=10,
            detail="The 95th percentile rather than the peak, so one burst does not make a port look saturated. A port "
                   "sitting high here is the one to widen or move traffic off before it starts discarding.")

    def undocumented_ports(self, live):
        """Live ports with no description"""
        out = []
        for device, iface in live:
            if iface.get("port_state") != "up" or (iface.get("description") or "").strip():
                continue
            out.append({"device": device.name, "port": iface["port"],
                        "speed": iface.get("speed") or "-",
                        "traffic": _num(float(iface.get("input_mbps") or 0) + float(iface.get("output_mbps") or 0), 1)})
        out.sort(key=lambda x: -(x["traffic"] or 0))
        return _finding(
            "undocumented", "Live ports nobody labelled", "note",
            f"{len(out)} port(s) are up with no description set, busiest first.",
            [("device", "Device"), ("port", "Port"), ("speed", "Link"), ("traffic", "Traffic now (Mbps)")],
            out, limit=15,
            detail="A description is the only thing that tells the next person what unplugging this port would break. "
                   "The ones carrying traffic are worth labelling first; a device that reports no rates (the EX3300) "
                   "shows zero rather than a guess.")

    def shut_with_optic(self, live):
        """Shut ports with a transceiver installed"""
        out = []
        for device, iface in live:
            t = iface.get("transceiver") or {}
            if iface.get("port_state") != "admin_down" or not t.get("present"):
                continue
            out.append({"device": device.name, "port": iface["port"], "type": t.get("type") or "-",
                        "description": iface.get("description") or "-"})
        return _finding(
            "shut_with_optic", "Ports shut with a module still in them", "note",
            f"{len(out)} administratively-down port(s) still have a transceiver.",
            [("device", "Device"), ("port", "Port"), ("type", "Type"), ("description", "Description")],
            out,
            detail="Someone shut the port and left the optic. Either it is staged for something, or it is a module that "
                   "could be back in the spares drawer.")

    # --- events ------------------------------------------------------------------

    def repeat_offenders(self, live):
        """Things that keep coming back"""
        rows = self.db.query(
            """SELECT device, subject, kind, sum(count) AS reports, sum(reopen_count) AS returns, count(*) AS episodes,
                      max(coalesce(reopened_at, raised_at)) AS last_seen
                 FROM events WHERE raised_at > now() - interval '7 days'
                GROUP BY device, subject, kind
               HAVING sum(reopen_count) > 0 OR count(*) > 1"""
        )
        out = [{"device": r["device"], "subject": r["subject"] or "-", "kind": r["kind"],
                "episodes": int(r["episodes"]), "returns": int(r["returns"] or 0), "reports": int(r["reports"]),
                "last": r["last_seen"].isoformat() if r["last_seen"] else None,
                "_sort": int(r["returns"] or 0) + int(r["episodes"])} for r in rows]
        out.sort(key=lambda x: -x["_sort"])
        for row in out:
            row.pop("_sort", None)
        return _finding(
            "repeat_offenders", "Things that keep coming back", "watch",
            f"{len(out)} thing(s) have gone wrong more than once this week.",
            [("device", "Device"), ("subject", "What"), ("kind", "Event"), ("episodes", "Episodes"),
             ("returns", "Returns"), ("reports", "Reports"), ("last", "Last seen")],
            out,
            detail="An intermittent fault is worse than a hard one: it clears before anyone looks, and it is the reason "
                   "a returning fault re-opens its event instead of filling the list with new rows.")

    def longest_open(self, live):
        """Events still open"""
        rows = self.db.query(
            """SELECT id, device, subject, kind, severity, raised_at, count
                 FROM events WHERE resolved_at IS NULL AND raised_at < now() - interval '1 hour'
                ORDER BY raised_at"""
        )
        now = datetime.now(timezone.utc)
        out = []
        for r in rows:
            raised = r["raised_at"]
            raised = raised if raised.tzinfo else raised.replace(tzinfo=timezone.utc)
            hours = (now - raised).total_seconds() / 3600.0
            out.append({"id": r["id"], "device": r["device"], "subject": r["subject"] or "-", "kind": r["kind"],
                        "severity": r["severity"], "open_for": f"{hours:.0f} h" if hours < 48 else f"{hours/24:.0f} d",
                        "reports": int(r["count"])})
        return _finding(
            "long_open", "Open for more than an hour", "act",
            f"{len(out)} event(s) have been open for over an hour.",
            [("severity", "Severity"), ("device", "Device"), ("subject", "What"), ("kind", "Event"),
             ("open_for", "Open for"), ("reports", "Reports")],
            out,
            detail="Nothing here acknowledges events, so anything still open is still true as far as the devices are "
                   "concerned - or it is something that will never clear itself and wants resolving by hand.")

    def event_pattern(self, live):
        """What has been happening"""
        rows = self.db.query(
            """SELECT kind, severity, count(*) AS episodes, count(DISTINCT device) AS devices
                 FROM events WHERE raised_at > now() - interval '7 days'
                GROUP BY kind, severity ORDER BY count(*) DESC"""
        )
        out = [{"kind": r["kind"], "severity": r["severity"], "episodes": int(r["episodes"]),
                "devices": int(r["devices"])} for r in rows]
        return _finding(
            "event_pattern", "What has been happening this week", "note",
            f"{sum(r['episodes'] for r in out)} event(s) across {len(out)} kind(s) in the last seven days.",
            [("kind", "Event"), ("severity", "Severity"), ("episodes", "Episodes"), ("devices", "Devices")],
            out, limit=15,
            detail="The shape of a week. A kind that dominates is either a real recurring problem or a severity setting "
                   "that wants changing on the Catalogue tab.")

    def quiet_syslog(self, live):
        """Devices sending syslog"""
        now = datetime.now(timezone.utc)
        out = []
        for d in self.devices:
            seen = self.syslog_seen.get(d.host) or self.syslog_seen.get(d.name)
            if seen is None:
                out.append({"device": d.name, "host": d.host, "last": "nothing since Switchboard started"})
            else:
                hours = (now - seen).total_seconds() / 3600.0
                if hours >= 6:
                    out.append({"device": d.name, "host": d.host, "last": f"{hours:.0f} h ago"})
        return _finding(
            "quiet_syslog", "Devices not sending syslog", "watch",
            f"{len(out)} device(s) have sent no syslog recently.",
            [("device", "Device"), ("host", "Address"), ("last", "Last line")],
            out,
            detail="A silent device is not necessarily a healthy one. Without syslog, everything about it is detected by "
                   "the SSH poll instead - seconds to minutes later, and nothing at all for the things only it reports.")
