import concurrent.futures
import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

import psycopg2
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

import junos_parsers
import alert_acks
import audit
import occurrences
import paging
from alertmanager_client import AlertmanagerClient, AlertmanagerError
import logging_setup
import metrics
import opnsense_parsers
import parsers
import settings as settings_store
from commands import COMMAND_TREES, find_command
from db import Database
from devices import DeviceConfigError, StoredDevice, load_devices
from loki_client import LokiClient, LokiError
from results_store import ResultsStore
from scheduler import ScheduleStore
import alert_rules
import compliance
import interface_alerting
from ssh_client import SwitchSSH, SwitchSSHError
from status_poller import StatusPoller
from store import DeviceStore
from summarize import summarize
import topology
import trending
from topology_store import TopologyStore

logging_setup.configure_logging()
log = logging.getLogger("webui")

BASE_DIR = Path(__file__).parent
DEVICES_PATH = os.environ.get("DEVICES_FILE", str(BASE_DIR / "devices.yaml"))
LEGACY_STORE_PATH = os.environ.get("DEVICE_STORE_FILE", str(BASE_DIR / "data" / "devices_store.json"))
LEGACY_SQLITE_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "switchboard.db"))

# Alertmanager (ROADMAP 3.2), unlike Loki, is a fixed service in this same
# docker-compose stack rather than something reachable at a
# deployment-specific address on a separate host - a plain env-var default
# is enough, no need for the Settings-page-editable DSN treatment Loki
# gets.
ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093")
ALERTMANAGER = AlertmanagerClient(ALERTMANAGER_URL)
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
# How long an alarm is held back from paging so it can be looked at first
# (see paging.py). 0 disables the hold entirely and restores "page the
# instant it fires". Applies to new alarms only - anything already held
# keeps the window it was given.
PAGE_DELAY_SECONDS = int(os.environ.get("PAGE_DELAY_SECONDS", "120"))
PAGER = paging.PagingController(ALERTMANAGER, PAGE_DELAY_SECONDS)
# Holds placed before an alarm exists as an occurrence, keyed by signature.
# A hold has to be in place *before* Alertmanager dispatches (group_wait is
# 0s, so the webhook telling us it fired arrives after the page would have
# gone out), but the occurrence record only exists once it has fired - so
# the hold waits here in between. In-memory on purpose: losing it across a
# restart means the alarm pages immediately, which is the safe direction.
PENDING_HOLDS = {}

# Where the Rules tab writes the generated alert rules file, and where it
# asks Prometheus to reload from - see docker-compose.yml for both the
# writable bind mount at this exact path and --web.enable-lifecycle.
ALERT_RULES_FILE = os.environ.get("ALERT_RULES_FILE", str(BASE_DIR / "data" / "prometheus-alerts.yml"))
PROMETHEUS_RELOAD_URL = os.environ.get("PROMETHEUS_RELOAD_URL", "http://prometheus:9090/-/reload")

# Deployment config (Postgres DSN, Loki URL, webui login) lives in a small
# JSON file on the webui-data volume, editable from the in-app Settings
# page - see settings.py for why this can't just live in Postgres too.
# Falls back to env vars on a brand new volume so existing docker-compose
# deployments keep working unchanged; if neither is present the app still
# boots (rather than crashing) and serves a setup wizard instead of the
# normal UI until someone configures it.
WEBUI_USER = None
WEBUI_PASS_HASH = None
LOKI_URL = None
DATABASE_URL = None
CONFIGURED = False
DB_ERROR = None

security = HTTPBasic(auto_error=False)


def _check_auth(credentials):
    if not CONFIGURED:
        raise HTTPException(status_code=503, detail="Switchboard is not configured yet")
    if credentials is None or not secrets.compare_digest(credentials.username, WEBUI_USER) or not settings_store.verify_password(
        credentials.password, WEBUI_PASS_HASH
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    _check_auth(credentials)
    return credentials.username


def require_auth_and_db(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    _check_auth(credentials)
    if STORE is None:
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable ({DB_ERROR}). Fix the connection on the Settings page.",
        )
    return credentials.username


app = FastAPI(title="Switchboard")
app.add_middleware(GZipMiddleware, minimum_size=500)


# Safety nets, not the primary error path: most routes already catch
# SwitchSSHError/LokiError locally and return a clean message (a device
# being unreachable, or Loki being down, is routine and expected). These
# two handlers exist for whatever slips through uncaught - most notably
# every `db.py`-backed store (store.py/results_store.py/topology_store.py)
# does zero exception handling of its own and lets a sustained Postgres
# outage propagate straight up (`Database._with_reconnect` only absorbs a
# single dropped connection, not a genuinely down database) - without
# this, that surfaces as FastAPI's generic 500 with a raw traceback
# instead of a clear "the database is unavailable" the Settings page
# already trains users to expect (see require_auth_and_db's 503).
@app.exception_handler(psycopg2.Error)
async def _db_error_handler(request: Request, exc: psycopg2.Error):
    log.error("unhandled database error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": f"Database unavailable: {exc}"})


@app.exception_handler(SwitchSSHError)
async def _ssh_error_handler(request: Request, exc: SwitchSSHError):
    log.warning("unhandled SSH error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    """Stamps every request with a correlation ID (ROADMAP 0.4's "trace a
    command run end to end") - reuses an incoming `X-Request-ID` if the
    caller already has one (useful behind a reverse proxy that generates
    its own), otherwise mints a short one. Set into logging_setup's
    contextvar so every log line this request touches - including
    ssh_client.py's connect/run logging deep inside a synchronous route
    handler - carries it with no extra plumbing (see that module's
    docstring for why the propagation is real, not aspirational). Echoed
    back as a response header so the frontend/caller can correlate too."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = logging_setup.request_id_var.set(request_id)
    start = time.monotonic()
    try:
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        log.info("%s %s -> %d (%.1fms)", request.method, request.url.path, response.status_code, duration_ms)
        return response
    finally:
        logging_setup.request_id_var.reset(token)

DB = None
STORE = None
RESULTS = None
TOPOLOGY_STORE = None
SCHEDULES = None
ALERT_RULES = None
INTERFACE_ALERT_RULES = None
OCCURRENCES = None
AUDIT = None
# Not reset on DB reconfigure like the stores above - it's just in-memory
# down-tracking state (see InterfaceAlertChecker's docstring), no reason
# to lose it because Settings saved a new Postgres DSN.
INTERFACE_ALERT_CHECKER = interface_alerting.InterfaceAlertChecker()
# Re-establish tracking for whatever InterfaceDown alerts are still
# genuinely active in Alertmanager right now, so a webui restart mid-alert
# doesn't orphan them (see InterfaceAlertChecker.reseed_from_alertmanager
# and reconcile_via_poll's docstring - a real restart-mid-outage bug that
# left a resolved-in-reality alert paging for ~10 extra minutes).
INTERFACE_ALERT_CHECKER.reseed_from_alertmanager(ALERTMANAGER)
DEVICES = []
DEVICES_BY_ID = {}
LOKI = None


def _migrate_legacy_json_devices():
    """One-time import from the pre-SQLite devices_store.json, if it's ever
    non-empty on an existing volume. A no-op on any volume created after
    this change (there's no legacy file), and safe to run every startup."""
    if not os.path.exists(LEGACY_STORE_PATH):
        return
    try:
        with open(LEGACY_STORE_PATH) as f:
            legacy = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not legacy or STORE.load():
        return
    for record in legacy:
        try:
            STORE.add(record)
        except ValueError:
            pass
    log.info("migrated %d device(s) from legacy %s into Postgres", len(legacy), LEGACY_STORE_PATH)


def _migrate_legacy_sqlite():
    """One-time import from the pre-Postgres switchboard.db, if it's ever
    present with rows on an existing volume. A no-op on any volume created
    after this change (no legacy file), or once Postgres already has rows
    (so this is safe to run every startup, same as the JSON migration
    above) - checked per-table since devices and results are independent."""
    if not os.path.exists(LEGACY_SQLITE_PATH):
        return
    try:
        legacy_conn = sqlite3.connect(LEGACY_SQLITE_PATH)
        legacy_conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return

    if not STORE.load():
        rows = legacy_conn.execute("SELECT data FROM devices ORDER BY rowid").fetchall()
        migrated = 0
        for row in rows:
            try:
                STORE.add(json.loads(row["data"]))
                migrated += 1
            except ValueError:
                pass
        if migrated:
            log.info("migrated %d device(s) from legacy %s into Postgres", migrated, LEGACY_SQLITE_PATH)

    existing_results, _ = RESULTS.list(limit=1)
    if not existing_results:
        rows = legacy_conn.execute(
            """SELECT filename, device_id, device_name, host, category_id, command_id, command, summary,
                      output, markdown, auto_saved, created_at
               FROM results ORDER BY filename"""
        ).fetchall()
        for row in rows:
            DB.execute(
                """INSERT INTO results
                   (filename, device_id, device_name, host, category_id, command_id, command, summary, output,
                    markdown, auto_saved, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (filename) DO NOTHING""",
                (
                    row["filename"], row["device_id"], row["device_name"], row["host"], row["category_id"],
                    row["command_id"], row["command"], row["summary"], row["output"], row["markdown"],
                    row["auto_saved"], row["created_at"],
                ),
            )
        if rows:
            log.info("migrated %d result(s) from legacy %s into Postgres", len(rows), LEGACY_SQLITE_PATH)

    legacy_conn.close()


# One persistent SSH session per device, reused across requests, rather than
# a fresh login per click. Dell OS9 only has a handful of concurrent vty
# (SSH) slots - opening/closing a new session per command reliably starved
# it under real use (confirmed live: most connection attempts failed with
# "Error reading SSH protocol banner" once the exporter's own persistent
# session plus a couple of clicks were in flight). A lock per device
# serializes command execution on that device's single shared session.
_sessions: dict[str, SwitchSSH] = {}
_session_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _make_switch(device):
    return SwitchSSH(
        device.host,
        device.username,
        device.password,
        enable_password=device.enable_password,
        private_key=device.private_key,
        passphrase=device.passphrase,
        platform=device.platform,
    )


def _get_session(device):
    switch = _sessions.get(device.id)
    if switch is None:
        switch = _make_switch(device)
        _sessions[device.id] = switch
    return switch


# Live status (up/down/alarm + data age), polled the same way the
# Prometheus exporter does but as its own in-process background poller -
# see status_poller.py for why this doesn't depend on the exporter
# container or Prometheus being reachable. Created once at import time;
# _load_database() below (re)populates the devices it polls, including
# on a Settings-page DSN change, without needing a new instance.
STATUS = StatusPoller(
    get_session=_get_session, lock_for=lambda device_id: _session_locks[device_id], get_db=lambda: DB
)

# Trend samples (see trending.py) accumulate at ~288/day/metric/port on the
# status poller's slow cadence - pruned once a day so the table doesn't
# grow forever. A plain daemon thread rather than a scheduler dependency;
# prune_old_samples() is a no-op until DB is actually configured.
def _trend_pruner_loop():
    while True:
        time.sleep(24 * 3600)
        try:
            trending.prune_old_samples(DB, keep_days=90)
        except Exception:
            log.exception("trend sample pruning failed")


threading.Thread(target=_trend_pruner_loop, daemon=True, name="trend-pruner").start()


# Scheduled/recurring runs (ROADMAP 3.6) - a lightweight poll loop rather
# than pulling in a scheduler dependency (cron semantics aren't needed,
# just "every N minutes"). 30s resolution is plenty for the shortest
# sensible interval (config-backup/compliance runs, not sub-minute
# polling - status_poller.py already owns that). A schedule pointed at an
# unreachable device or a command that doesn't exist on that platform
# records last_error and reschedules for next interval rather than
# blocking the rest of the queue - the same per-device isolation bulk-run
# gives via ThreadPoolExecutor, just sequential here since scheduled runs
# aren't latency-sensitive.
def _schedule_loop():
    while True:
        time.sleep(30)
        if DB is None or SCHEDULES is None:
            continue
        try:
            due = SCHEDULES.due()
        except Exception:
            log.exception("schedule lookup failed")
            continue
        for sched in due:
            device = DEVICES_BY_ID.get(sched["device_id"])
            error = None
            if device is None:
                error = f"device {sched['device_id']!r} no longer exists"
            else:
                try:
                    _run_and_save(device, sched["category_id"], sched["command_id"], sched["params"], "scheduler")
                except Exception as e:
                    error = str(e)
                    log.warning("scheduled run %s failed: %s", sched["id"], error)
            try:
                SCHEDULES.mark_run(sched["id"], sched["interval_minutes"], error=error)
            except Exception:
                log.exception("could not record schedule run for %s", sched["id"])


threading.Thread(target=_schedule_loop, daemon=True, name="schedule-runner").start()


def _port_state_for(device_id, port):
    status = STATUS.get(device_id, include_interfaces=True)
    if status is None:
        return None
    for iface in status.get("interfaces", []):
        if iface.get("port") == port:
            return iface.get("port_state")
    return None


def _port_state_and_polled_at_for(device_id, port):
    """Like _port_state_for, but also returns the status poller's own
    `last_polled` timestamp - interface_alerting.py's reconcile_via_poll
    needs this to tell a genuinely fresh SSH poll apart from re-reading
    the same cached snapshot it already considered."""
    status = STATUS.get(device_id, include_interfaces=True)
    if status is None:
        return None, None
    polled_at = status.get("last_polled")
    for iface in status.get("interfaces", []):
        if iface.get("port") == port:
            return iface.get("port_state"), polled_at
    return None, polled_at


def _device_name_for(device_id):
    device = DEVICES_BY_ID.get(device_id)
    return device.name if device else device_id


# Per-interface down-alerting (ROADMAP 3.2's Interfaces tab, see
# interface_alerting.py) - same 30s cadence as the status poller's fast
# poll, since it's reading that same already-polled state rather than
# doing its own SSH.
def _interface_alert_loop():
    while True:
        time.sleep(30)
        if DB is None or INTERFACE_ALERT_RULES is None:
            continue
        try:
            configs = INTERFACE_ALERT_RULES.list()
        except Exception:
            log.exception("interface alert config lookup failed")
            continue
        try:
            INTERFACE_ALERT_CHECKER.check_once(configs, _port_state_for, _device_name_for, ALERTMANAGER)
        except Exception:
            log.exception("interface alert check failed")


threading.Thread(target=_interface_alert_loop, daemon=True, name="interface-alert-checker").start()


# Fast path for "immediate" mode - a 30s-bounded SSH poll cycle isn't
# what a human means by "alert me immediately" (confirmed live: the
# switch's own syslog reports a link-down transition within ~1-2s, real
# users noticed the ~10-30s gap between that and this alerting). Vector
# already ships that same event to Loki in real time (syslog/vector.yaml),
# so this polls Loki - a single cheap HTTP query, not an SSH round trip -
# on a much tighter interval instead of waiting on the device poll cycle.
def _interface_alert_syslog_loop():
    while True:
        time.sleep(3)
        if DB is None or INTERFACE_ALERT_RULES is None or LOKI is None:
            continue
        try:
            configs = INTERFACE_ALERT_RULES.list()
        except Exception:
            log.exception("interface alert config lookup failed (syslog path)")
            continue
        try:
            INTERFACE_ALERT_CHECKER.check_via_syslog(configs, LOKI, DEVICES_BY_ID, ALERTMANAGER, _device_name_for)
        except Exception:
            log.exception("interface alert syslog check failed")


threading.Thread(target=_interface_alert_syslog_loop, daemon=True, name="interface-alert-syslog-checker").start()


# Reconciliation for currently-alerting immediate-mode ports (ROADMAP
# 3.2, user request 2026-08-01) - every 5s, checks whether a still-firing
# alert's real polled state has since gone back up, in case the syslog
# "up" event that would normally resolve it got missed (Vector hiccup,
# Loki ingestion gap). See interface_alerting.py's reconcile_via_poll for
# why this is safe against the stale-read hazard that ruled out a
# simpler "just let check_once resolve too" design.
def _interface_alert_reconcile_loop():
    while True:
        time.sleep(5)
        if DB is None or INTERFACE_ALERT_RULES is None:
            continue
        try:
            configs = INTERFACE_ALERT_RULES.list()
        except Exception:
            log.exception("interface alert config lookup failed (reconcile path)")
            continue
        try:
            INTERFACE_ALERT_CHECKER.reconcile_via_poll(configs, _port_state_and_polled_at_for, _device_name_for, ALERTMANAGER)
        except Exception:
            log.exception("interface alert reconcile check failed")


threading.Thread(target=_interface_alert_reconcile_loop, daemon=True, name="interface-alert-reconciler").start()


def _place_hold(labels, signature=None):
    """Puts the investigation hold on an alarm that is about to fire, and
    remembers it until the occurrence exists to attach it to. Safe to call
    more than once for the same alarm - a second call while a hold is
    already recorded is a no-op rather than a stacked silence.

    Only ever called for Prometheus-rule ("environmental" hardware) alarms
    - see _paging_scheduler_loop's docstring for why interface alerts are
    deliberately excluded from paging holds entirely.

    The duration comes from the alert rule's own page_delay_seconds if one
    is set (Rules tab), falling back to the app-wide PAGE_DELAY_SECONDS -
    looked up by alertname, since that's the identity a Rules tab entry
    has and labels don't carry a rule name of their own."""
    alertname = labels.get("alertname")
    delay_seconds = PAGE_DELAY_SECONDS
    if ALERT_RULES is not None and alertname:
        try:
            delay_seconds = ALERT_RULES.page_delay_for(alertname, PAGE_DELAY_SECONDS)
        except Exception:
            log.exception("could not look up page delay for rule %s - using app-wide default", alertname)
    if delay_seconds <= 0:
        return
    signature = signature or alert_acks.fingerprint_for(labels)
    if signature in PENDING_HOLDS:
        return
    silence_id, page_at = PAGER.hold_for_duration(labels, delay_seconds)
    if silence_id:
        PENDING_HOLDS[signature] = (silence_id, page_at.isoformat())
        log.info("paging held for %ss: %s", delay_seconds, alertname)


def _gather_pending_alerts():
    """Every alert currently inside a confirmation window and nowhere
    else - Prometheus rules still in their `for:` window, plus
    interface_alerting's delayed-mode ports still counting down toward
    `delay_seconds`. Same two sources api_list_alerts_live merges for the
    Active alerts tab's "pending" rows; factored out here so the occurrence
    sync loop can open a record for them too."""
    pending = _prometheus_pending_rules()
    if DB is not None and INTERFACE_ALERT_RULES is not None:
        try:
            configs = INTERFACE_ALERT_RULES.list()
            pending += INTERFACE_ALERT_CHECKER.pending_entries(configs, _device_name_for)
        except Exception:
            log.exception("could not compute pending interface-alert entries for occurrence sync")
    return pending


def _sync_occurrences():
    """Opens and closes occurrences from real system state - Alertmanager's
    own alert list, plus Prometheus/interface "pending" state - rather than
    relying on the Alertmanager webhook.

    The webhook can't be the source of truth here, confirmed live: a
    silence suppresses *every* receiver, including this app's webhook. Since
    paging holds are silences (paging.py), a held alarm produced no webhook
    at all - so it got no occurrence, no countdown, and nothing to press
    Page now on. The webhook also never fires for something that never
    crosses Prometheus's `for:` window at all: a condition that goes
    pending and clears again before ever firing produces zero
    notifications, so it was previously invisible everywhere, including
    the alarm log - confirmed live (a Te 1/47 flap and an EX3300 flap, both
    real, neither logged anywhere for later investigation). This function
    fixes both gaps by opening a record for a pending alarm immediately,
    the moment Prometheus or interface_alerting first reports it - not
    waiting for it to actually fire.

    Alertmanager's /api/v2/alerts *does* list suppressed alerts (state
    "suppressed"), so polling it sees held and silenced alarms alike. The
    webhook still records notification history and closes occurrences
    promptly for the unheld, already-firing case; this loop is what makes
    the record complete rather than only covering alarms that happened to
    notify."""
    try:
        alerts = ALERTMANAGER.list_alerts() or []
    except AlertmanagerError:
        return  # transient - next tick will catch up

    firing_signatures = set()
    for alert in alerts:
        labels = alert.get("labels", {})
        signature = alert_acks.fingerprint_for(labels)
        firing_signatures.add(signature)
        occurrence = OCCURRENCES.open(
            signature,
            labels.get("alertname", "unknown"),
            labels.get("severity"),
            alert.get("annotations", {}).get("summary"),
            labels,
            started_at=alert.get("startsAt"),
        )
        if occurrence is None or occurrence["paged_at"] or occurrence["paging_disabled"]:
            continue
        if occurrence["page_at"] is None:
            held = PENDING_HOLDS.pop(signature, None)
            if held:
                OCCURRENCES.set_paging(occurrence["id"], held[1], held[0])
            else:
                # Nothing held it back, so Alertmanager has already
                # notified - record that instead of showing a countdown
                # for a page that has been and gone.
                OCCURRENCES.mark_paged(occurrence["id"])

    # A pending alarm gets its own occurrence too - opened now, with no
    # page_at yet (that arrives once _place_hold actually places a hold, or
    # once it fires for real and the branch above takes over). This is what
    # makes a condition that never crosses `for:` still end up logged: it
    # gets a record the moment it's first seen, not the moment it fires.
    pending_signatures = set()
    for alert in _gather_pending_alerts():
        labels = alert.get("labels", {})
        signature = alert_acks.fingerprint_for(labels)
        pending_signatures.add(signature)
        if signature in firing_signatures:
            continue  # already open via the real alert above - don't reopen/duplicate
        OCCURRENCES.open(
            signature,
            labels.get("alertname", "unknown"),
            labels.get("severity"),
            alert.get("annotations", {}).get("summary"),
            labels,
            started_at=alert.get("startsAt"),
        )

    # Anything open that's neither firing nor still pending is over. The
    # case worth naming: an alarm that recovered *inside* its paging hold,
    # which is exactly what the hold exists for - it closes here having
    # never paged. A pending-only alarm that simply cleared before ever
    # firing closes the same way, which is the fix for the logging gap
    # above: it now has a start time, an end time, and a full timeline,
    # instead of never having existed as a record at all.
    never_closing = firing_signatures | pending_signatures
    for occurrence in OCCURRENCES.list(limit=1000, open_only=True):
        if occurrence["signature"] in never_closing:
            continue
        if occurrence["silence_id"]:
            PAGER.release(occurrence["silence_id"])
        # A pending alarm can have a hold already placed (by _place_hold,
        # ahead of ever firing) without it having been attached to the row
        # yet - that only happens once it actually fires. Release it here
        # too, or a hold placed for something that recovered while merely
        # pending would just sit until Alertmanager auto-expires it rather
        # than being cleaned up the moment we know it's no longer needed.
        held = PENDING_HOLDS.pop(occurrence["signature"], None)
        if held:
            PAGER.release(held[0])
        OCCURRENCES.close(occurrence["signature"])
        if occurrence["paged_at"] is None:
            log.info("alarm %s cleared without ever paging", occurrence["id"])


def _paging_scheduler_loop():
    """Three jobs, all on the same 3s tick:

    1. Put the paging hold on Prometheus rule alerts while they're still in
       their `for:` window - the only moment that's possible, since
       Alertmanager dispatches the instant they fire.

       Deliberately Prometheus-rule alerts only - PSU/fan/device/optic
       alarms, the "environmental" hardware alarms. Interface link-state
       alerts (InterfaceDown) never get a paging hold: confirmed live this
       was a real mistake when it briefly existed - a hold was appearing
       on a genuine, real-time interface-down page, adding a 120s
       investigation delay the user never asked for and doesn't apply to
       interfaces at all. The Interfaces tab already has its own, separate
       "immediate vs delayed" concept (how long a port must stay down
       before it's even considered a fault, per interface_alerting.py) -
       that's the only delay meant to apply to interface alerts. Once one
       fires, it pages immediately, same as before paging holds existed.
    2. Keep occurrences in step with Alertmanager, including suppressed
       ones the webhook can never report (see above).
    3. Mark an occurrence paged once its hold lapses, so the record matches
       what Alertmanager actually did."""
    while True:
        time.sleep(3)
        if DB is None or OCCURRENCES is None:
            continue
        try:
            for alert in _prometheus_pending_rules():
                _place_hold(alert.get("labels", {}))
        except Exception:
            log.exception("paging pre-hold check failed")
        try:
            _sync_occurrences()
        except Exception:
            log.exception("occurrence sync failed")
        try:
            for occurrence in OCCURRENCES.due_to_page():
                OCCURRENCES.mark_paged(occurrence["id"])
                log.info("alarm %s paging hold lapsed - now paging", occurrence["id"])
        except Exception:
            log.exception("paging due-check failed")


threading.Thread(target=_paging_scheduler_loop, daemon=True, name="paging-scheduler").start()


def _backfill_alert_history_fingerprints():
    """Fills in alert_history.fingerprint for rows written before that
    column existed. Without this, every alert that fired before the
    per-alarm ticket view shipped is invisible to it (the incidents query
    groups by fingerprint), which would make an install with real history
    look like it had never alerted at all.

    Done in Python rather than SQL because the fingerprint is a sha256 over
    the sorted label set - the same function that fingerprints live alerts
    (alert_acks.fingerprint_for), so backfilled rows land on exactly the
    same identity as new ones for the same alarm. Runs every startup and is
    a no-op once there's nothing left to fill."""
    try:
        rows = DB.query("SELECT id, labels FROM alert_history WHERE fingerprint IS NULL LIMIT 10000")
    except Exception:
        log.exception("could not read alert history for fingerprint backfill")
        return
    if not rows:
        return
    filled = 0
    for row in rows:
        try:
            fingerprint = alert_acks.fingerprint_for(json.loads(row["labels"]))
            DB.execute("UPDATE alert_history SET fingerprint = %s WHERE id = %s", (fingerprint, row["id"]))
            filled += 1
        except Exception:
            log.exception("could not backfill fingerprint for alert_history id=%s", row["id"])
    log.info("backfilled fingerprints for %d alert history row(s)", filled)


def _load_database(dsn):
    """Connects to Postgres, runs one-time legacy migrations, and (re)loads
    devices + status polling from it. Raises on a bad DSN/unreachable host
    so callers (setup wizard, Settings save) can report a clear error
    without disturbing whatever was working before the attempt."""
    global DB, STORE, RESULTS, TOPOLOGY_STORE, SCHEDULES, ALERT_RULES, INTERFACE_ALERT_RULES
    global OCCURRENCES, AUDIT, DEVICES, DEVICES_BY_ID
    new_db = Database(dsn)
    new_store = DeviceStore(new_db)
    new_results = ResultsStore(new_db)
    new_topology_store = TopologyStore(new_db)
    new_schedules = ScheduleStore(new_db)
    new_alert_rules = alert_rules.AlertRuleStore(new_db)
    new_interface_alert_rules = interface_alerting.InterfaceAlertConfigStore(new_db)
    new_occurrences = occurrences.OccurrenceStore(new_db)
    new_audit = audit.AuditLog(new_db)
    DB, STORE, RESULTS, TOPOLOGY_STORE, SCHEDULES, ALERT_RULES, INTERFACE_ALERT_RULES = (
        new_db, new_store, new_results, new_topology_store, new_schedules, new_alert_rules, new_interface_alert_rules
    )
    OCCURRENCES, AUDIT = new_occurrences, new_audit

    _migrate_legacy_json_devices()
    _migrate_legacy_sqlite()
    _backfill_alert_history_fingerprints()
    try:
        OCCURRENCES.backfill_from_history(alert_acks.fingerprint_for)
    except Exception:
        log.exception("could not backfill alarm occurrences from history")
    try:
        OCCURRENCES.repair_stale_paging_on_resolved()
    except Exception:
        log.exception("could not repair stale paging state on resolved alarms")

    for device_id in list(_session_locks):
        STATUS.stop(device_id)
        switch = _sessions.pop(device_id, None)
        if switch is not None:
            switch.close()
    _session_locks.clear()

    DEVICES = load_devices(DEVICES_PATH, STORE)
    DEVICES_BY_ID = {d.id: d for d in DEVICES}
    for d in DEVICES:
        _session_locks[d.id] = threading.Lock()
        STATUS.start(d)


def _apply_settings(settings_dict):
    """Applies a full settings dict - called at startup (if settings are
    already on disk or seedable from env vars) and whenever the setup
    wizard or Settings page saves a new config. Raises on a bad Postgres
    DSN; callers decide how to surface that (500 at boot vs. a 400 back to
    the wizard/settings form)."""
    global WEBUI_USER, WEBUI_PASS_HASH, LOKI_URL, DATABASE_URL, CONFIGURED, DB_ERROR, LOKI
    # Validate the DSN before committing any globals, so a failed update
    # (e.g. a typo'd Postgres URL) can't half-apply - login credentials
    # and the previously-working DB connection are left untouched.
    _load_database(settings_dict["database_url"])
    WEBUI_USER = settings_dict["webui_user"]
    WEBUI_PASS_HASH = settings_dict["webui_pass_hash"]
    LOKI_URL = settings_dict.get("loki_url") or settings_store.DEFAULT_LOKI_URL
    DATABASE_URL = settings_dict["database_url"]
    LOKI = LokiClient(LOKI_URL)
    CONFIGURED = True
    DB_ERROR = None


_initial_settings = settings_store.load()
if _initial_settings is None:
    _initial_settings = settings_store.bootstrap_from_env()
    if _initial_settings is not None:
        settings_store.save(_initial_settings)

if _initial_settings is not None:
    try:
        _apply_settings(_initial_settings)
    except Exception as e:
        log.error("startup: could not connect using stored settings: %s", e)
        WEBUI_USER = _initial_settings["webui_user"]
        WEBUI_PASS_HASH = _initial_settings["webui_pass_hash"]
        LOKI_URL = _initial_settings.get("loki_url") or settings_store.DEFAULT_LOKI_URL
        DATABASE_URL = _initial_settings["database_url"]
        CONFIGURED = True
        DB_ERROR = str(e)
else:
    log.warning("Switchboard has no settings yet - visit the web UI to complete setup")


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "device"
    candidate = slug
    n = 2
    while candidate in DEVICES_BY_ID:
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


class RunRequest(BaseModel):
    device_id: str
    category_id: str
    command_id: str
    params: Optional[dict] = None


class DeviceCreateRequest(BaseModel):
    name: str
    host: str
    make: str = ""
    model: str = ""
    platform: str = "os9"
    username: str
    auth_method: str = "password"  # "password" | "ssh_key"
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None
    enable_password: Optional[str] = None
    # Whitelist for parameterized commands (e.g. "show interfaces <port>
    # transceiver") - same shape as devices.yaml's `ports`/`port_channels`,
    # just entered through the UI instead of a static file. Optional:
    # a device with none of this set simply can't run parameterized
    # commands, everything else still works.
    ports: Optional[list] = None
    port_channels: Optional[dict] = None
    # Only used by /api/devices/test, when testing a draft edit of an
    # existing device without re-entering its secret - lets the test fall
    # back to the already-stored password/key the same way a real save
    # would, instead of a confusing "password is required" for a field the
    # user deliberately left blank to keep unchanged.
    edit_id: Optional[str] = None


def _validate_device_request(req, existing=None):
    """`existing` is the current raw record when editing - a blank
    password/private_key in the request then means "keep what's on file"
    rather than "missing", so it isn't rejected the way a genuinely new
    device with no credential at all would be."""
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if not req.host.strip():
        raise HTTPException(status_code=400, detail="host is required")
    if not req.username.strip():
        raise HTTPException(status_code=400, detail="username is required")
    if req.auth_method == "password":
        if not req.password and not (existing and existing.get("password")):
            raise HTTPException(status_code=400, detail="password is required for password auth")
    elif req.auth_method == "ssh_key":
        if not req.private_key and not (existing and existing.get("private_key")):
            raise HTTPException(status_code=400, detail="private_key is required for SSH key auth")
    else:
        raise HTTPException(status_code=400, detail="auth_method must be 'password' or 'ssh_key'")
    for spec in req.ports or []:
        if "prefix" not in spec or "range" not in spec or len(spec["range"]) != 2:
            raise HTTPException(status_code=400, detail="each ports entry needs 'prefix' and a 2-value 'range'")
    if req.port_channels is not None and (
        "range" not in req.port_channels or len(req.port_channels["range"]) != 2
    ):
        raise HTTPException(status_code=400, detail="port_channels needs a 2-value 'range'")


class SetupRequest(BaseModel):
    webui_user: str
    webui_pass: str
    database_url: str
    loki_url: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    webui_user: str
    webui_pass: Optional[str] = None  # blank = keep current
    database_url: Optional[str] = None  # blank = keep current
    loki_url: Optional[str] = None


@app.get("/api/setup/status")
def api_setup_status():
    return {"configured": CONFIGURED, "db_error": DB_ERROR if CONFIGURED else None}


# Self-observability (ROADMAP 0.4) - unauthenticated like /api/setup/status
# above, deliberately: an orchestrator's health probe and a Prometheus
# scrape don't carry this app's basic-auth credentials (the exporter this
# app sits next to isn't authenticated either - see
# prometheus/prometheus.yml), and neither leaks anything sensitive (no
# command output, no device credentials - device_id/host as metric labels
# is the only fleet-identifying info in any of the three).
@app.get("/healthz")
def healthz():
    """Liveness only - the process can accept and answer a request at all.
    Deliberately checks nothing else: Postgres/Loki/a switch being down is
    routine and already handled per-request elsewhere, not a reason for an
    orchestrator to kill and restart this container (see /readyz for the
    check that's actually about whether real traffic can be served)."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness - can this instance actually serve authenticated traffic
    right now? The one dependency every `require_auth_and_db` route needs
    is Postgres reachability, checked with a trivial query rather than
    trusted from whatever DB/STORE happened to be set at startup. Loki and
    the switches aren't checked here - those already degrade gracefully
    per-request (see the exception handlers above), and pulling this
    instance out of rotation because one switch is unreachable would be
    wrong; Postgres being down means nothing meaningful can be served."""
    if not CONFIGURED or STORE is None or DB is None:
        return JSONResponse(status_code=503, content={"status": "not configured"})
    try:
        DB.query_one("SELECT 1")
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "database unavailable", "detail": str(e)})
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus scrape target for this app's own operational metrics
    (see metrics.py) - poll success/failure/duration, SSH reconnects, Loki
    query latency/failures, command run count/duration. A separate concern
    from exporter/exporter.py's `s4048_*` metrics (that's the switch's own
    hardware/interface state); this is Switchboard monitoring itself."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/setup")
def api_setup(req: SetupRequest):
    """First-run only - deliberately unauthenticated, since there's no
    login yet to authenticate with, but locked out entirely once
    CONFIGURED so it can't be used to reconfigure a running deployment
    without a Settings-page login."""
    if CONFIGURED:
        raise HTTPException(status_code=403, detail="Switchboard is already configured")
    if not req.webui_user.strip() or not req.webui_pass:
        raise HTTPException(status_code=400, detail="a login username and password are required")
    if not req.database_url.strip():
        raise HTTPException(status_code=400, detail="a Postgres connection string is required")

    new_settings = {
        "webui_user": req.webui_user.strip(),
        "webui_pass_hash": settings_store.hash_password(req.webui_pass),
        "database_url": req.database_url.strip(),
        "loki_url": (req.loki_url or "").strip() or settings_store.DEFAULT_LOKI_URL,
    }
    try:
        _apply_settings(new_settings)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not connect to Postgres: {e}")
    settings_store.save(new_settings)
    log.info("initial setup completed (webui_user=%s)", new_settings["webui_user"])
    return {"ok": True}


@app.get("/api/settings")
def api_get_settings(user: str = Depends(require_auth)):
    return {
        "webui_user": WEBUI_USER,
        "database_url_display": settings_store.redact_dsn(DATABASE_URL) if DATABASE_URL else None,
        "loki_url": LOKI_URL,
        "db_error": DB_ERROR,
    }


@app.put("/api/settings")
def api_update_settings(req: SettingsUpdateRequest, user: str = Depends(require_auth)):
    if not req.webui_user.strip():
        raise HTTPException(status_code=400, detail="a login username is required")

    new_settings = {
        "webui_user": req.webui_user.strip(),
        "webui_pass_hash": settings_store.hash_password(req.webui_pass) if req.webui_pass else WEBUI_PASS_HASH,
        "database_url": (req.database_url or "").strip() or DATABASE_URL,
        "loki_url": (req.loki_url or "").strip() or settings_store.DEFAULT_LOKI_URL,
    }
    try:
        _apply_settings(new_settings)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not connect to Postgres: {e}")
    settings_store.save(new_settings)
    log.info("user=%s updated deployment settings", user)
    return {"ok": True}


@app.get("/api/devices")
def api_devices(user: str = Depends(require_auth_and_db)):
    return [d.to_public_dict() for d in DEVICES]


@app.post("/api/devices")
def api_create_device(req: DeviceCreateRequest, user: str = Depends(require_auth_and_db)):
    _validate_device_request(req)
    with _registry_lock:
        device_id = _slugify(req.name)
        record = {
            "id": device_id,
            "name": req.name.strip(),
            "host": req.host.strip(),
            "make": req.make.strip(),
            "model": req.model.strip(),
            "platform": req.platform,
            "username": req.username.strip(),
            "auth_method": req.auth_method,
            "password": req.password,
            "private_key": req.private_key,
            "passphrase": req.passphrase,
            "enable_password": req.enable_password,
            "ports": req.ports,
            "port_channels": req.port_channels,
        }
        STORE.add(record)
        device = StoredDevice(record)
        DEVICES.append(device)
        DEVICES_BY_ID[device.id] = device
        _session_locks[device.id] = threading.Lock()
    STATUS.start(device)
    log.info("user=%s added device %s (%s)", user, device.id, device.host)
    return device.to_public_dict()


@app.get("/api/devices/{device_id}/edit")
def api_get_device_for_edit(device_id: str, user: str = Depends(require_auth_and_db)):
    """Everything the Edit form needs to repopulate itself - see
    `StoredDevice.to_edit_dict()` for exactly what is (and isn't)
    included. Static (devices.yaml) devices aren't editable through the
    UI at all."""
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if device.source != "added":
        raise HTTPException(status_code=400, detail="only devices added through the UI can be edited")
    return device.to_edit_dict()


@app.put("/api/devices/{device_id}")
def api_update_device(device_id: str, req: DeviceCreateRequest, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if device.source != "added":
        raise HTTPException(status_code=400, detail="only devices added through the UI can be edited")

    existing = STORE.get_raw(device_id)
    _validate_device_request(req, existing=existing)

    with _registry_lock:
        record = {
            "id": device_id,
            "name": req.name.strip(),
            "host": req.host.strip(),
            "make": req.make.strip(),
            "model": req.model.strip(),
            "platform": req.platform,
            "username": req.username.strip(),
            "auth_method": req.auth_method,
            # Blank in the request means "keep the existing secret" - a
            # secret already on file never has to round-trip through the
            # browser just to let someone fix an unrelated field like `make`.
            "password": req.password or (existing or {}).get("password"),
            "private_key": req.private_key or (existing or {}).get("private_key"),
            "passphrase": req.passphrase or (existing or {}).get("passphrase"),
            "enable_password": req.enable_password or (existing or {}).get("enable_password"),
            "ports": req.ports,
            "port_channels": req.port_channels,
        }
        STORE.update(device_id, record)
        new_device = StoredDevice(record)
        idx = next(i for i, d in enumerate(DEVICES) if d.id == device_id)
        DEVICES[idx] = new_device
        DEVICES_BY_ID[device_id] = new_device
        # Host/credentials may have changed - drop any live session rather
        # than keep running against stale connection details.
        switch = _sessions.pop(device_id, None)
    STATUS.stop(device_id)
    STATUS.start(new_device)
    if switch is not None:
        switch.close()
    log.info("user=%s updated device %s (%s)", user, device_id, new_device.host)
    return new_device.to_public_dict()


@app.delete("/api/devices/{device_id}")
def api_delete_device(device_id: str, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if device.source != "added":
        raise HTTPException(status_code=400, detail="only devices added through the UI can be deleted")
    with _registry_lock:
        STORE.delete(device_id)
        DEVICES[:] = [d for d in DEVICES if d.id != device_id]
        del DEVICES_BY_ID[device_id]
        _session_locks.pop(device_id, None)
        switch = _sessions.pop(device_id, None)
    STATUS.stop(device_id)
    if switch is not None:
        switch.close()
    log.info("user=%s deleted device %s", user, device_id)
    return {"ok": True}


@app.get("/api/devices/{device_id}/status")
def api_device_status(device_id: str, interfaces: bool = False, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    status = STATUS.get(device_id, include_interfaces=interfaces)
    if status is None:
        raise HTTPException(status_code=404, detail="status not yet available")
    return status


@app.post("/api/devices/{device_id}/status/refresh")
def api_device_status_refresh(device_id: str, user: str = Depends(require_auth_and_db)):
    """Forces an immediate status poll instead of waiting for the next
    background cycle - backs the "Refresh" button on the Switch Status
    tab."""
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    status = STATUS.refresh_now(device)
    log.info("user=%s forced status refresh for %s", user, device_id)
    return status


@app.get("/api/devices/{device_id}/trends")
def api_device_trend_series(device_id: str, user: str = Depends(require_auth_and_db)):
    """Every distinct trend series this device actually has samples for
    (metric + port) - drives the frontend's metric/port picker without it
    needing to guess in advance which ports have optics or PSUs."""
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    series = trending.list_available_series(DB, device_id)
    for s in series:
        s["label"] = trending.METRIC_LABELS.get(s["metric"], s["metric"])
    return {"series": series}


# Rough, honestly-labeled thresholds per metric - see trending.py's
# evaluate_decline/evaluate_deviation/forecast_linear docstrings for why
# each one is shaped the way it is. Not applied to metrics with no sound
# threshold to reason about yet (temperature: no alarm precedent to trend
# against beyond what the device's own alarm flags already cover).
_TREND_EVALUATORS = {
    "optic_rx_power_dbm": lambda samples: trending.evaluate_decline(samples, warn_by=3.0, unit=" dBm"),
    "psu_power_watts": lambda samples: trending.evaluate_deviation(samples, warn_pct=20.0, unit=" W"),
}
# Interface capacity forecasts need the port's own link speed as the
# target, not a fixed number - resolved per-request from the device's live
# interface list (see api_device_trend_data). Dell OS9's `show interfaces
# status` reports it as e.g. "10000 Mbit" (confirmed live) - parsed rather
# than matched against a fixed table, so any speed the switch reports just
# works.
_SPEED_MBIT_RE = re.compile(r"(\d+)\s*Mbit")


def _link_speed_mbps(speed_str):
    m = _SPEED_MBIT_RE.search(speed_str or "")
    return float(m.group(1)) if m else None


@app.get("/api/devices/{device_id}/trends/{metric}")
def api_device_trend_data(
    device_id: str, metric: str, port: Optional[str] = None, hours: int = 168, user: str = Depends(require_auth_and_db)
):
    """Sample history for one trend series, plus a threshold evaluation
    where one applies (see _TREND_EVALUATORS) and, for interface
    utilization, a simple capacity forecast toward the port's own link
    speed (ROADMAP 3.4's "capacity forecasting")."""
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    samples = trending.get_samples(DB, device_id, metric, port=port, hours=hours)

    alert = None
    evaluator = _TREND_EVALUATORS.get(metric)
    if evaluator:
        alert = evaluator(samples)

    forecast = None
    if metric in ("iface_input_mbps", "iface_output_mbps") and port:
        status = STATUS.get(device_id, include_interfaces=True) or {}
        iface = next((i for i in status.get("interfaces", []) if i["port"] == port), None)
        link_mbps = _link_speed_mbps((iface or {}).get("speed"))
        if link_mbps:
            forecast = trending.forecast_linear(samples, target_value=link_mbps * 0.9, unit=" Mbps")

    return {
        "metric": metric,
        "port": port,
        "label": trending.METRIC_LABELS.get(metric, metric),
        "samples": samples,
        "alert": alert,
        "forecast": forecast,
    }


@app.post("/api/devices/test")
def api_test_device(req: DeviceCreateRequest, user: str = Depends(require_auth_and_db)):
    """Try connecting with the given draft device details, without saving
    anything. Best-effort: a failure here doesn't block Save, since the
    login handshake this checks only has real support for `os9`/`junos`
    today and a device running something else may legitimately fail it
    while still being fine to store for later.

    `edit_id`, if set, is the device being edited - a blank
    password/private_key then falls back to what's already stored, same
    as a real save would, rather than failing validation for a field the
    user deliberately left untouched."""
    existing = STORE.get_raw(req.edit_id) if req.edit_id else None
    _validate_device_request(req, existing=existing)
    password = req.password or (existing or {}).get("password")
    private_key = req.private_key or (existing or {}).get("private_key")
    passphrase = req.passphrase or (existing or {}).get("passphrase")
    enable_password = req.enable_password or (existing or {}).get("enable_password")
    switch = SwitchSSH(
        req.host.strip(),
        req.username.strip(),
        password,
        enable_password=enable_password or password,
        private_key=private_key,
        passphrase=passphrase,
        platform=req.platform,
        timeout=8,
    )
    try:
        switch.connect(retries=1)
        switch.close()
    except SwitchSSHError as e:
        return {"ok": False, "message": str(e)}
    reached = "privileged EXEC mode" if req.platform != "junos" else "Junos operational mode"
    return {"ok": True, "message": f"Connected and reached {reached}."}


@app.get("/api/devices/{device_id}/values/{param_name}")
def api_device_param_values(device_id: str, param_name: str, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    return {"values": device.valid_values_for(param_name)}


@app.get("/api/commands")
def api_commands(user: str = Depends(require_auth)):
    return COMMAND_TREES


class CommandLookupError(Exception):
    """Unknown category/command, or a bad/missing param value - a client
    error (400/404), not a device/transport failure. Kept as its own
    exception rather than raising HTTPException directly from
    `_resolve_command`/`_run_and_save` so bulk-run and the scheduler (which
    run against many devices and need to record a per-device error instead
    of aborting the whole request) can catch it the same way they catch
    SwitchSSHError, without FastAPI's HTTPException short-circuiting the
    loop they're in. Carries `status_code` so /api/run can still surface
    the same 404-vs-400 distinction it always has (unknown command vs. bad
    param) while bulk-run/the scheduler, which don't need that nuance,
    can catch it uniformly."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _resolve_command(device, category_id, command_id, params):
    spec = find_command(category_id, command_id, device.platform)
    if spec is None:
        raise CommandLookupError(f"unknown command {category_id}/{command_id} for platform {device.platform}", 404)
    cmd = spec["cmd"]
    if "param" in spec:
        param_name = spec["param"]
        value = (params or {}).get(param_name)
        if value not in device.valid_values_for(param_name):
            raise CommandLookupError(f"invalid or missing {param_name!r}", 400)
        cmd = cmd.format(**{param_name: value})
    return cmd


def _run_raw(device, category_id, command_id, params):
    """Like `_run_and_save` but skips the auto-save - used by compliance
    checks, which run several commands per device on every sweep and
    would otherwise flood Saved Results with entries nobody asked to
    keep."""
    cmd = _resolve_command(device, category_id, command_id, params)
    with _session_locks[device.id]:
        switch = _get_session(device)
        return switch.run(cmd)


def _run_and_save(device, category_id, command_id, params, user, auto_saved=True):
    """Shared by /api/run, bulk-run, and the scheduler - resolves the
    allowlisted command, runs it over the device's locked SSH session, and
    auto-saves the result the same way every code path expects. Raises
    CommandLookupError/SwitchSSHError/DeviceConfigError; callers decide
    whether to turn that into an HTTP error (single-device) or a per-device
    error entry (bulk/scheduled)."""
    cmd = _resolve_command(device, category_id, command_id, params)
    log.info("user=%s device=%s running: %s", user, device.id, cmd)

    metrics.command_run_total.labels(device_id=device.id, platform=device.platform).inc()
    start = time.monotonic()
    try:
        with _session_locks[device.id]:
            switch = _get_session(device)
            output = switch.run(cmd)
    finally:
        metrics.command_run_duration_seconds.labels(device_id=device.id).observe(time.monotonic() - start)

    summary = summarize(device.platform, category_id, command_id, output)
    saved = RESULTS.save(
        device.id, device.name, device.host, category_id, command_id, cmd, summary, output, auto_saved=auto_saved
    )
    return {"command": cmd, "output": output, "summary": summary, "saved_as": saved["filename"]}


@app.post("/api/run")
def api_run(req: RunRequest, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(req.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")

    try:
        result = _run_and_save(device, req.category_id, req.command_id, req.params, user)
    except CommandLookupError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except DeviceConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except SwitchSSHError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"device": device.id, **result}


class BulkRunRequest(BaseModel):
    device_ids: list[str]
    category_id: str
    command_id: str
    params: Optional[dict] = None


@app.post("/api/bulk-run")
def api_bulk_run(req: BulkRunRequest, user: str = Depends(require_auth_and_db)):
    """Runs the same allowlisted command across several devices at once
    (ROADMAP 3.6 "bulk operations"), for a collated view of e.g. "show
    version" across the whole fleet in one shot. One device's failure
    (offline, or the command doesn't exist on that device's platform)
    never aborts the others - each gets its own result/error entry.
    Devices run in parallel, bounded by the same worker count as the
    global SSH connect semaphore (ssh_client.py) so this can't blow past
    the concurrency limit that already exists for a single device's
    reconnects."""
    if not req.device_ids:
        raise HTTPException(status_code=400, detail="device_ids must be non-empty")
    devices = []
    for device_id in req.device_ids:
        device = DEVICES_BY_ID.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}")
        devices.append(device)

    def run_one(device):
        try:
            result = _run_and_save(device, req.category_id, req.command_id, req.params, user)
            return {"device_id": device.id, "device_name": device.name, "error": None, **result}
        except CommandLookupError as e:
            return {"device_id": device.id, "device_name": device.name, "error": str(e)}
        except DeviceConfigError as e:
            return {"device_id": device.id, "device_name": device.name, "error": str(e)}
        except SwitchSSHError as e:
            return {"device_id": device.id, "device_name": device.name, "error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="bulk-run") as pool:
        results = list(pool.map(run_one, devices))

    return {"results": results}


class SaveResultRequest(BaseModel):
    device_id: str
    command: str
    summary: Optional[str] = None
    output: str
    category_id: str
    command_id: str


@app.post("/api/results")
def api_save_result(req: SaveResultRequest, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(req.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    result = RESULTS.save(
        device.id, device.name, device.host, req.category_id, req.command_id, req.command, req.summary, req.output
    )
    log.info("user=%s saved result %s", user, result["filename"])
    return result


@app.get("/api/results")
def api_list_results(
    device_id: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 10,
    user: str = Depends(require_auth_and_db),
):
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    items, total = RESULTS.list(device_id=device_id, q=q, limit=page_size, offset=(page - 1) * page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/results/{filename}")
def api_get_result(filename: str, user: str = Depends(require_auth_and_db)):
    content = RESULTS.read(filename)
    if content is None:
        raise HTTPException(status_code=404, detail="unknown result")
    return {"filename": filename, "content": content}


_TABULAR_SPLIT_RE = re.compile(r" {2,}|\t")


def _output_to_csv_rows(output):
    """Best-effort structure for CSV export of a raw `show` command's text
    output. Real Dell/Junos/OPNsense output is column-aligned with runs of
    2+ spaces between fields (confirmed against every fixture in
    tests/fixtures/) - split on that and use the first split line's column
    count as the expected width. Lines that don't match (banners, footers,
    wrapped continuation lines) fall back to a single padded column rather
    than being dropped, so nothing from the original output silently goes
    missing in the export."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if not lines:
        return [["output"]]
    split_lines = [_TABULAR_SPLIT_RE.split(ln.strip()) for ln in lines]
    widths = [len(cols) for cols in split_lines]
    common_width = max(set(widths), key=widths.count) if widths else 1
    if common_width <= 1:
        return [["line"]] + [[ln] for ln in lines]
    rows = [[f"col{i + 1}" for i in range(common_width)]]
    for cols in split_lines:
        if len(cols) == common_width:
            rows.append(cols)
        else:
            padded = cols + [""] * (common_width - len(cols))
            rows.append(padded[:common_width])
    return rows


@app.get("/api/results/{filename}/export")
def api_export_result(filename: str, format: str = "json", user: str = Depends(require_auth_and_db)):
    row = RESULTS.get_row(filename)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown result")
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be 'json' or 'csv'")

    if format == "json":
        body = json.dumps(dict(row), indent=2, default=str)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["device_id", "device_name", "host", "command", "summary", "created_at"])
    writer.writerow([row["device_id"], row["device_name"], row["host"], row["command"], row["summary"] or "", row["created_at"]])
    writer.writerow([])
    for csv_row in _output_to_csv_rows(row["output"]):
        writer.writerow(csv_row)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


class ScheduleCreateRequest(BaseModel):
    device_id: str
    category_id: str
    command_id: str
    params: Optional[dict] = None
    interval_minutes: int


class ScheduleUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = None


@app.get("/api/schedules")
def api_list_schedules(user: str = Depends(require_auth_and_db)):
    return SCHEDULES.list()


@app.post("/api/schedules")
def api_create_schedule(req: ScheduleCreateRequest, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(req.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if find_command(req.category_id, req.command_id, device.platform) is None:
        raise HTTPException(status_code=400, detail="unknown command for this device's platform")
    if req.interval_minutes < 5:
        raise HTTPException(status_code=400, detail="interval_minutes must be at least 5")
    schedule = SCHEDULES.create(req.device_id, req.category_id, req.command_id, req.params, req.interval_minutes)
    log.info("user=%s created schedule %s (device=%s every %dm)", user, schedule["id"], req.device_id, req.interval_minutes)
    return schedule


@app.put("/api/schedules/{schedule_id}")
def api_update_schedule(schedule_id: str, req: ScheduleUpdateRequest, user: str = Depends(require_auth_and_db)):
    if SCHEDULES.get(schedule_id) is None:
        raise HTTPException(status_code=404, detail="unknown schedule")
    if req.interval_minutes is not None and req.interval_minutes < 5:
        raise HTTPException(status_code=400, detail="interval_minutes must be at least 5")
    return SCHEDULES.update(schedule_id, enabled=req.enabled, interval_minutes=req.interval_minutes)


@app.delete("/api/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: str, user: str = Depends(require_auth_and_db)):
    if not SCHEDULES.delete(schedule_id):
        raise HTTPException(status_code=404, detail="unknown schedule")
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/run")
def api_run_schedule_now(schedule_id: str, user: str = Depends(require_auth_and_db)):
    sched = SCHEDULES.get(schedule_id)
    if sched is None:
        raise HTTPException(status_code=404, detail="unknown schedule")
    device = DEVICES_BY_ID.get(sched["device_id"])
    if device is None:
        raise HTTPException(status_code=404, detail="device no longer exists")
    error = None
    try:
        result = _run_and_save(device, sched["category_id"], sched["command_id"], sched["params"], user)
    except (CommandLookupError, DeviceConfigError, SwitchSSHError) as e:
        error = str(e)
        result = None
    SCHEDULES.mark_run(schedule_id, sched["interval_minutes"], error=error)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return result


def _load_compliance_config():
    row = DB.query_one("SELECT data FROM compliance_config WHERE id = 'default'")
    return json.loads(row["data"]) if row else {"expected_vlans": []}


class ComplianceConfigRequest(BaseModel):
    expected_vlans: list[int]


@app.get("/api/compliance/config")
def api_get_compliance_config(user: str = Depends(require_auth_and_db)):
    return _load_compliance_config()


@app.put("/api/compliance/config")
def api_update_compliance_config(req: ComplianceConfigRequest, user: str = Depends(require_auth_and_db)):
    data = json.dumps({"expected_vlans": req.expected_vlans})
    DB.execute(
        "INSERT INTO compliance_config (id, data) VALUES ('default', %s) "
        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
        (data,),
    )
    return _load_compliance_config()


@app.get("/api/compliance")
def api_run_compliance(user: str = Depends(require_auth_and_db)):
    """Runs every compliance check (see compliance.py) against every
    configured device, live - not cached, since a stale "compliant" result
    would defeat the point. Small fleets (this app's whole reason for
    existing) make that cheap enough to do synchronously on request."""
    config = _load_compliance_config()
    findings = compliance.run_checks(DEVICES, config.get("expected_vlans") or [], _run_raw)
    summary = {
        "pass": sum(1 for f in findings if f["status"] == "pass"),
        "fail": sum(1 for f in findings if f["status"] == "fail"),
        "skip": sum(1 for f in findings if f["status"] == "skip"),
    }
    return {"findings": findings, "summary": summary}


@app.delete("/api/results/{filename}")
def api_delete_result(filename: str, user: str = Depends(require_auth_and_db)):
    if not RESULTS.delete(filename):
        raise HTTPException(status_code=404, detail="unknown result")
    return {"ok": True}


VALID_CATEGORIES = {"auth", "interface", "spanning-tree", "hardware", "routing", "other"}


@app.get("/api/syslog")
def api_syslog(
    device_id: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 200,
    since_seconds: int = 3600,
    user: str = Depends(require_auth_and_db),
):
    """Recent switch syslog, read straight from Loki - the same sink
    syslog/vector.yaml on the LXC already ships structured events to (see
    that directory's README). `category` is checked against a fixed
    allowlist before being handed to LokiClient, which pushes it into the
    LogQL query itself (Loki's `| json` stage) rather than filtering
    client-side after fetching - fetching `limit` raw lines and filtering
    by category in Python afterward silently starved out every category
    except whichever one dominates recent traffic (verified live: this
    switch's recent log is ~99.8% auth-category churn, so any other
    category always came back empty even when real matching events
    existed further back, before the fix). Device/host filtering still
    happens in Python since it's not built from raw client input."""
    if category is not None and category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of {sorted(VALID_CATEGORIES)}")

    host_filter = None
    if device_id is not None:
        device = DEVICES_BY_ID.get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="unknown device")
        host_filter = device.host

    try:
        filters = [f'event_category="{category}"'] if category else None
        events = LOKI.query_range(filters=filters, limit=limit, since_seconds=since_seconds)
    except LokiError as e:
        raise HTTPException(status_code=502, detail=f"Loki unreachable: {e}")

    if host_filter is not None:
        events = [e for e in events if e.get("source_ip") == host_filter or e.get("device_host") == host_filter]
    return events[:limit]


def _classify_alarm(detail: str) -> dict:
    """Python mirror of syslog/vector.yaml's alarm-normalization VRL block.

    This is a deliberate second implementation, not a refactor to share
    code with Vector: relying solely on Vector to have tagged an event at
    ingestion time means a Vector regression (missing category, unshipped
    config change, etc - this exact thing happened once already, see
    syslog/README.md changelog) silently empties this whole feature with
    no error anywhere. Classifying again here from the raw `facility`/
    `detail` fields - which the interpreter has reliably extracted since
    day one - means Alarm History keeps working even if Vector's own
    alarm_severity/alarm_component fields are missing or wrong, and also
    lets it recover alarm history for events ingested before the Vector
    fix landed, which otherwise has no alarm_severity at all."""
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


@app.get("/api/devices/{device_id}/alarm-history")
def api_alarm_history(
    device_id: str,
    limit: int = 300,
    since_seconds: int = 604800,
    user: str = Depends(require_auth_and_db),
):
    """Historical hardware alarms for this device, read from Loki.

    Alarm classification is done here in Python via `_classify_alarm`,
    from each event's `facility`/`detail` fields, rather than trusted from
    whatever `alarm_severity`/`alarm_component` syslog/vector.yaml's
    `interpret_switch_event` transform may have already stamped on the
    event. Two reasons: (1) that transform has already shipped one real
    bug where the fields were silently missing entirely because a deploy
    step was skipped, and another where "alarm cleared" recoveries were
    misread as new active alarms - relying only on Vector for this again
    means a future regression there silently empties this whole feature
    with no error anywhere; (2) events ingested before that transform
    existed have `facility`/`detail` (extracted by the interpreter since
    day one) but no alarm_* fields at all, so reclassifying here recovers
    real alarm history instead of only showing events from after the fix.

    Filtered server-side via LogQL on `facility` (chassis-manager /
    environment-monitor facilities that emit fan/PSU/temperature alarms)
    rather than fetching `limit` raw lines and filtering after - alarm
    events are rare next to routine auth churn, so without the
    server-side filter they'd get starved out of the window the same way
    category filtering used to be.

    `is_current` is computed here, not trusted from any single log line:
    for each distinct `alarm_component` (e.g. "Fan tray 2 of Unit 1"), the
    most recent tagged event for that component is the current truth: if
    it's a fault (`alarm_active: true`) with no later recovery event logged
    yet, that component's alarm is still in progress."""
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")

    try:
        events = LOKI.query_range(
            filters=['facility=~"CHMGR|ENVMON|RPM|OSTATE"'], limit=limit, since_seconds=since_seconds
        )
    except LokiError as e:
        raise HTTPException(status_code=502, detail=f"Loki unreachable: {e}")

    events = [e for e in events if e.get("source_ip") == device.host or e.get("device_host") == device.host]

    alarm_events = []
    for e in events:
        detail = e.get("detail") or e.get("message") or ""
        classified = _classify_alarm(detail)
        if classified["alarm_severity"] is None and classified["alarm_active"] is not False:
            continue  # not alarm-relevant text (e.g. fan-speed-% telemetry)
        e.update(classified)
        alarm_events.append(e)

    # events are already newest-first (LokiClient sorts by timestamp desc)
    latest_seen = set()
    for e in alarm_events:
        comp = e.get("alarm_component")
        is_latest_for_component = comp is not None and comp not in latest_seen
        if comp is not None:
            latest_seen.add(comp)
        e["is_current"] = bool(is_latest_for_component and e.get("alarm_active") is True)

    return alarm_events[:limit]


# Alerting (ROADMAP 3.2) - proxies Alertmanager's own REST API rather than
# re-storing alert/silence state in Postgres. Alertmanager is the source
# of truth for both; Switchboard just gives a UI for them next to
# everything else instead of a separate tab/tool. See
# alertmanager/alertmanager.yml for how alerts actually get here
# (Prometheus evaluating prometheus/alerts.yml's rules against the
# exporter's s4048_* metrics) and its docstring for why the receiver is
# currently a webhook back into this app rather than a real
# Slack/email/PagerDuty destination.
@app.get("/api/alerts")
def api_list_alerts(user: str = Depends(require_auth_and_db)):
    try:
        return ALERTMANAGER.list_alerts()
    except AlertmanagerError as e:
        raise HTTPException(status_code=502, detail=str(e))


def _prometheus_pending_rules():
    """Prometheus's own rule-evaluation state includes a "pending" phase
    for anything currently inside its `for:` confirmation window - real
    condition, not yet old enough to fire (see prometheus/alerts.yml).
    Alertmanager never sees these at all (Prometheus only forwards alerts
    that have crossed `for:`), so /api/alerts (the raw Alertmanager
    passthrough above) has no way to show them - which is exactly the gap
    that made a genuine, already-detected PSU failure look like "no alarm"
    for the ~74s it spent confirming (2026-08-01 investigation). Best
    effort: an unreachable Prometheus just means no pending rows, not a
    500 for the whole alerts page."""
    try:
        req = urllib.request.Request(f"{PROMETHEUS_URL}/api/v1/rules")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        log.warning("could not reach Prometheus for pending-rule state", exc_info=True)
        return []
    out = []
    for group in data.get("data", {}).get("groups", []):
        for rule in group.get("rules", []):
            if rule.get("type") != "alerting":
                continue
            for alert in rule.get("alerts", []):
                if alert.get("state") != "pending":
                    continue
                out.append({
                    "labels": alert.get("labels", {}),
                    "annotations": rule.get("annotations", {}),
                    "status": {"state": "pending"},
                    "startsAt": alert.get("activeAt"),
                })
    return out


def _prometheus_firing_fingerprints():
    """Label-set fingerprints Prometheus itself currently considers firing
    (state == "firing" in the rules API) - used to tell a genuinely-firing
    Alertmanager alert apart from one whose underlying condition has
    already cleared but hasn't been resolved in Alertmanager yet (see
    "resolving" in api_list_alerts_live below). Best effort, same as
    _prometheus_pending_rules."""
    try:
        req = urllib.request.Request(f"{PROMETHEUS_URL}/api/v1/rules")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None  # unknown, not "nothing firing" - caller must not treat this as "clear"
    fps = set()
    for group in data.get("data", {}).get("groups", []):
        for rule in group.get("rules", []):
            if rule.get("type") != "alerting":
                continue
            for alert in rule.get("alerts", []):
                if alert.get("state") == "firing":
                    fps.add(tuple(sorted(alert.get("labels", {}).items())))
    return fps


@app.get("/api/alerts/live")
def api_list_alerts_live(user: str = Depends(require_auth_and_db)):
    """Everything the Active alerts tab needs in one call: Alertmanager's
    real alerts, annotated with a best-effort "resolving" state when the
    underlying condition has already cleared but Alertmanager hasn't
    formally resolved yet (the exact gap a restart-orphaned interface
    alert fell into - see interface_alerting.py's reconcile_via_poll),
    plus synthetic "pending" rows for anything still inside a confirmation
    window (Prometheus `for:` and interface_alerting's delayed-mode
    `delay_seconds`) that Alertmanager doesn't know about at all yet."""
    try:
        am_alerts = ALERTMANAGER.list_alerts()
    except AlertmanagerError as e:
        raise HTTPException(status_code=502, detail=str(e))

    firing_fps = _prometheus_firing_fingerprints()
    for alert in am_alerts:
        if alert.get("status", {}).get("state") != "active":
            continue
        labels = alert.get("labels", {})
        alertname = labels.get("alertname")
        if alertname == "InterfaceDown":
            state = _port_state_for(labels.get("device_id"), labels.get("port"))
            if state is not None and state != "down":
                alert["status"]["state"] = "resolving"
        elif firing_fps is not None:
            fp = tuple(sorted(labels.items()))
            if fp not in firing_fps:
                alert["status"]["state"] = "resolving"

    pending = _prometheus_pending_rules()
    if DB is not None and INTERFACE_ALERT_RULES is not None:
        try:
            configs = INTERFACE_ALERT_RULES.list()
            pending += INTERFACE_ALERT_CHECKER.pending_entries(configs, _device_name_for)
        except Exception:
            log.exception("could not compute pending interface-alert entries")

    # Drop a synthetic pending row if Alertmanager already has that exact
    # alarm. Alertmanager keeps a just-resolved alert visible briefly, so
    # an alarm that clears and immediately re-enters its `for:` window can
    # otherwise appear twice at once - once from each source - which reads
    # as two separate faults on the same thing.
    am_fingerprints = {alert_acks.fingerprint_for(a.get("labels", {})) for a in am_alerts}
    pending = [p for p in pending if alert_acks.fingerprint_for(p.get("labels", {})) not in am_fingerprints]

    combined = am_alerts + pending
    # Attach the currently-open occurrence (if any) so the UI can link a
    # live alert to its alarm record and show who has taken *this* episode.
    for alert in combined:
        fp = alert_acks.fingerprint_for(alert.get("labels", {}))
        alert["fingerprint"] = fp
        alert["occurrence"] = None
        alert["ack"] = None
        if OCCURRENCES is None:
            continue
        try:
            occurrence = OCCURRENCES.open_for(fp)
        except Exception:
            log.exception("could not look up open occurrence for %s", fp)
            continue
        if occurrence is not None:
            alert["occurrence"] = occurrence["id"]
            alert["ack"] = OCCURRENCES.ack_for(occurrence["id"])
    return combined


class NoteRequest(BaseModel):
    note: Optional[str] = None


class CommentRequest(BaseModel):
    body: str


def _require_occurrence(occurrence_id):
    occurrence = OCCURRENCES.get(occurrence_id)
    if occurrence is None:
        raise HTTPException(status_code=404, detail="unknown alarm")
    return occurrence




@app.post("/api/alarms/{occurrence_id}/ack")
def api_ack_occurrence(occurrence_id: int, req: NoteRequest, user: str = Depends(require_auth_and_db)):
    """Acknowledges one occurrence: records who/when/why without
    suppressing anything. Scoped to this occurrence deliberately - taking
    today's flap says nothing about the next one, and the log should show
    that rather than implying the alarm as a whole is handled forever."""
    occurrence = _require_occurrence(occurrence_id)
    ack = OCCURRENCES.ack(occurrence_id, user, req.note)
    AUDIT.record(user, "alert.ack", occurrence["alertname"], {"note": req.note},
                 occurrence["signature"], occurrence_id)
    log.info("user=%s acknowledged alarm %s (%s)", user, occurrence_id, occurrence["alertname"])
    return ack


@app.post("/api/alarms/{occurrence_id}/unack")
def api_unack_occurrence(occurrence_id: int, user: str = Depends(require_auth_and_db)):
    occurrence = _require_occurrence(occurrence_id)
    if not OCCURRENCES.unack(occurrence_id):
        raise HTTPException(status_code=404, detail="that alarm is not acknowledged")
    AUDIT.record(user, "alert.unack", occurrence["alertname"], None, occurrence["signature"], occurrence_id)
    log.info("user=%s un-acknowledged alarm %s", user, occurrence_id)
    return {"ok": True}


def _alertmanager_notification_stats():
    """Per-integration notification counters straight from Alertmanager's
    /metrics. These are the reliable ground truth for "did a notification
    actually go out" - confirmed live during the 2026-08-01 work that
    `docker logs alertmanager` does NOT print a line for a clean first-try
    send (only retries/failures log reliably), so the logs look silent even
    when delivery is working perfectly."""
    sent, failed = {}, {}
    try:
        with urllib.request.urlopen(f"{ALERTMANAGER_URL}/metrics", timeout=5) as resp:
            body = resp.read().decode(errors="replace")
    except (urllib.error.URLError, OSError):
        return None
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        m = re.match(r'alertmanager_notifications_(failed_)?total\{integration="([^"]+)"\}\s+([0-9.e+]+)', line)
        if not m:
            continue
        target = failed if m.group(1) else sent
        target[m.group(2)] = int(float(m.group(3)))
    return {
        "sent": {k: v for k, v in sent.items() if v},
        "failed": {k: v for k, v in failed.items() if v},
    }


@app.get("/api/alerts/overview")
def api_alerts_overview(user: str = Depends(require_auth_and_db)):
    """Everything the Overview tab shows: how many alerts are in each
    state, how many are owned (acked) vs unowned, and whether the alerting
    pipeline itself is actually healthy - the last part matters because
    every count above reads zero both when nothing is wrong and when the
    thing that detects what's wrong is down, and those two look identical
    on a dashboard that only shows counts."""
    counts = {"current": 0, "pending": 0, "resolving": 0, "suppressed": 0}
    severities = {"critical": 0, "warning": 0, "other": 0}
    acked = unacked = 0
    alertmanager_ok, alerts_error = True, None
    try:
        alerts = api_list_alerts_live(user=user)
    except HTTPException as e:
        alerts, alertmanager_ok, alerts_error = [], False, str(e.detail)

    for alert in alerts:
        state = alert.get("status", {}).get("state")
        if state == "active":
            counts["current"] += 1
        elif state in counts:
            counts[state] += 1
        sev = alert.get("labels", {}).get("severity")
        severities[sev if sev in severities else "other"] += 1
        if alert.get("ack"):
            acked += 1
        else:
            unacked += 1

    active_silences = 0
    try:
        active_silences = sum(1 for s in ALERTMANAGER.list_silences() if s.get("status", {}).get("state") == "active")
    except AlertmanagerError:
        pass

    prometheus_ok = _prometheus_firing_fingerprints() is not None
    notifications = _alertmanager_notification_stats()

    return {
        "counts": counts,
        "severities": severities,
        "acknowledged": acked,
        "unacknowledged": unacked,
        "active_silences": active_silences,
        "pipeline": {
            "alertmanager_ok": alertmanager_ok,
            "alertmanager_error": alerts_error,
            "prometheus_ok": prometheus_ok,
            "notifications": notifications,
        },
        "page_delay_seconds": PAGE_DELAY_SECONDS,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/audit-log")
def api_get_audit_log(
    limit: int = 200,
    action_prefix: Optional[str] = None,
    fingerprint: Optional[str] = None,
    user: str = Depends(require_auth_and_db),
):
    return AUDIT.list(limit=limit, action_prefix=action_prefix, fingerprint=fingerprint)

# Wording for each event kind as it appears on an alarm's timeline. Keeps
# the vocabulary in one place rather than scattered through the frontend,
# so the log and the timeline can't drift into describing the same event
# two different ways.
_TIMELINE_KINDS = {
    "firing": ("fired", "Alarm raised - notifications sent"),
    "resolved": ("resolved", "Alarm cleared - resolve notifications sent"),
    "alert.ack": ("acknowledged", "Acknowledged"),
    "alert.unack": ("unacknowledged", "Acknowledgement removed"),
    "alert.resolve": ("manually resolved", "Manually resolved from Switchboard"),
    "alert.comment": ("comment", "Comment posted"),
    "alert.comment_deleted": ("comment removed", "Comment deleted by its author"),
}


def _occurrence_state(occurrence, firing_signatures, pending_signatures):
    """An occurrence is open until a resolve notification closes it.

    Three ways a still-open occurrence can look, and they mean different
    things: genuinely firing/suppressed in Alertmanager ("open"); still
    only inside a confirmation window and never yet confirmed ("pending" -
    see _gather_pending_alerts, and the alarm log gap this state exists to
    close); or open in our record but present in neither ("expired" - it
    aged out via Alertmanager's resolve_timeout without ever resolving,
    the one state that means the pipeline dropped something, so it's kept
    distinct rather than laundered into "resolved")."""
    if occurrence["resolved_at"]:
        return "resolved"
    if occurrence["signature"] in firing_signatures:
        return "open"
    if occurrence["signature"] in pending_signatures:
        return "pending"
    return "expired"


def _live_signatures(user):
    """Firing/suppressed signatures and pending-only signatures, as two
    separate sets - api_list_alerts_live mixes both kinds of row together
    (it has to, for the Active alerts tab), so this splits them back apart
    by each row's own status.state rather than duplicating either
    Alertmanager or Prometheus call."""
    firing, pending = set(), set()
    try:
        for alert in api_list_alerts_live(user=user):
            fp = alert["fingerprint"]
            if alert.get("status", {}).get("state") == "pending":
                pending.add(fp)
            else:
                firing.add(fp)
    except HTTPException:
        pass
    return firing, pending


def _decorate(occurrence, firing_signatures, pending_signatures, ack=None, comments=0, total=None):
    return {
        **occurrence,
        "state": _occurrence_state(occurrence, firing_signatures, pending_signatures),
        "ack": ack,
        "comments": comments,
        "occurrences_for_signature": total,
    }


@app.get("/api/alarms")
def api_list_alarms(limit: int = 200, signature: Optional[str] = None, user: str = Depends(require_auth_and_db)):
    """The alarm log: one row per occurrence, newest first.

    Deliberately NOT one row per alarm signature. Four flaps of the same
    port are four rows here, because they are four separate things that
    happened, each with its own acknowledgement and discussion. Passing
    `signature` filters to the history of one particular alarm."""
    occurrences = OCCURRENCES.list(limit=limit, signature=signature)
    ids = [o["id"] for o in occurrences]
    acks = OCCURRENCES.acks_by_occurrence(ids)
    counts = OCCURRENCES.comment_counts(ids)
    firing, pending = _live_signatures(user)
    return [_decorate(o, firing, pending, acks.get(o["id"]), counts.get(o["id"], 0)) for o in occurrences]


@app.get("/api/alarms/{occurrence_id}")
def api_get_alarm(occurrence_id: int, user: str = Depends(require_auth_and_db)):
    """One occurrence in full: its own timeline, its own discussion, and a
    list of *earlier* occurrences of the same alarm - linked, not merged,
    so an external ticketing system fed from this can decide for itself
    whether to reopen a prior ticket or cross-reference it."""
    occurrence = _require_occurrence(occurrence_id)
    firing, pending = _live_signatures(user)
    signature = occurrence["signature"]

    # System events belonging to this occurrence only: notifications
    # between its start and its end (or now, if still open).
    upper = occurrence["resolved_at"] or datetime.now(timezone.utc).isoformat()
    history = DB.query(
        "SELECT status, summary, received_at FROM alert_history "
        "WHERE fingerprint = %s AND received_at >= %s AND received_at <= %s ORDER BY received_at ASC",
        (signature, occurrence["started_at"], upper),
    )
    system_events = []
    for h in history:
        kind, description = _TIMELINE_KINDS.get(h["status"], (h["status"], h["status"]))
        system_events.append({
            "ts": h["received_at"], "kind": kind, "actor": "system",
            "description": description, "summary": h["summary"],
        })

    # The occurrence's own started_at/resolved_at are the guaranteed record
    # of this alarm's real state transitions - fired, and (if applicable)
    # cleared - independent of whether alert_history ever recorded them.
    # This matters because it doesn't always: a silence suppresses *every*
    # Alertmanager receiver, including this app's webhook (confirmed live
    # earlier - see paging.py), so an alarm held under a paging delay, or
    # covered by an ordinary maintenance-window silence, produces zero
    # alert_history rows while suppressed. Without this, an alarm that
    # genuinely fired and genuinely resolved - a real down-to-up transition
    # - could show an empty timeline on its own ticket. Only added when
    # alert_history doesn't already have a matching row within a few
    # seconds, so the common (unheld, promptly-notified) case doesn't show
    # the same transition twice.
    def _has_nearby(kind, ts, window_seconds=5):
        target = datetime.fromisoformat(ts)
        for e in system_events:
            if e["kind"] != kind:
                continue
            try:
                other = datetime.fromisoformat(e["ts"])
            except ValueError:
                continue
            if abs((other - target).total_seconds()) <= window_seconds:
                return True
        return False

    if not _has_nearby("fired", occurrence["started_at"]):
        system_events.append({
            "ts": occurrence["started_at"], "kind": "fired", "actor": "system",
            "description": "Alarm raised", "summary": occurrence["summary"],
        })
    if occurrence["resolved_at"] and not _has_nearby("resolved", occurrence["resolved_at"]):
        system_events.append({
            "ts": occurrence["resolved_at"], "kind": "resolved", "actor": "system",
            "description": "Alarm cleared", "summary": None,
        })
    system_events.sort(key=lambda e: e["ts"])

    operator_events = []
    for e in AUDIT.list(limit=500, occurrence_id=occurrence_id):
        kind, description = _TIMELINE_KINDS.get(e["action"], (e["action"], e["action"]))
        detail = e["detail"] or {}
        operator_events.append({
            "ts": e["ts"], "kind": kind, "actor": e["actor"], "action": e["action"],
            "description": description, "summary": detail.get("note"),
        })

    previous = OCCURRENCES.previous_for(signature, occurrence_id)
    prev_acks = OCCURRENCES.acks_by_occurrence([p["id"] for p in previous])

    return {
        **_decorate(
            occurrence, firing, pending,
            ack=OCCURRENCES.ack_for(occurrence_id),
            comments=0,
            total=OCCURRENCES.count_for(signature),
        ),
        "events": sorted(system_events + operator_events, key=lambda e: e["ts"]),
        "system_events": system_events,
        "operator_events": operator_events,
        "comments": OCCURRENCES.comments_for(occurrence_id),
        "previous_occurrences": [_decorate(p, firing, pending, prev_acks.get(p["id"])) for p in previous],
        # So the UI knows which comments offer a delete control. The server
        # enforces the same rule independently (see api_delete_comment).
        "current_user": user,
    }


@app.get("/api/alarms/{occurrence_id}/comments")
def api_list_comments(occurrence_id: int, user: str = Depends(require_auth_and_db)):
    _require_occurrence(occurrence_id)
    return OCCURRENCES.comments_for(occurrence_id)


@app.post("/api/alarms/{occurrence_id}/comments")
def api_add_comment(occurrence_id: int, req: CommentRequest, user: str = Depends(require_auth_and_db)):
    occurrence = _require_occurrence(occurrence_id)
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="comment cannot be empty")
    result = OCCURRENCES.add_comment(occurrence_id, user, body)
    AUDIT.record(user, "alert.comment", occurrence["alertname"], {"note": body},
                 occurrence["signature"], occurrence_id)
    return result


@app.delete("/api/alarms/{occurrence_id}/comments/{comment_id}")
def api_delete_comment(occurrence_id: int, comment_id: int, user: str = Depends(require_auth_and_db)):
    """Authors can delete their own comments (a typo in a conversation is
    worth fixing); nobody can delete anyone else's, and the deletion itself
    is audited, so the record of what happened survives the message."""
    occurrence = _require_occurrence(occurrence_id)
    comment = OCCURRENCES.get_comment(occurrence_id, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="unknown comment")
    if comment["author"] != user:
        raise HTTPException(status_code=403, detail="you can only delete your own comments")
    OCCURRENCES.delete_comment(comment_id)
    AUDIT.record(user, "alert.comment_deleted", occurrence["alertname"], {"note": comment["body"]},
                 occurrence["signature"], occurrence_id)
    return {"ok": True}


class DelayRequest(BaseModel):
    seconds: int = 300


@app.post("/api/alarms/{occurrence_id}/page-now")
def api_page_now(occurrence_id: int, user: str = Depends(require_auth_and_db)):
    """Skips the remaining investigation window - lifts the hold so
    Alertmanager pages on its next dispatch."""
    occurrence = _require_occurrence(occurrence_id)
    if occurrence["paged_at"]:
        raise HTTPException(status_code=400, detail="this alarm has already paged")
    PAGER.release(occurrence["silence_id"])
    OCCURRENCES.set_paging_disabled(occurrence_id, False, None)
    updated = OCCURRENCES.mark_paged(occurrence_id)
    AUDIT.record(user, "alert.page_now", occurrence["alertname"], None, occurrence["signature"], occurrence_id)
    log.info("user=%s paged alarm %s immediately", user, occurrence_id)
    return updated


@app.post("/api/alarms/{occurrence_id}/delay-page")
def api_delay_page(occurrence_id: int, req: DelayRequest, user: str = Depends(require_auth_and_db)):
    """Pushes the page further out while investigating. Replaces the
    existing hold rather than stacking a second one - silences are
    additive in Alertmanager, so layering them would make "page now"
    have to unpick an unknown number of them."""
    occurrence = _require_occurrence(occurrence_id)
    if occurrence["paged_at"]:
        raise HTTPException(status_code=400, detail="this alarm has already paged")
    if req.seconds <= 0 or req.seconds > 86400:
        raise HTTPException(status_code=400, detail="delay must be between 1s and 24h")
    PAGER.release(occurrence["silence_id"])
    page_at = datetime.now(timezone.utc) + timedelta(seconds=req.seconds)
    silence_id = PAGER.hold_until(
        occurrence["labels"], page_at, f"Paging delayed {req.seconds}s for investigation by {user}", user
    )
    if silence_id is None:
        raise HTTPException(status_code=502, detail="could not extend the paging hold in Alertmanager")
    updated = OCCURRENCES.set_paging(occurrence_id, page_at.isoformat(), silence_id)
    AUDIT.record(user, "alert.page_delay", occurrence["alertname"], {"seconds": req.seconds},
                 occurrence["signature"], occurrence_id)
    log.info("user=%s delayed paging for alarm %s by %ss", user, occurrence_id, req.seconds)
    return updated


@app.post("/api/alarms/{occurrence_id}/narg")
def api_narg(occurrence_id: int, req: NoteRequest, user: str = Depends(require_auth_and_db)):
    """NARG - paging off for this alarm. The alarm stays recorded, stays
    visible and still resolves normally; only the pager is stopped. Not
    open-ended (see paging.hold_indefinitely): the hold lapses after 24h
    so an alarm can't be silently lost by turning paging off and forgetting
    about it."""
    occurrence = _require_occurrence(occurrence_id)
    PAGER.release(occurrence["silence_id"])
    reason = (req.note or "no reason given").strip()
    silence_id = PAGER.hold_indefinitely(occurrence["labels"], user, reason)
    updated = OCCURRENCES.set_paging_disabled(occurrence_id, True, silence_id)
    AUDIT.record(user, "alert.narg", occurrence["alertname"], {"note": reason},
                 occurrence["signature"], occurrence_id)
    log.info("user=%s disabled paging (NARG) for alarm %s: %s", user, occurrence_id, reason)
    return updated


@app.post("/api/alarms/{occurrence_id}/enable-paging")
def api_enable_paging(occurrence_id: int, user: str = Depends(require_auth_and_db)):
    """Undoes NARG - lifts the hold and lets the alarm page again."""
    occurrence = _require_occurrence(occurrence_id)
    PAGER.release(occurrence["silence_id"])
    updated = OCCURRENCES.set_paging_disabled(occurrence_id, False, None)
    OCCURRENCES.mark_paged(occurrence_id)
    AUDIT.record(user, "alert.paging_enabled", occurrence["alertname"], None,
                 occurrence["signature"], occurrence_id)
    return updated


@app.post("/api/alarms/{occurrence_id}/resolve")
def api_resolve_alarm(occurrence_id: int, user: str = Depends(require_auth_and_db)):
    """Manually resolves the alarm behind this occurrence by posting an
    `endsAt` to Alertmanager for its exact label set, which sends the
    normal resolved notification through every receiver (so a PagerDuty
    incident closes) and, via the webhook, closes this occurrence.

    A correction tool, not a suppression tool: if the underlying condition
    is still true the alarm fires again on the next check - as a *new*
    occurrence, which is the honest record of what happened. What this
    genuinely fixes is the opposite case, an alert still sitting in
    Alertmanager after the real condition already cleared."""
    occurrence = _require_occurrence(occurrence_id)
    labels = occurrence["labels"]
    now = datetime.now(timezone.utc)
    try:
        ALERTMANAGER.post_alerts([{
            "labels": labels,
            "annotations": {"summary": f"Manually resolved by {user}"},
            "startsAt": (now - timedelta(minutes=1)).isoformat(),
            "endsAt": now.isoformat(),
        }])
    except AlertmanagerError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if labels.get("alertname") == "InterfaceDown":
        INTERFACE_ALERT_CHECKER.forget(labels.get("device_id"), labels.get("port"))

    AUDIT.record(user, "alert.resolve", occurrence["alertname"], None, occurrence["signature"], occurrence_id)
    log.info("user=%s manually resolved alarm %s", user, occurrence_id)
    return {"ok": True}


@app.get("/api/silences")
def api_list_silences(user: str = Depends(require_auth_and_db)):
    try:
        return ALERTMANAGER.list_silences()
    except AlertmanagerError as e:
        raise HTTPException(status_code=502, detail=str(e))


class SilenceMatcher(BaseModel):
    name: str
    value: str
    isRegex: bool = False
    isEqual: bool = True


class SilenceCreateRequest(BaseModel):
    matchers: list[SilenceMatcher]
    duration_hours: float
    comment: str


@app.post("/api/silences")
def api_create_silence(req: SilenceCreateRequest, user: str = Depends(require_auth_and_db)):
    if not req.matchers:
        raise HTTPException(status_code=400, detail="at least one matcher is required")
    if req.duration_hours <= 0:
        raise HTTPException(status_code=400, detail="duration_hours must be positive")
    now = datetime.now(timezone.utc)
    starts_at = now.isoformat()
    ends_at = (now + timedelta(hours=req.duration_hours)).isoformat()
    try:
        result = ALERTMANAGER.create_silence(
            [m.model_dump() for m in req.matchers], starts_at, ends_at, user, req.comment
        )
    except AlertmanagerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    log.info("user=%s created silence %s (%dh): %s", user, result.get("silenceID"), req.duration_hours, req.comment)
    AUDIT.record(user, "silence.create", result.get("silenceID"), {
        "matchers": [m.model_dump() for m in req.matchers],
        "duration_hours": req.duration_hours,
        "comment": req.comment,
    })
    return result


@app.delete("/api/silences/{silence_id}")
def api_delete_silence(silence_id: str, user: str = Depends(require_auth_and_db)):
    try:
        ALERTMANAGER.delete_silence(silence_id)
    except AlertmanagerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    log.info("user=%s deleted silence %s", user, silence_id)
    AUDIT.record(user, "silence.expire", silence_id)
    return {"ok": True}


@app.post("/api/alertmanager/webhook")
async def api_alertmanager_webhook(request: Request):
    """Receiver for Alertmanager's webhook notifications (see
    alertmanager/alertmanager.yml) - unauthenticated like /healthz, since
    Alertmanager doesn't send this app's basic-auth credentials. Every
    notification here also gets a copy sent to Pushover (a second,
    separate receiver on the same route), and is persisted to the
    `alert_history` table (see /api/alert-history) - this one receiver
    covers both Prometheus-rule alerts and interface_alerting.py's
    directly-posted per-interface alerts, since both go through
    Alertmanager the same way."""
    payload = await request.json()
    now = datetime.now(timezone.utc).isoformat()
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        name = labels.get("alertname", "unknown")
        status = alert.get("status", "unknown")
        severity = labels.get("severity")
        summary = alert.get("annotations", {}).get("summary", "")
        metrics.alertmanager_notifications_total.labels(alertname=name, status=status).inc()
        log.info("alertmanager: %s %s - %s", status, name, summary)
        if DB is not None:
            try:
                DB.execute(
                    "INSERT INTO alert_history (alertname, status, severity, summary, labels, received_at, fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (name, status, severity, summary, json.dumps(labels), now, alert_acks.fingerprint_for(labels)),
                )
            except Exception:
                log.exception("could not record alert history for %s", name)
        # Open or close this alarm's occurrence. A repeated "firing" for an
        # already-open alarm is the same episode being re-notified, not a
        # new occurrence (see occurrences.py) - so flap counts stay honest
        # rather than inflating with every repeat_interval re-send.
        if OCCURRENCES is not None:
            signature = alert_acks.fingerprint_for(labels)
            try:
                if status == "firing":
                    occurrence = OCCURRENCES.open(signature, name, severity, summary, labels)
                    held = PENDING_HOLDS.pop(signature, None)
                    if occurrence and held and occurrence.get("page_at") is None and occurrence.get("paged_at") is None:
                        OCCURRENCES.set_paging(occurrence["id"], held[1], held[0])
                    elif occurrence and held is None and occurrence.get("paged_at") is None:
                        # No hold was in place, so Alertmanager has already
                        # notified - record that rather than showing a
                        # countdown for a page that has been and gone.
                        OCCURRENCES.mark_paged(occurrence["id"])
                elif status == "resolved":
                    held = PENDING_HOLDS.pop(signature, None)
                    open_occurrence = OCCURRENCES.open_for(signature)
                    if open_occurrence and open_occurrence.get("silence_id"):
                        # Recovered inside its investigation window - drop
                        # the hold so it doesn't sit suppressing the next
                        # occurrence of the same alarm.
                        PAGER.release(open_occurrence["silence_id"])
                    elif held:
                        PAGER.release(held[0])
                    OCCURRENCES.close(signature)
            except Exception:
                log.exception("could not update alarm occurrence for %s", name)
    return {"ok": True}


def _ack_timeline():
    """Replays every ack/unack ever recorded in the audit log into a
    per-fingerprint, time-ordered timeline. Used to answer "was this alert
    acknowledged *at the time it fired*, and by whom" for history rows -
    the alert_acks table alone can't answer that, because un-acking (and
    acking a later, unrelated recurrence of the same fault) overwrites or
    deletes the row. The audit log is append-only, so it can."""
    timeline = {}
    try:
        entries = DB.query(
            "SELECT ts, actor, action, detail FROM audit_log "
            "WHERE action IN ('alert.ack', 'alert.unack') ORDER BY ts ASC"
        )
    except Exception:
        log.exception("could not load ack timeline")
        return timeline
    for entry in entries:
        try:
            detail = json.loads(entry["detail"]) if entry["detail"] else {}
        except json.JSONDecodeError:
            continue
        labels = detail.get("labels")
        if not labels:
            continue
        fp = alert_acks.fingerprint_for(labels)
        timeline.setdefault(fp, []).append({
            "ts": entry["ts"],
            "actor": entry["actor"],
            "acked": entry["action"] == "alert.ack",
            "note": detail.get("note"),
        })
    return timeline


def _ack_in_effect_at(timeline, fingerprint, when):
    """The most recent ack/unack for `fingerprint` at or before `when` -
    returns the ack if the latest event was an ack, else None."""
    latest = None
    for event in timeline.get(fingerprint, []):
        if event["ts"] <= when:
            latest = event
        else:
            break  # timeline is ascending
    if latest is None or not latest["acked"]:
        return None
    return {"acked_by": latest["actor"], "acked_at": latest["ts"], "note": latest["note"]}


@app.get("/api/alert-history")
def api_get_alert_history(limit: int = 200, user: str = Depends(require_auth_and_db)):
    limit = max(1, min(limit, 1000))
    rows = DB.query(
        "SELECT alertname, status, severity, summary, labels, received_at, fingerprint FROM alert_history "
        "ORDER BY received_at DESC LIMIT %s",
        (limit,),
    )
    timeline = _ack_timeline()
    out = []
    for r in rows:
        labels = json.loads(r["labels"])
        # fingerprint is NULL for rows written before that column existed -
        # recompute from the labels we stored, which is the same input.
        fingerprint = r["fingerprint"] or alert_acks.fingerprint_for(labels)
        out.append({
            "alertname": r["alertname"],
            "status": r["status"],
            "severity": r["severity"],
            "summary": r["summary"],
            "labels": labels,
            "received_at": r["received_at"],
            "fingerprint": fingerprint,
            "ack": _ack_in_effect_at(timeline, fingerprint, r["received_at"]),
        })
    return out


@app.get("/api/alert-rules")
def api_list_alert_rules(user: str = Depends(require_auth_and_db)):
    return ALERT_RULES.list()


class AlertRuleUpdateRequest(BaseModel):
    severity: Optional[str] = None
    enabled: Optional[bool] = None
    # How long the condition must hold before Prometheus counts this rule
    # as firing - the "pending" confirmation window (see Prometheus's
    # `for:`). Always a real value for every rule (0 is valid: fire
    # instantly, no confirmation window), unlike page_delay_seconds below -
    # no separate "use the default" flag needed here.
    for_seconds: Optional[int] = None
    # How long this rule's alarms are held before paging (paging.py),
    # overriding the app-wide PAGE_DELAY_SECONDS. Distinguishing "not
    # touched" from "explicitly reset to the app default" needs its own
    # flag - page_delay_seconds=None on the wire is ambiguous between the
    # two, and Optional's usual "omitted" meaning collides with the field
    # existing to hold NULL as a real, chosen value.
    page_delay_seconds: Optional[int] = None
    use_default_page_delay: bool = False


@app.put("/api/alert-rules/{name}")
def api_update_alert_rule(name: str, req: AlertRuleUpdateRequest, user: str = Depends(require_auth_and_db)):
    try:
        updated = ALERT_RULES.update(
            name,
            severity=req.severity,
            enabled=req.enabled,
            for_seconds=req.for_seconds,
            page_delay_seconds=req.page_delay_seconds,
            clear_page_delay=req.use_default_page_delay,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="unknown rule")

    try:
        alert_rules.write_and_reload(ALERT_RULES.list(), ALERT_RULES_FILE, PROMETHEUS_RELOAD_URL)
    except Exception as e:
        # The DB write already committed - the rule change is real and
        # will apply next time anything reloads Prometheus - but the user
        # needs to know live reload itself didn't happen, not get a
        # falsely reassuring 200.
        raise HTTPException(status_code=502, detail=f"Rule saved, but Prometheus reload failed: {e}")

    log.info("user=%s updated alert rule %s: severity=%s enabled=%s for_seconds=%s page_delay_seconds=%s",
              user, name, req.severity, req.enabled, req.for_seconds, updated.get("page_delay_seconds"))
    AUDIT.record(user, "alert_rule.update", name, {
        "severity": req.severity, "enabled": req.enabled, "for_seconds": req.for_seconds,
        "page_delay_seconds": updated.get("page_delay_seconds"),
    })
    return updated


@app.get("/api/interface-alerts")
def api_list_interface_alerts(device_id: str, user: str = Depends(require_auth_and_db)):
    """Every port this device can address (device.valid_values_for("port")
    - same list the Console's param dropdowns use), merged with any saved
    alert config and current live state, so the UI can show a full port
    list with sane "not configured yet" defaults rather than only the
    ports someone already opted in."""
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    configs_by_port = {c["port"]: c for c in INTERFACE_ALERT_RULES.list(device_id=device_id)}
    ports = device.valid_values_for("port")
    result = []
    for port in ports:
        cfg = configs_by_port.get(port)
        result.append({
            "device_id": device_id,
            "port": port,
            "enabled": cfg["enabled"] if cfg else False,
            "mode": cfg["mode"] if cfg else "immediate",
            "delay_seconds": cfg["delay_seconds"] if cfg else 60,
            "severity": cfg["severity"] if cfg else "warning",
            "current_state": _port_state_for(device_id, port),
        })
    return result


class InterfaceAlertUpdateRequest(BaseModel):
    # Real port names (e.g. "Te 1/47") contain a "/" - a path segment
    # can't safely carry that (confirmed live: even URL-encoded as %2F,
    # FastAPI's default {port} path converter doesn't match it and
    # returns a 404), so `port` travels in the body instead of the URL,
    # unlike every other single-resource PUT/DELETE in this app.
    port: str
    enabled: bool
    mode: str = "immediate"
    delay_seconds: int = 60
    severity: str = "warning"


@app.put("/api/interface-alerts/{device_id}")
def api_update_interface_alert(device_id: str, req: InterfaceAlertUpdateRequest, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if req.port not in device.valid_values_for("port"):
        raise HTTPException(status_code=400, detail="unknown port for this device")
    try:
        updated = INTERFACE_ALERT_RULES.upsert(
            device_id, req.port, req.enabled, req.mode, req.delay_seconds, severity=req.severity
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info("user=%s set interface alert %s/%s: enabled=%s mode=%s delay=%ds",
              user, device_id, req.port, req.enabled, req.mode, req.delay_seconds)
    AUDIT.record(user, "interface_alert.update", f"{device_id}/{req.port}", {
        "enabled": req.enabled, "mode": req.mode,
        "delay_seconds": req.delay_seconds, "severity": req.severity,
    })
    return updated


_LLDP_COMMAND = {"os9": "show lldp neighbors detail", "junos": "show lldp neighbors"}
_ARP_COMMAND = {"os9": "show arp", "junos": "show arp", "opnsense": "arp -an"}
_ARP_PARSER = {"os9": parsers.parse_arp, "junos": junos_parsers.parse_arp, "opnsense": opnsense_parsers.parse_arp}
# MAC-address/switching table: a second, independent topology discovery
# source alongside LLDP (see topology.py's module docstring) - only
# meaningful on an actual switch, so OPNsense (a firewall, no bridge
# table) has no entry here, same as _LLDP_COMMAND.
_MAC_TABLE_COMMAND = {"os9": "show mac-address-table", "junos": "show ethernet-switching table"}
_MAC_TABLE_PARSER = {"os9": parsers.parse_mac_address_table, "junos": junos_parsers.parse_ethernet_switching_table}
# Port-channel membership: only needed for Dell OS9 - its LLDP output never
# names the parent port-channel a member belongs to (unlike Junos, which
# reports the `ae` interface directly in `show lldp neighbors`), so without
# this a Dell port-channel that happens to carry a confirmed uplink to
# another known device can't be distinguished from one that's just a
# server's LACP-bonded NIC (see topology.py's build_topology).
_PORT_CHANNEL_COMMAND = {"os9": "show interfaces port-channel brief"}
_PORT_CHANNEL_PARSER = {"os9": parsers.parse_port_channel_brief}


def _lag_health(edges):
    """Flags a LAG bundle as degraded when its members disagree on link
    state (one up, one down) - a real problem that nothing else in this
    app calls out today (each member port looks individually "up" or
    "down" on its own, nothing rolls that up to bundle level)."""
    groups = {}
    for e in edges:
        if e["kind"] != "internal":
            continue
        for side in ("a", "b"):
            ep = e[side]
            if ep.get("lag"):
                groups.setdefault((ep["device_id"], ep["lag"]), []).append(ep["state"]["status"])
    health = []
    for (device_id, lag), statuses in groups.items():
        known = [s for s in statuses if s]
        health.append({
            "device_id": device_id,
            "lag": lag,
            "member_count": len(statuses),
            "statuses": statuses,
            "degraded": len(set(known)) > 1 if known else False,
        })
    return health


def _fetch_live_topology():
    """Fetches live LLDP from every device, builds the graph, and overlays
    current link state + (where the platform has it) Mbps utilization from
    the already-running status poller - no extra SSH round trip for that
    part, just whatever's already cached. Per-device SSH failures don't
    fail the whole call - that device just shows up with no edges and
    `lldp_error` set, same partial-failure tolerance as the rest of this
    app's multi-device endpoints."""
    raw_by_device = {}
    errors_by_device = {}
    for device in DEVICES:
        # Not every platform runs/exposes LLDP (e.g. OPNsense - a firewall
        # appliance, not part of the LLDP-discovered switch fabric) -
        # skipped entirely rather than surfaced as a per-device error.
        lldp_command = _LLDP_COMMAND.get(device.platform)
        if lldp_command is None:
            continue
        try:
            with _session_locks[device.id]:
                switch = _get_session(device)
                raw_by_device[device.id] = switch.run(lldp_command)
        except SwitchSSHError as e:
            errors_by_device[device.id] = str(e)
        except Exception:
            log.exception("unexpected error fetching LLDP for topology from %s", device.id)
            errors_by_device[device.id] = "internal error"

    # ARP tables merge in from every device regardless of LLDP support -
    # OPNsense (no LLDP integration) still sees the whole LAN and is often
    # the most complete source, since it's the router. Best-effort: a
    # device that fails here just doesn't contribute any MAC->IP entries,
    # same partial-failure tolerance as everything else on this page.
    arp_rows_by_device = {}
    for device in DEVICES:
        arp_command = _ARP_COMMAND.get(device.platform)
        if arp_command is None:
            continue
        try:
            with _session_locks[device.id]:
                switch = _get_session(device)
                raw_arp = switch.run(arp_command)
            arp_rows_by_device[device.id] = _ARP_PARSER[device.platform](raw_arp)
        except Exception:
            log.warning("could not fetch ARP table from %s for topology", device.id, exc_info=True)
    mac_to_ip = topology.merge_mac_to_ip(arp_rows_by_device)

    # MAC/switching table: a second, independent discovery source (see
    # topology.py's module docstring) that finds hosts LLDP never will -
    # anything that's sent/received a frame shows up here, LLDP-capable or
    # not. Same best-effort tolerance as ARP above.
    mac_table_by_device = {}
    for device in DEVICES:
        mac_table_command = _MAC_TABLE_COMMAND.get(device.platform)
        if mac_table_command is None:
            continue
        try:
            with _session_locks[device.id]:
                switch = _get_session(device)
                raw_mac_table = switch.run(mac_table_command)
            mac_table_by_device[device.id] = _MAC_TABLE_PARSER[device.platform](raw_mac_table)
        except Exception:
            log.warning("could not fetch MAC table from %s for topology", device.id, exc_info=True)

    # Port-channel membership (Dell OS9 only - see _PORT_CHANNEL_COMMAND).
    # Same best-effort tolerance as ARP/MAC-table above.
    port_channel_members_by_device = {}
    for device in DEVICES:
        pc_command = _PORT_CHANNEL_COMMAND.get(device.platform)
        if pc_command is None:
            continue
        try:
            with _session_locks[device.id]:
                switch = _get_session(device)
                raw_pc = switch.run(pc_command)
            port_channel_members_by_device[device.id] = _PORT_CHANNEL_PARSER[device.platform](raw_pc)
        except Exception:
            log.warning("could not fetch port-channel membership from %s for topology", device.id, exc_info=True)

    result = topology.build_topology(
        DEVICES,
        raw_by_device,
        errors_by_device,
        mac_to_ip=mac_to_ip,
        mac_table_by_device_id=mac_table_by_device,
        port_channel_members_by_device_id=port_channel_members_by_device,
    )

    def _iface_lookup(device_id):
        status = STATUS.get(device_id, include_interfaces=True)
        return {i["port"]: i for i in (status or {}).get("interfaces", [])}

    ifaces_by_device = {d.id: _iface_lookup(d.id) for d in DEVICES}

    def _endpoint_state(device_id, port):
        iface = ifaces_by_device.get(device_id, {}).get(port)
        if iface is None:
            return {"status": None, "input_mbps": None, "output_mbps": None}
        return {
            "status": iface.get("status"),
            "input_mbps": iface.get("input_mbps"),
            "output_mbps": iface.get("output_mbps"),
        }

    def _endpoint_state_multi(device_id, ports):
        # An external edge's `port` can be a port-channel name (e.g. "Po
        # 2") when the host was reached over a LAG - the status poller
        # only ever tracks physical interfaces (confirmed live: `show
        # interfaces status` has no "Po N" row), so state/utilization is
        # combined across the port-channel's actual physical members
        # instead of a single direct lookup that would always come up
        # empty for an aggregate name.
        states = [_endpoint_state(device_id, p) for p in ports]
        known_statuses = [s["status"] for s in states if s["status"]]
        status = "Up" if "Up" in known_statuses else (known_statuses[0] if known_statuses else None)
        ins = [s["input_mbps"] for s in states if s["input_mbps"] is not None]
        outs = [s["output_mbps"] for s in states if s["output_mbps"] is not None]
        return {
            "status": status,
            "input_mbps": sum(ins) if ins else None,
            "output_mbps": sum(outs) if outs else None,
        }

    for edge in result["edges"]:
        if edge["kind"] == "internal":
            edge["a"]["state"] = _endpoint_state(edge["a"]["device_id"], edge["a"]["port"])
            edge["b"]["state"] = _endpoint_state(edge["b"]["device_id"], edge["b"]["port"])
        else:
            edge["state"] = _endpoint_state_multi(edge["device_id"], edge.get("member_ports") or [edge["port"]])

    return result


@app.get("/api/topology")
def api_topology(user: str = Depends(require_auth_and_db)):
    """Fleet-wide topology from live LLDP data - fetched fresh on every
    call (no background poller for this) since topology changes rarely and
    there are only ever as many devices as are configured, so the extra
    per-device SSH round trip on page load/refresh is cheap."""
    result = _fetch_live_topology()
    result["lag_health"] = _lag_health(result["edges"])

    baseline = TOPOLOGY_STORE.get()
    result["baseline"] = (
        {"saved_at": baseline["saved_at"], "saved_by": baseline["saved_by"]} if baseline else None
    )
    result["drift"] = topology.diff_against_baseline(result["edges"], baseline["edges"] if baseline else None)
    return result


@app.post("/api/topology/baseline")
def api_save_topology_baseline(user: str = Depends(require_auth_and_db)):
    """"Relearn" - overwrites the whole baseline with exactly what's live
    right now, discarding any previously-accepted drift."""
    result = _fetch_live_topology()
    signatures = [topology.edge_signature(e) for e in result["edges"]]
    TOPOLOGY_STORE.save(signatures, saved_by=user)
    log.info("user=%s relearned the topology baseline (%d edges)", user, len(signatures))
    return {"ok": True, "edge_count": len(signatures)}


class TopologyBaselineAcceptRequest(BaseModel):
    added: list = []
    removed: list = []


@app.post("/api/topology/baseline/accept")
def api_accept_topology_drift(req: TopologyBaselineAcceptRequest, user: str = Depends(require_auth_and_db)):
    """Manually folds specific drift into the baseline (e.g. "yes, that
    link was intentionally moved") without discarding the rest of the
    baseline the way a full relearn would."""
    TOPOLOGY_STORE.accept(req.added, req.removed, saved_by=user)
    log.info("user=%s accepted topology drift (+%d/-%d)", user, len(req.added), len(req.removed))
    return {"ok": True}


@app.delete("/api/topology/baseline")
def api_clear_topology_baseline(user: str = Depends(require_auth_and_db)):
    TOPOLOGY_STORE.clear()
    log.info("user=%s cleared the topology baseline", user)
    return {"ok": True}


FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


class ImmutableCachedStaticFiles(StaticFiles):
    """Vite content-hashes every file under /static/assets/ (e.g.
    index-D546dtnJ.js) - a changed file gets a new name, so these can be
    cached by the browser forever. index.html itself is served separately,
    uncached, so it always points at the current hashed asset names."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# The Docker image always builds the frontend before the backend starts
# (see Dockerfile), so this directory exists in every real deployment -
# but importing this module shouldn't *require* that (backend-only tools,
# and the test suite's TestClient-based tests, have no reason to run
# `npm run build` first). Mounting on a missing directory raises
# immediately at import time otherwise, which is a needless coupling
# between two logically separate concerns.
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/static/assets", ImmutableCachedStaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")
else:
    log.warning("frontend/dist/assets not found - run `npm run build` in webui/frontend/; static assets won't be served")


@app.get("/")
def index(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    # Unauthenticated (and un-cached) until setup is complete, so the SPA
    # can boot and show the setup wizard - once CONFIGURED, this behaves
    # exactly like the old always-authenticated index route.
    if CONFIGURED:
        _check_auth(credentials)
    return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
