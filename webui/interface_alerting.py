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

    # Alertmanager's resolve_timeout (global, alertmanager.yml) is 5m - an
    # alert that's never refreshed just quietly expires there even though
    # the underlying condition never cleared. Prometheus-rule alerts don't
    # have this problem because Prometheus re-sends its whole currently-
    # firing set every evaluation cycle; a directly-posted alert (this
    # module) only gets sent once on the transition unless something
    # re-affirms it. HEARTBEAT_SECONDS is that re-affirmation - comfortably
    # under resolve_timeout, and cheap (posting the same labels again is a
    # no-op refresh to Alertmanager, not a duplicate notification).
    HEARTBEAT_SECONDS = 120

    def __init__(self):
        self._down_since = {}  # (device_id, port) -> monotonic timestamp
        self._alerting = set()  # (device_id, port) currently alerting
        self._last_posted = {}  # (device_id, port) -> monotonic time of last fire/heartbeat POST
        self._last_syslog_ts_ns = 0  # dedup cursor for check_via_syslog
        self._last_seen_poll_at = {}  # (device_id, port) -> last status_poller "last_polled" considered

    def check_once(self, configs, get_port_state, device_name_for, alertmanager):
        """Owns delayed-mode timing end to end (its down_since tracking is
        what "stayed down for N seconds" means) - both fire and resolve.

        For immediate mode, this is deliberately fire-only, never resolve
        - resolving is check_via_syslog's job (and reconcile_via_poll's,
        for the missed-syslog-event case - see that method's docstring
        for why *it* can safely resolve where this can't). Confirmed live
        why both halves of that split matter: (1) without any role here,
        a port already down before a webui restart (no *new* syslog
        transition to react to - check_via_syslog only sees fresh events)
        would silently never alert at all; (2) but letting this loop also
        resolve is what caused a real race in the first place -
        check_via_syslog correctly fired 3s after a genuine down event,
        then this loop's next ~30s SSH poll sampled the status poller's
        own cache mid-transition, read a stale "up", and incorrectly
        resolved the alert the syslog path had just raised, which then
        re-fired on the next syslog tick (looked like "took a minute" for
        what was actually a 3s real detection). Firing here is safe to
        duplicate (already_alerting guards it) and safe to be occasionally
        premature (worst case: alerts a real transient blip a syslog
        "up" will resolve moments later) - resolving here was the actual
        hazard, since a stale-read false resolve silently un-alerts a
        real ongoing outage."""
        now = time.monotonic()
        for cfg in configs:
            key = (cfg["device_id"], cfg["port"])
            if not cfg["enabled"]:
                # Disabling a port mid-alert must still resolve it right
                # away, regardless of mode - the fast path only resolves
                # on a real "up" syslog event, which disabling doesn't
                # wait for.
                self._down_since.pop(key, None)
                if key in self._alerting:
                    self._resolve(cfg, alertmanager, device_name_for)
                    self._alerting.discard(key)
                    self._last_posted.pop(key, None)
                    self._last_seen_poll_at.pop(key, None)
                continue

            state = get_port_state(cfg["device_id"], cfg["port"])
            is_down = state == "down"  # "admin_down" is intentional, never alerted

            if cfg["mode"] == "immediate":
                if is_down and key not in self._alerting:
                    self._down_since.setdefault(key, now)
                    self._fire(cfg, alertmanager, device_name_for, now - self._down_since[key])
                    self._alerting.add(key)
                    self._last_posted[key] = now
                    self._last_seen_poll_at.pop(key, None)
                elif is_down and key in self._alerting:
                    self._maybe_heartbeat(key, cfg, alertmanager, device_name_for, now)
                elif not is_down:
                    self._down_since.pop(key, None)
                continue  # resolve is check_via_syslog's job alone

            if not is_down:
                self._down_since.pop(key, None)
                if key in self._alerting:
                    self._resolve(cfg, alertmanager, device_name_for)
                    self._alerting.discard(key)
                    self._last_posted.pop(key, None)
                continue

            if key not in self._down_since:
                self._down_since[key] = now
            down_for = now - self._down_since[key]

            if down_for >= cfg["delay_seconds"]:
                if key not in self._alerting:
                    self._fire(cfg, alertmanager, device_name_for, down_for)
                    self._alerting.add(key)
                    self._last_posted[key] = now
                else:
                    self._maybe_heartbeat(key, cfg, alertmanager, device_name_for, now)

    def _maybe_heartbeat(self, key, cfg, alertmanager, device_name_for, now):
        """Re-POSTs an already-firing alert's labels periodically, purely
        to refresh Alertmanager's resolve_timeout clock - confirmed live
        this matters: an Alertmanager restart (or anything that drops its
        in-memory alert store) silently loses a directly-posted alert with
        no way for it to ask for a resend, unlike Prometheus-rule alerts,
        which Prometheus itself continuously re-sends every evaluation
        cycle regardless of Alertmanager's state. Deliberately quiet - no
        "fired" log line - this is upkeep, not a new event."""
        last = self._last_posted.get(key, 0)
        if now - last >= self.HEARTBEAT_SECONDS:
            down_for = now - self._down_since.get(key, now)
            self._fire(cfg, alertmanager, device_name_for, down_for, log_it=False)
            self._last_posted[key] = now

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
        N seconds" actually means; check_once remains the fire-only
        fallback for immediate mode if Loki itself is unreachable, and
        reconcile_via_poll is the resolve-side fallback for a missed
        syslog "up" event specifically.

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
            # Deliberately unconditional on self._alerting here - confirmed
            # live this matters during rapid flapping: check_once's poll
            # path can fire on a stale SSH-poll sample that doesn't match
            # real-time reality (a false positive during a fast up/down/
            # up/down sequence), which sets _alerting=True with nothing
            # real backing it. If a genuine new "down" then arrived here
            # gated on "not already alerting", it would be silently
            # swallowed - a real down event producing no alert at all,
            # because bookkeeping from an unrelated false fire said
            # "already handled". Syslog is authoritative ground truth for
            # its own events; posting again is a safe, idempotent refresh
            # to Alertmanager either way (same reasoning as the heartbeat).
            now = time.monotonic()
            if link_state == "down":
                self._down_since[key] = now
                self._fire(cfg, alertmanager, device_name_for, 0)
                self._alerting.add(key)
                self._last_posted[key] = now
                self._last_seen_poll_at.pop(key, None)
            else:
                self._down_since.pop(key, None)
                self._resolve(cfg, alertmanager, device_name_for)
                self._alerting.discard(key)
                self._last_posted.pop(key, None)
                self._last_seen_poll_at.pop(key, None)
        self._last_syslog_ts_ns = newest_seen

    def reconcile_via_poll(self, configs, get_state_and_polled_at, device_name_for, alertmanager):
        """Safety net for immediate-mode ports, run on a tight ~5s loop
        (see app.py): if a *fresh* status-poller SSH poll (status_poller.py's
        own independent 30s cycle) shows the port genuinely up, resolves
        the alert. Exists specifically for what check_via_syslog can't
        cover on its own - a missed/dropped syslog "up" event (Vector
        hiccup, a Loki ingestion gap) would otherwise leave an alert stuck
        firing forever, since resolve is normally syslog's exclusive job
        for immediate mode (see check_once's docstring for why a bare
        "trust whatever's polled" design was removed in the first place:
        a *stale* read can falsely resolve a real ongoing outage).

        The safety here is in what counts as "fresh": each config's
        last-considered `last_polled` timestamp is tracked, and a resolve
        only fires when the observed `last_polled` has actually advanced
        past what was last seen *and* now reads "up". A snapshot from
        before the alert even started can never trigger this - it's only
        ever comparing against poll results that landed after the alert
        began (the `_last_seen_poll_at` entry for a key is cleared every
        time that key starts a fresh alerting episode, in both check_once
        and check_via_syslog) - so this doesn't reopen the original
        stale-read hazard, it only reacts to genuinely new information.

        Deliberately does NOT require `key in self._alerting` any more
        (confirmed live this was a real bug, not just theoretical): this
        module's tracking state is in-memory only and is wiped on every
        webui restart (see class docstring). A restart mid-outage orphaned
        a real, still-active Alertmanager alert - nothing resolved it or
        kept it heartbeating, so it sat firing (paging PagerDuty) for
        several extra minutes past the port's real ~30s recovery, until
        Alertmanager's own resolve_timeout silently expired it. Now this
        loop treats a fresh poll as ground truth regardless of what our
        bookkeeping thinks: an unexpectedly-up port gets resolved (a
        resolve for an alert Alertmanager doesn't have active is a safe
        no-op), and an unexpectedly-down port gets its fire/heartbeat
        bookkeeping re-established, so a lost-state restart converges back
        to reality within one poll cycle instead of drifting until either
        a fresh syslog event or a 5-minute timeout happens to fix it."""
        now = time.monotonic()
        for cfg in configs:
            if not cfg["enabled"] or cfg["mode"] != "immediate":
                continue
            key = (cfg["device_id"], cfg["port"])
            state, polled_at = get_state_and_polled_at(cfg["device_id"], cfg["port"])
            if polled_at is None or state is None:
                continue
            if self._last_seen_poll_at.get(key) == polled_at:
                continue  # same snapshot as last check - nothing new learned
            self._last_seen_poll_at[key] = polled_at
            if state != "down":
                if key in self._alerting:
                    log.info(
                        "interface alert reconciled via poll (missed syslog up?): %s/%s",
                        cfg["device_id"], cfg["port"],
                    )
                    self._resolve(cfg, alertmanager, device_name_for)
                    self._alerting.discard(key)
                    self._down_since.pop(key, None)
                    self._last_posted.pop(key, None)
                    self._last_seen_poll_at.pop(key, None)
            elif key not in self._alerting:
                log.info(
                    "interface alert re-armed via poll (state lost, e.g. restart): %s/%s",
                    cfg["device_id"], cfg["port"],
                )
                self._down_since.setdefault(key, now)
                self._fire(cfg, alertmanager, device_name_for, 0)
                self._alerting.add(key)
                self._last_posted[key] = now

    def forget(self, device_id, port):
        """Drops all tracking for one port, so a manual resolve from the UI
        (app.py's /api/alerts/resolve) isn't immediately undone by this
        module's own heartbeat re-posting the alert it just cleared.

        Note this does not, and should not, *suppress* the port: if it's
        genuinely still down, the next fresh poll re-arms it via
        reconcile_via_poll and it fires again. Manually resolving a real,
        ongoing fault is meant to be temporary - the tool for "stop telling
        me about this known problem" is a maintenance window (silence),
        which is time-boxed and leaves an auditable reason, rather than a
        one-click way to permanently hide a live fault."""
        key = (device_id, port)
        self._alerting.discard(key)
        self._down_since.pop(key, None)
        self._last_posted.pop(key, None)
        self._last_seen_poll_at.pop(key, None)

    def pending_entries(self, configs, device_name_for):
        """Delayed-mode ports currently counting down toward their
        `delay_seconds` confirmation window (down, but not yet fired) - the
        interface-alerting equivalent of a Prometheus rule's "pending"
        state (see app.py's /api/alerts/live, which surfaces both so a
        currently-down-but-not-yet-alerted condition is visible in the UI
        instead of looking like nothing is happening, the exact confusion
        that came up over a Prometheus rule's own pending window). Immediate
        mode has no pending phase - it fires the instant a down event is
        seen - so this only ever reports delayed-mode configs."""
        now = time.monotonic()
        out = []
        for cfg in configs:
            if not cfg["enabled"] or cfg["mode"] != "delayed":
                continue
            key = (cfg["device_id"], cfg["port"])
            if key in self._alerting or key not in self._down_since:
                continue
            down_for = now - self._down_since[key]
            if down_for >= cfg["delay_seconds"]:
                continue  # about to fire on the next check_once tick
            out.append({
                "labels": {
                    "alertname": "InterfaceDown",
                    "device_id": cfg["device_id"],
                    "port": cfg["port"],
                    "severity": cfg["severity"],
                },
                "annotations": {
                    "summary": f"{cfg['port']} on {device_name_for(cfg['device_id'])} is down",
                    "description": f"Down for {int(down_for)}s - alerts at {cfg['delay_seconds']}s if it doesn't recover first.",
                },
                "status": {"state": "pending"},
                "startsAt": datetime.now(timezone.utc).isoformat(),
            })
        return out

    def reseed_from_alertmanager(self, alertmanager):
        """Called once at process startup (see app.py) - repopulates
        `_alerting`/`_down_since`/`_last_posted` from whatever InterfaceDown
        alerts are genuinely still active in Alertmanager right now, so a
        webui restart doesn't leave those alerts orphaned (heartbeat-less
        and untracked) until reconcile_via_poll's next fresh-poll tick or a
        new syslog event happens to rediscover them. Best-effort: if
        Alertmanager isn't reachable yet at startup, the self-healing in
        reconcile_via_poll (and check_via_syslog for a fresh down/up event)
        still converges on its own within one poll cycle - this just makes
        the common case (Alertmanager already up) instant instead of
        leaving a real alert briefly unheartbeated after every restart."""
        try:
            alerts = alertmanager.list_alerts()
        except Exception:
            log.warning("could not reseed interface-alert state from Alertmanager", exc_info=True)
            return
        now = time.monotonic()
        seeded = 0
        for alert in alerts or []:
            labels = alert.get("labels", {})
            if labels.get("alertname") != "InterfaceDown":
                continue
            if alert.get("status", {}).get("state") != "active":
                continue
            key = (labels.get("device_id"), labels.get("port"))
            if None in key:
                continue
            self._alerting.add(key)
            self._down_since.setdefault(key, now)
            self._last_posted[key] = now
            seeded += 1
        if seeded:
            log.info("interface alert state reseeded from Alertmanager: %d alert(s) still active", seeded)

    def _fire(self, cfg, alertmanager, device_name_for, down_for, log_it=True):
        starts_at = datetime.now(timezone.utc).isoformat()
        labels = {
            "alertname": "InterfaceDown",
            "device_id": cfg["device_id"],
            "port": cfg["port"],
            "severity": cfg["severity"],
        }
        try:
            alertmanager.post_alerts([{
                "labels": labels,
                "annotations": {
                    "summary": f"{cfg['port']} on {device_name_for(cfg['device_id'])} is down",
                    "description": (
                        f"Configured for immediate alerting" if cfg["mode"] == "immediate"
                        else f"Down for {int(down_for)}s (>= {cfg['delay_seconds']}s configured delay)"
                    ),
                },
                "startsAt": starts_at,
            }])
            if log_it:
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
