"""Per-interface down-alerting (ROADMAP 3.2's Interfaces tab). Unlike
prometheus/alerts.yml's fleet-wide rules (evaluated by Prometheus itself),
this is deliberately per-(device, port): most ports on a switch are
legitimately unused, so alerting "any interface down" fleet-wide is just
noise (the same lesson the original S4048TransceiverAlarm bug taught) -
here, a human explicitly opts specific ports in, and chooses per-port
whether a down transition alerts immediately or only after staying down
for a configurable delay (checked again at that point, not just slept -
a port that bounced back up before the delay elapsed never alerts).

Evaluated by a background loop in app.py that reuses status_poller.py's
already-polled interface state (no extra SSH round trips) and posts
straight to Alertmanager's /api/v2/alerts - a dynamic, per-port rule set
isn't something a static Prometheus rules file is a good fit for, but
posting directly still puts these alerts through the exact same
receivers/silences/Pushover pipeline as everything else.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webui.interface_alerting")

MODES = ("immediate", "delayed")


class InterfaceAlertConfigStore:
    def __init__(self, db):
        self.db = db

    def list(self, device_id=None):
        if device_id is not None:
            rows = self.db.query(
                "SELECT * FROM interface_alert_rules WHERE device_id = %s ORDER BY port", (device_id,)
            )
        else:
            rows = self.db.query("SELECT * FROM interface_alert_rules ORDER BY device_id, port")
        return [self._to_dict(r) for r in rows]

    def get(self, device_id, port):
        row = self.db.query_one(
            "SELECT * FROM interface_alert_rules WHERE device_id = %s AND port = %s", (device_id, port)
        )
        return self._to_dict(row) if row else None

    def upsert(self, device_id, port, enabled, mode, delay_seconds, severity="warning"):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if delay_seconds < 5:
            raise ValueError("delay_seconds must be at least 5")
        if severity not in ("warning", "critical"):
            raise ValueError("severity must be 'warning' or 'critical'")
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO interface_alert_rules (device_id, port, enabled, mode, delay_seconds, severity, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (device_id, port) DO UPDATE SET
                 enabled = EXCLUDED.enabled, mode = EXCLUDED.mode,
                 delay_seconds = EXCLUDED.delay_seconds, severity = EXCLUDED.severity,
                 updated_at = EXCLUDED.updated_at""",
            (device_id, port, 1 if enabled else 0, mode, delay_seconds, severity, now),
        )
        return self.get(device_id, port)

    @staticmethod
    def _to_dict(row):
        return {
            "device_id": row["device_id"],
            "port": row["port"],
            "enabled": bool(row["enabled"]),
            "mode": row["mode"],
            "delay_seconds": row["delay_seconds"],
            "severity": row["severity"],
            "updated_at": row["updated_at"],
        }


class InterfaceAlertChecker:
    """Holds the in-memory down-tracking state across ticks - deliberately
    not persisted: a webui restart re-observes real current state on the
    next tick, which is simpler and just as correct as trying to recover
    "how long has this been down" from before a restart."""

    def __init__(self):
        self._down_since = {}  # (device_id, port) -> monotonic timestamp
        self._alerting = set()  # (device_id, port) currently alerting
        self._last_syslog_ts_ns = 0  # dedup cursor for check_via_syslog

    def check_once(self, configs, get_port_state, device_name_for, alertmanager):
        now = time.monotonic()
        for cfg in configs:
            key = (cfg["device_id"], cfg["port"])
            if not cfg["enabled"]:
                # Disabling a port mid-alert must still resolve it -
                # otherwise it's stuck firing in Alertmanager forever,
                # since a disabled config is never checked again to
                # notice the interface recovered.
                self._down_since.pop(key, None)
                if key in self._alerting:
                    self._resolve(cfg, alertmanager, device_name_for)
                    self._alerting.discard(key)
                continue
            state = get_port_state(cfg["device_id"], cfg["port"])
            is_down = state == "down"  # "admin_down" is intentional, never alerted

            if not is_down:
                self._down_since.pop(key, None)
                if key in self._alerting:
                    self._resolve(cfg, alertmanager, device_name_for)
                    self._alerting.discard(key)
                continue

            if key not in self._down_since:
                self._down_since[key] = now
            down_for = now - self._down_since[key]

            already_alerting = key in self._alerting
            should_alert = cfg["mode"] == "immediate" or down_for >= cfg["delay_seconds"]
            if should_alert and not already_alerting:
                self._fire(cfg, alertmanager, device_name_for, down_for)
                self._alerting.add(key)

    def check_via_syslog(self, configs, loki_client, devices_by_id, alertmanager, device_name_for, lookback_seconds=20):
        """Near-real-time detection for `mode="immediate"` configs, using
        the interface link-state events Vector already ships to Loki in
        real time (syslog/vector.yaml's `.link_event`/`.interface`/
        `.link_state` fields - no new parsing, this reuses exactly what
        the Console's Syslog tab already reads). Runs on a much tighter
        loop than check_once (see app.py) since it's a single cheap Loki
        query, not an SSH round trip - typically only a second or two
        behind the real device event instead of check_once's ~30s poll
        bound. Delayed-mode configs are deliberately left to check_once,
        whose sustained down_since tracking is what "stayed down for
        N seconds" actually means; check_once also remains the fallback
        for immediate mode if Loki itself is unreachable.

        `_last_syslog_ts_ns` is a monotonically-advancing cursor so the
        same already-processed event (log lines don't disappear from the
        query window between ticks) never fires twice."""
        immediate_by_key = {
            (c["device_id"], c["port"]): c for c in configs if c["enabled"] and c["mode"] == "immediate"
        }
        if not immediate_by_key:
            return
        host_to_device_id = {d.host: d.id for d in devices_by_id.values()}
        try:
            events = loki_client.query_range(filters=['link_event="true"'], limit=100, since_seconds=lookback_seconds)
        except Exception:
            log.warning("syslog-based interface check skipped: Loki unreachable", exc_info=True)
            return

        newest_seen = self._last_syslog_ts_ns
        for event in events:
            ts_ns = int(event.get("_timestamp_ns", 0))
            if ts_ns <= self._last_syslog_ts_ns:
                continue
            newest_seen = max(newest_seen, ts_ns)
            port = event.get("interface")
            link_state = event.get("link_state")
            if not port or link_state not in ("up", "down"):
                continue
            host = event.get("source_ip") or event.get("device_host")
            device_id = host_to_device_id.get(host)
            if device_id is None:
                continue
            key = (device_id, port)
            cfg = immediate_by_key.get(key)
            if cfg is None:
                continue
            if link_state == "down":
                if key not in self._alerting:
                    self._down_since[key] = time.monotonic()
                    self._fire(cfg, alertmanager, device_name_for, 0)
                    self._alerting.add(key)
            else:
                self._down_since.pop(key, None)
                if key in self._alerting:
                    self._resolve(cfg, alertmanager, device_name_for)
                    self._alerting.discard(key)
        self._last_syslog_ts_ns = newest_seen

    def _fire(self, cfg, alertmanager, device_name_for, down_for):
        starts_at = datetime.now(timezone.utc).isoformat()
        try:
            alertmanager.post_alerts([{
                "labels": {
                    "alertname": "InterfaceDown",
                    "device_id": cfg["device_id"],
                    "port": cfg["port"],
                    "severity": cfg["severity"],
                },
                "annotations": {
                    "summary": f"{cfg['port']} on {device_name_for(cfg['device_id'])} is down",
                    "description": (
                        f"Configured for immediate alerting" if cfg["mode"] == "immediate"
                        else f"Down for {int(down_for)}s (>= {cfg['delay_seconds']}s configured delay)"
                    ),
                },
                "startsAt": starts_at,
            }])
            log.info("interface alert fired: %s/%s (mode=%s)", cfg["device_id"], cfg["port"], cfg["mode"])
        except Exception:
            log.exception("could not post interface-down alert for %s/%s", cfg["device_id"], cfg["port"])

    def _resolve(self, cfg, alertmanager, device_name_for):
        now = datetime.now(timezone.utc)
        try:
            alertmanager.post_alerts([{
                "labels": {
                    "alertname": "InterfaceDown",
                    "device_id": cfg["device_id"],
                    "port": cfg["port"],
                    "severity": cfg["severity"],
                },
                "annotations": {"summary": f"{cfg['port']} on {device_name_for(cfg['device_id'])} recovered"},
                "startsAt": (now - timedelta(minutes=1)).isoformat(),
                "endsAt": now.isoformat(),
            }])
            log.info("interface alert resolved: %s/%s", cfg["device_id"], cfg["port"])
        except Exception:
            log.exception("could not resolve interface-down alert for %s/%s", cfg["device_id"], cfg["port"])
