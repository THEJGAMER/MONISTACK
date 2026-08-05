"""Fan/PSU hardware alerting - syslog-primary, SSH-poll fallback. Mirrors
interface_alerting.py's design closely (same three-loop shape, wired the
same way in app.py: a fast syslog-tail-equivalent, a slower poll-based
fire path, and a reconcile loop that's the safety net for whatever the
syslog path misses), for the same reason: Vector already ships fan/PSU
syslog events (CHMGR/ENVMON/RPM/OSTATE facilities) to Loki in real time,
so a Loki poll is a much tighter detection loop than waiting on the SSH
poll cycle - but syslog can be missed (a Vector hiccup, a Loki ingestion
gap, or a device that was simply never configured to send syslog at all),
so nothing here is ever solely dependent on it. Unlike interface alerting,
this has no per-entity opt-in config table - every device's fans/PSUs are
always alerted on, matching the always-on Prometheus rules
(S4048FanDown/S4048PSUDown) this supplements, not replaces.

`_classify_alarm` is moved here from app.py (which now imports it from
here for /api/devices/{id}/alarm-history) rather than gaining a third
independent implementation - it was already a deliberate *second*
implementation of syslog/vector.yaml's own VRL alarm-normalization block
(see its docstring for why: two independently-verified implementations
survive a Vector regression, a third adds no more safety, just more
places for the same bug to need fixing).
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webui.hardware_alerting")

ALERTNAME = "HardwareAlarm"


def _classify_alarm(detail: str) -> dict:
    """Python mirror of syslog/vector.yaml's alarm-normalization VRL block.

    This is a deliberate second implementation, not a refactor to share
    code with Vector: relying solely on Vector to have tagged an event at
    ingestion time means a Vector regression (missing category, unshipped
    config change, etc - this exact thing happened once already, see
    syslog/README.md changelog) silently empties this whole feature with
    no error anywhere. Classifying again here from the raw `facility`/
    `detail` fields - which the interpreter has reliably extracted since
    day one - means Alarm History (and now live alerting) keeps working
    even if Vector's own alarm_severity/alarm_component fields are missing
    or wrong, and also lets it recover alarm history for events ingested
    before the Vector fix landed, which otherwise has no alarm_severity at
    all."""
    detail_lower = detail.lower()
    severity = None
    active = None
    if "cleared" in detail_lower:
        active = False
    elif "major alarm" in detail_lower:
        severity, active = "critical", True
    elif "minor alarm" in detail_lower:
        severity, active = "minor", True
    elif "is down" in detail_lower or "is removed" in detail_lower:
        severity, active = "minor", True
    elif "is up" in detail_lower or "is inserted" in detail_lower:
        active = False

    component = None
    if severity is not None or active is False:
        component = detail
        component = re.sub(r"(?i)^(major|minor)\s+alarm\s+cleared\s*:?\s*", "", component)
        component = re.sub(r"(?i)^(major alarm:\s*|minor alarm\s*:\s*)", "", component)
        component = re.sub(r"(?i)\s+is\s+(up|down|inserted|removed)\s*$", "", component)
        component = re.sub(r"(?i)\s+(alarm\s+)?reported(\s+in\s+unit\s+\d+)?\s+is\s+cleared\s*$", "", component)
        component = component.strip()

    return {"alarm_severity": severity, "alarm_active": active, "alarm_component": component}


# Extracts (kind, bay, unit) from _classify_alarm's stripped component
# text, e.g. "Power supply 2 in unit 1" -> ("psu", "2", "1"). The PSU
# pattern is based on real captured syslog text (see syslog/tests/
# test_vrl.py, "%CHMGR-0-PS_DOWN: Major alarm: Power supply 2 in unit 1 is
# down"). The fan pattern is NOT independently verified against a real
# fan-fault syslog line - the exact wording was never captured verbatim
# anywhere in this repo (confirmed by search), only inferred from the PSU
# message's structure. If it doesn't match a real device's actual wording,
# fan syslog events simply won't parse here and fall through unmatched
# (logged at debug, not silently wrong) - reconcile_via_poll doesn't
# depend on this regex at all (it reads unit/bay directly from
# status_poller's structured show-environment data), so a wrong guess
# here degrades to "detected within one poll cycle" rather than "never
# detected".
_COMPONENT_RE = re.compile(
    r"(?i)(power\s*suppl\w*|fan\s*(?:tray)?)\s*(\d+)\s+in\s+unit\s+(\d+)"
)
_KIND_MAP = {"psu": "psu"}  # normalized below; matched prefix -> kind


def _parse_component(component):
    """Returns (kind, unit, bay) or None if the text doesn't match the
    known shape - see _COMPONENT_RE's comment above for why a fan-text
    mismatch is an expected, handled case, not a bug to fix blindly."""
    if not component:
        return None
    m = _COMPONENT_RE.search(component)
    if not m:
        return None
    prefix, bay, unit = m.groups()
    kind = "psu" if prefix.lower().startswith("power") else "fan"
    return kind, unit, bay


class HardwareAlertChecker:
    """Holds in-memory fan/PSU alarm state across ticks - deliberately not
    persisted, same reasoning as InterfaceAlertChecker: a webui restart
    just re-observes real current state on the next tick."""

    HEARTBEAT_SECONDS = 120  # see InterfaceAlertChecker's own constant - same Alertmanager resolve_timeout reasoning

    def __init__(self):
        self._alerting = {}  # (device_id, kind, unit, bay) -> severity, so _resolve can reuse the same severity it fired with
        self._last_posted = {}  # (device_id, kind, unit, bay) -> monotonic time of last fire/heartbeat POST
        self._last_syslog_ts_ns = 0  # dedup cursor for check_via_syslog
        self._last_seen_poll_at = {}  # device_id -> last status_poller "last_polled" considered

    def check_via_syslog(self, loki_client, devices_by_id, alertmanager, device_name_for, lookback_seconds=20):
        """Near-real-time detection via the same CHMGR/ENVMON/RPM/OSTATE
        syslog events /api/devices/{id}/alarm-history already reads (see
        app.py) - Vector ships these to Loki in real time, so this is a
        single cheap Loki query on a tight loop (see app.py's wiring)
        rather than waiting on the SSH poll cycle. reconcile_via_poll is
        the fallback for whatever this misses."""
        host_to_device_id = {d.host: d.id for d in devices_by_id.values()}
        try:
            events = loki_client.query_range(
                filters=['facility=~"CHMGR|ENVMON|RPM|OSTATE"'], limit=100, since_seconds=lookback_seconds
            )
        except Exception:
            log.warning("syslog-based hardware check skipped: Loki unreachable", exc_info=True)
            return

        newest_seen = self._last_syslog_ts_ns
        for event in events:
            ts_ns = int(event.get("_timestamp_ns", 0))
            if ts_ns <= self._last_syslog_ts_ns:
                continue
            newest_seen = max(newest_seen, ts_ns)
            detail = event.get("detail") or event.get("message") or ""
            classified = _classify_alarm(detail)
            if classified["alarm_severity"] is None and classified["alarm_active"] is not False:
                continue  # not alarm-relevant text (e.g. fan-speed-% telemetry)
            parsed = _parse_component(classified["alarm_component"])
            if parsed is None:
                log.debug("hardware syslog event did not match known component shape: %r", detail)
                continue
            kind, unit, bay = parsed
            host = event.get("source_ip") or event.get("device_host")
            device_id = host_to_device_id.get(host)
            if device_id is None:
                continue
            key = (device_id, kind, unit, bay)
            now = time.monotonic()
            if classified["alarm_active"]:
                self._fire(key, classified["alarm_severity"], alertmanager, device_name_for)
                self._last_posted[key] = now
            elif key in self._alerting:
                self._resolve(key, alertmanager, device_name_for)
                self._last_posted.pop(key, None)
        self._last_syslog_ts_ns = newest_seen

    def reconcile_via_poll(self, device_ids, get_env_and_polled_at, device_name_for, alertmanager):
        """Poll-fallback safety net, run on a tight loop (see app.py) -
        reads status_poller.py's already-cached `show environment` state
        (no extra SSH) and reconciles against it, exactly the same
        "only react to a genuinely fresh poll" design as
        InterfaceAlertChecker.reconcile_via_poll (see its docstring for
        the full stale-read-hazard reasoning this mirrors). This is what
        makes fan/PSU alerting work for a device with no syslog configured
        at all, not just what catches a dropped syslog message on a
        properly-configured one - it doesn't care why the syslog path
        didn't fire, it just periodically re-derives ground truth."""
        for device_id in device_ids:
            env, polled_at = get_env_and_polled_at(device_id)
            if polled_at is None or env is None:
                continue
            if self._last_seen_poll_at.get(device_id) == polled_at:
                continue  # same snapshot as last check - nothing new learned
            self._last_seen_poll_at[device_id] = polled_at

            # Key order is (device_id, kind, unit, bay) - must match
            # check_via_syslog/_parse_component's key order exactly, or
            # the two paths silently never correlate the same physical
            # component with each other (confirmed by a real test
            # failure during development, not just a style nit - a
            # swapped order here means a syslog-fired alert can never be
            # resolved by a poll tick that sees it recovered, and vice
            # versa).
            currently_faulted = {}
            for fan in env.get("fans", []) or []:
                # fan2_status is None (not "down") for Junos devices -
                # status_poller.py maps Junos's one-fan-per-entry shape
                # onto Dell's paired fan1/fan2 fields with fan2 left
                # unset, since Junos has no second fan in the same entry
                # to report. Treating None the same as a real "down"
                # here was a real false-positive bug (confirmed live: it
                # fired a fault for every fan on a real, healthy EX3300)
                # - only a field that's actually present and not "up"
                # counts as faulted.
                statuses = [s for s in (fan.get("fan1_status"), fan.get("fan2_status")) if s is not None]
                if statuses and any(s != "up" for s in statuses):
                    key = (device_id, "fan", str(fan["unit"]), str(fan["bay"]))
                    currently_faulted[key] = "warning"
            for psu in env.get("psus", []) or []:
                if psu.get("status") != "up":
                    key = (device_id, "psu", str(psu["unit"]), str(psu["bay"]))
                    currently_faulted[key] = "critical"

            device_keys = {k for k in self._alerting if k[0] == device_id}
            for key in device_keys - currently_faulted.keys():
                log.info("hardware alert reconciled via poll (missed syslog recovery?): %s", key)
                self._resolve(key, alertmanager, device_name_for)
                self._last_posted.pop(key, None)
            for key, severity in currently_faulted.items():
                self._maybe_fire_or_heartbeat(key, severity, alertmanager, device_name_for)

    def _maybe_fire_or_heartbeat(self, key, severity, alertmanager, device_name_for):
        """Fires a genuinely new fault immediately, but re-POSTs an
        already-firing one only every HEARTBEAT_SECONDS - the direct
        equivalent of InterfaceAlertChecker._maybe_heartbeat, and needed
        for the same two reasons. The re-POST exists at all because a
        directly-posted alert (unlike a Prometheus-rule one, which
        Prometheus re-sends every evaluation cycle) is lost silently if
        Alertmanager restarts, with no way for it to ask for a resend.
        But reconcile_via_poll runs every ~10s against a fault that can
        persist for hours, so firing unconditionally there re-POSTed the
        same unchanged alarm ~12x more often than the heartbeat interval
        this class already declared - confirmed by counting real posts
        (10 posts across 10 unchanged polls, where 1 was intended)."""
        now = time.monotonic()
        if key not in self._alerting:
            self._fire(key, severity, alertmanager, device_name_for)
            self._last_posted[key] = now
            return
        if now - self._last_posted.get(key, 0) >= self.HEARTBEAT_SECONDS:
            self._fire(key, severity, alertmanager, device_name_for)
            self._last_posted[key] = now

    def reseed_from_alertmanager(self, alertmanager):
        """Called once at process startup (see app.py) - same reasoning as
        InterfaceAlertChecker.reseed_from_alertmanager: makes the common
        case (Alertmanager already up) instant instead of leaving a real
        alert briefly unheartbeated after every restart."""
        try:
            alerts = alertmanager.list_alerts()
        except Exception:
            log.warning("could not reseed hardware-alert state from Alertmanager", exc_info=True)
            return
        seeded = 0
        for alert in alerts or []:
            labels = alert.get("labels", {})
            if labels.get("alertname") != ALERTNAME:
                continue
            if alert.get("status", {}).get("state") != "active":
                continue
            key = (labels.get("device_id"), labels.get("kind"), labels.get("unit"), labels.get("bay"))
            if None in key:
                continue
            self._alerting[key] = labels.get("severity", "warning")
            self._last_posted[key] = time.monotonic()
            seeded += 1
        if seeded:
            log.info("hardware alert state reseeded from Alertmanager: %d alert(s) still active", seeded)

    def forget(self, device_id, kind, unit, bay):
        """Drops all tracking for one component, so a manual resolve from
        the UI isn't immediately undone by this module's own reconcile
        loop re-arming it. Same non-suppression semantics as
        InterfaceAlertChecker.forget - if it's genuinely still faulted,
        the next reconcile tick re-fires it."""
        key = (device_id, kind, unit, bay)
        self._alerting.pop(key, None)
        self._last_posted.pop(key, None)

    def _fire(self, key, severity, alertmanager, device_name_for):
        """Always POSTs (safe/idempotent to Alertmanager either way, same
        reasoning as InterfaceAlertChecker's heartbeat) but only logs on a
        genuine new-vs-already-alerting transition, so a reconcile tick
        re-affirming an already-known fault doesn't spam the log the way
        a real new fault firing should be visible."""
        device_id, kind, unit, bay = key
        is_new = key not in self._alerting
        self._alerting[key] = severity
        labels = {
            "alertname": ALERTNAME,
            "device_id": device_id,
            "kind": kind,
            "unit": unit,
            "bay": bay,
            "severity": severity,
        }
        try:
            alertmanager.post_alerts([{
                "labels": labels,
                "annotations": {
                    "summary": f"{kind.upper()} unit {unit} bay {bay} down on {device_name_for(device_id)}",
                    "description": "show environment / syslog reports this component as down.",
                },
                "startsAt": datetime.now(timezone.utc).isoformat(),
            }])
            if is_new:
                log.info("hardware alert fired: %s", key)
        except Exception:
            log.exception("could not post hardware alert for %s", key)

    def _resolve(self, key, alertmanager, device_name_for):
        device_id, kind, unit, bay = key
        severity = self._alerting.pop(key, "warning")
        now = datetime.now(timezone.utc)
        try:
            alertmanager.post_alerts([{
                "labels": {
                    "alertname": ALERTNAME,
                    "device_id": device_id,
                    "kind": kind,
                    "unit": unit,
                    "bay": bay,
                    "severity": severity,
                },
                "annotations": {"summary": f"{kind.upper()} unit {unit} bay {bay} recovered on {device_name_for(device_id)}"},
                "startsAt": (now - timedelta(minutes=1)).isoformat(),
                "endsAt": now.isoformat(),
            }])
            log.info("hardware alert resolved: %s", key)
        except Exception:
            log.exception("could not resolve hardware alert for %s", key)
