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
from urllib.parse import quote, urlencode, urlsplit
import uuid
from pathlib import Path
from typing import Optional

import psycopg2
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import junos_parsers
import alert_acks
import audit
import auth
import occurrences
import paging
from alertmanager_client import AlertmanagerClient, AlertmanagerError
import logging_setup
import metrics
import opnsense_parsers
import parsers
import settings as settings_store
from commands import COMMAND_TREES, command_exists, find_command
from db import Database
from devices import DeviceConfigError, StoredDevice, load_devices
from loki_client import LokiClient, LokiError
from results_store import ResultsStore
from scheduler import ScheduleStore
import alert_rules
import command_history
import compliance
import hardware_alerting
import interface_alerting
import retention
import dns_cache
import sflow_store
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
# How long an open occurrence must go without *any* instance reporting its
# alarm active before it's closed (see occurrences.stale_open). Needs to
# comfortably exceed the 3s sync tick plus a slow Alertmanager/Prometheus
# round trip, so a single slow or failed poll never ends a live alarm;
# 90s costs at most that much delay on a resolve, against the alternative
# of spuriously closing and reopening real alarms every few seconds.
OCCURRENCE_CLOSE_GRACE_SECONDS = int(os.environ.get("OCCURRENCE_CLOSE_GRACE_SECONDS", "90"))
# How quiet the syslog pipeline may go before the Settings health panel
# calls it stale. Generous by default: a small fleet can genuinely be
# silent for a while, and this should flag "the pipeline is dead", not
# "the switches had nothing to say for ten minutes".
SYSLOG_STALE_AFTER_SECONDS = int(os.environ.get("SYSLOG_STALE_AFTER_SECONDS", "1800"))
# Same idea for sFlow. Shorter than syslog's window because sFlow is
# continuous by nature - a switch with any traffic at all samples
# constantly, so silence means the pipeline is broken rather than "nothing
# happened to be said".
SFLOW_STALE_AFTER_SECONDS = int(os.environ.get("SFLOW_STALE_AFTER_SECONDS", "600"))
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
# Not scraped by the webui (Prometheus does that) - held only so the
# Settings page can report whether the exporter is actually reachable.
EXPORTER_URL = os.environ.get("EXPORTER_URL", "http://s4048-exporter:9101")
# Where sfacctd runs. Never connected to - flows arrive via Postgres - but
# named by the health panel so "no flows" comes with somewhere to look.
SFLOW_COLLECTOR = os.environ.get("SFLOW_COLLECTOR", "")

# Deployment config (Postgres DSN, Loki URL) lives in a small JSON file on
# the webui-data volume, editable from the in-app Settings page - see
# settings.py for why this can't just live in Postgres too. Falls back to
# env vars on a brand new volume so existing docker-compose deployments
# keep working unchanged; if neither is present the app still boots
# (rather than crashing) and serves a setup wizard instead of the normal UI
# until someone configures it.
LOKI_URL = None
DATABASE_URL = None
CONFIGURED = False
DB_ERROR = None

# Per-user identity via OIDC against an external, BYO Keycloak instance -
# replaces the old single shared HTTP Basic Auth credential (ROADMAP Phase
# 1 "the gate on anyone other than you using this"). Keycloak itself is not
# part of this stack; these just point at wherever it already runs (see
# webui/README.md for the exact client/role setup required on that end).
# Deliberately env-var only, not Settings-page-editable like the DSN above:
# this is infrastructure config (which identity provider to trust), not a
# per-deployment operational knob, and shouldn't be changeable by whoever
# is merely logged in as an admin *inside* the app.
OIDC_ISSUER_URL = os.environ.get("OIDC_ISSUER_URL")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "switchboard")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI")
# Signs the session cookie (Starlette's SessionMiddleware) - not the same
# secret as the OIDC client secret. Must be set explicitly in production;
# a random per-process fallback just means every restart invalidates all
# sessions, which is safe (if mildly annoying) rather than a security hole.
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY") or secrets.token_hex(32)
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_TTL_HOURS = float(os.environ.get("SESSION_TTL_HOURS", "12"))

oidc_client = None
if OIDC_ISSUER_URL and OIDC_CLIENT_SECRET:
    oidc_client = auth.build_oauth_client(OIDC_ISSUER_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET)
else:
    log.warning("OIDC_ISSUER_URL/OIDC_CLIENT_SECRET not set - login will not work until configured")


def _session_expired(session):
    expires_at = session.get("expires_at")
    return not expires_at or datetime.now(timezone.utc).timestamp() > expires_at


# Sessions here are signed cookies with no server-side table (see the
# plan's SessionMiddleware correction) - there's nothing to look up to
# force-end one early. Back-Channel Logout (api_auth_backchannel_logout
# below) is the one case that needs exactly that: Keycloak calls us
# directly, server-to-server, when a session ends anywhere (admin-revoked,
# logged out from another app sharing this SSO session, etc.), and the
# only way to honor that against a stateless cookie is a small in-memory
# revocation list keyed by Keycloak's own session id (sid claim, stored in
# our session at login). Bounded by pruning anything older than the
# longest a session could legitimately still be alive for.
_revoked_sids = {}
_revoked_sids_lock = threading.Lock()


def _revoke_sid(sid):
    now = time.time()
    with _revoked_sids_lock:
        _revoked_sids[sid] = now
        cutoff = now - SESSION_TTL_HOURS * 3600
        for stale_sid in [s for s, revoked_at in _revoked_sids.items() if revoked_at < cutoff]:
            del _revoked_sids[stale_sid]


def _is_sid_revoked(sid):
    with _revoked_sids_lock:
        return sid in _revoked_sids


def require_auth(request: Request):
    session = request.session
    # A role is required here, not just a username - api_auth_callback
    # refuses to create a session at all for a Keycloak login with no
    # recognized client role, but this is the actual enforcement backstop:
    # even a read-only route must not treat "logged in" and "has a role"
    # as the same thing (confirmed live: without this check, a
    # username-only session could still list every device - a real gap,
    # not hypothetical).
    if not session.get("username") or not session.get("role") or _session_expired(session):
        raise HTTPException(status_code=401, detail="Not logged in")
    if session.get("sid") and _is_sid_revoked(session["sid"]):
        # Keycloak told us (via backchannel logout) this session already
        # ended - the cookie is still validly signed but no longer honored.
        raise HTTPException(status_code=401, detail="Session was ended")
    return session["username"]


def require_auth_and_db(request: Request):
    user = require_auth(request)
    if STORE is None:
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable ({DB_ERROR}). Fix the connection on the Settings page.",
        )
    return user


def require_role(min_role):
    """Dependency factory - same session lookup as require_auth_and_db,
    plus a role floor. `viewer < operator < admin`, checked against the
    role captured in the session at login time (from Keycloak client
    roles - see auth.role_from_claims)."""
    def _dep(request: Request, user: str = Depends(require_auth_and_db)):
        # `user` comes through Depends() rather than a direct call, so
        # tests overriding require_auth_and_db via
        # app.dependency_overrides (see test_api_run_params.py) still work
        # for every route this wraps - FastAPI resolves overrides through
        # the whole sub-dependency graph, not just top-level Depends().
        # No default here deliberately - a session missing a role entirely
        # (shouldn't happen; api_auth_callback refuses to create one
        # without a real role) must fail role_meets, not silently pass as
        # viewer.
        role = request.session.get("role")
        if not auth.role_meets(role, min_role):
            raise HTTPException(status_code=403, detail=f"requires {min_role} role, you have {role}")
        return user
    return _dep


# Named once, not called inline as `Depends(require_operator)` at
# every route - FastAPI's dependency_overrides (used by tests, e.g.
# test_api_run_params.py) keys on the exact callable object, and a fresh
# closure from a fresh require_role(...) call wouldn't match one used
# elsewhere.
require_operator = require_role("operator")
require_admin = require_role("admin")


def require_role_no_db(min_role):
    """Same as require_role, but built on require_auth, not
    require_auth_and_db - a role check that doesn't itself require the
    database to be reachable. Exists for exactly one route: PUT
    /api/settings. require_admin (built on require_auth_and_db) 503s
    whenever STORE is None, i.e. whenever the DB connection is broken -
    which is precisely the situation this route exists to fix. Wiring it
    through require_admin recreates the circular dependency
    require_auth/require_auth_and_db's own split was originally
    introduced to avoid for this exact page (see the design notes on
    SessionMiddleware vs. a DB-backed session store) - confirmed live: a
    real admin, with a real broken DATABASE_URL, got a 503 trying to fix
    it, unable to recover without direct file/DB access. This must never
    happen again for this route."""
    def _dep(request: Request, user: str = Depends(require_auth)):
        role = request.session.get("role")
        if not auth.role_meets(role, min_role):
            raise HTTPException(status_code=403, detail=f"requires {min_role} role, you have {role}")
        return user
    return _dep


require_admin_no_db = require_role_no_db("admin")


app = FastAPI(title="Switchboard")
app.add_middleware(GZipMiddleware, minimum_size=500)
# SameSite=Lax + JSON-only mutating bodies is this app's CSRF defense (no
# CORS middleware exists or is added, so a cross-site form POST has nowhere
# to succeed) - see webui/README.md for the full reasoning. Secure is only
# enabled once SESSION_COOKIE_SECURE=true, i.e. once TLS is actually
# terminating in front of this app (a separate, still-open ROADMAP item);
# forcing it before then would break every login.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="switchboard_session",
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
)


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
COMMAND_HISTORY = None
FAVORITES = None
DNS = dns_cache.DnsCache()

SFLOW = None
NETFLOW = None
SFLOW_IFINDEX = None
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
# Same in-memory, not-reset-on-DB-reconfigure treatment as
# INTERFACE_ALERT_CHECKER above, for the same reason - see
# hardware_alerting.py's HardwareAlertChecker docstring.
HARDWARE_ALERT_CHECKER = hardware_alerting.HardwareAlertChecker()
HARDWARE_ALERT_CHECKER.reseed_from_alertmanager(ALERTMANAGER)
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

# Retention for every growing table (see retention.py), not just trend
# samples. A plain daemon thread rather than a scheduler dependency;
# prune_all() is a no-op until DB is actually configured.
#
# Prunes on startup *before* the first sleep, which the previous version of
# this loop did not: it slept 24h first and had no startup call, so on a
# process redeployed several times a day the prune realistically never ran
# at all - `metric_samples` was found at 2.03M rows / 493 MB, roughly 3x
# its size six days earlier, despite nominally having a 90-day policy since
# early on. The startup pass is what makes the policy real rather than
# aspirational, and it's cheap: the DELETEs match nothing once caught up.
def _retention_loop():
    while True:
        if DB is None:
            # This thread starts at import time, ~500 lines before settings
            # are applied and DB actually exists. Sleeping a full day here
            # would mean the startup prune silently never happens on a
            # fresh boot - the same "never runs" outcome as the loop this
            # replaced, just reached a different way. Confirmed live: the
            # first version of this fix planted a 400-day-old row, restarted,
            # and the row was still there. Poll briefly until configured
            # instead, then fall into the daily cadence.
            time.sleep(60)
            continue
        try:
            retention.prune_all(DB)
        except Exception:
            log.exception("retention pruning failed")
        time.sleep(24 * 3600)


threading.Thread(target=_retention_loop, daemon=True, name="retention-pruner").start()


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


def _env_and_polled_at_for(device_id):
    """Like _port_state_and_polled_at_for, but the whole environment
    (fans/psus) rather than one interface - hardware_alerting.py's
    reconcile_via_poll needs both the raw show-environment shape and the
    freshness timestamp for the same stale-read-hazard reasons
    interface_alerting.py's reconcile_via_poll does."""
    status = STATUS.get(device_id)
    if status is None:
        return None, None
    return status.get("env"), status.get("last_polled")


# Fan/PSU hardware alerting - same syslog-primary/poll-fallback shape as
# interface alerting above, minus the immediate/delayed mode split (no
# per-entity opt-in table here - see hardware_alerting.py for why). Only
# two loops are needed, not three: reconcile_via_poll here does both the
# fire and resolve/restart-recovery jobs interface_alerting.py splits
# across check_once and reconcile_via_poll, since there's no "confirmed
# down for N seconds" delayed-mode concept to keep separate.
def _hardware_alert_syslog_loop():
    while True:
        time.sleep(3)
        if LOKI is None:
            continue
        try:
            HARDWARE_ALERT_CHECKER.check_via_syslog(LOKI, DEVICES_BY_ID, ALERTMANAGER, _device_name_for)
        except Exception:
            log.exception("hardware alert syslog check failed")


threading.Thread(target=_hardware_alert_syslog_loop, daemon=True, name="hardware-alert-syslog-checker").start()


def _hardware_alert_reconcile_loop():
    while True:
        time.sleep(10)
        try:
            HARDWARE_ALERT_CHECKER.reconcile_via_poll(
                list(DEVICES_BY_ID.keys()), _env_and_polled_at_for, _device_name_for, ALERTMANAGER
            )
        except Exception:
            log.exception("hardware alert reconcile check failed")


threading.Thread(target=_hardware_alert_reconcile_loop, daemon=True, name="hardware-alert-reconciler").start()


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
    # Record that everything we *can* see is genuinely still active, before
    # deciding what to close. Closing is deliberately evidence-based -
    # "nobody has seen this active for a while" - rather than absence-based
    # ("it isn't in my view this instant"), because this instance's view is
    # not always complete. Found live: a second Switchboard sharing this
    # database reconciles against its own Alertmanager, so each instance
    # kept closing occurrences the other had just opened and the other
    # immediately reopened them - ~19,800 junk rows for one continuously
    # down device. The same flaw bites a single instance whenever
    # Alertmanager or Prometheus blips for one tick. See db.py's
    # last_seen_at comment.
    never_closing = firing_signatures | pending_signatures
    for signature in never_closing:
        OCCURRENCES.touch(signature)

    for occurrence in OCCURRENCES.stale_open(OCCURRENCE_CLOSE_GRACE_SECONDS):
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
    global OCCURRENCES, AUDIT, DEVICES, DEVICES_BY_ID, COMMAND_HISTORY, FAVORITES, SFLOW, SFLOW_IFINDEX, NETFLOW
    new_db = Database(dsn)
    new_store = DeviceStore(new_db)
    new_results = ResultsStore(new_db)
    new_topology_store = TopologyStore(new_db)
    new_schedules = ScheduleStore(new_db)
    new_alert_rules = alert_rules.AlertRuleStore(new_db)
    new_interface_alert_rules = interface_alerting.InterfaceAlertConfigStore(new_db)
    new_occurrences = occurrences.OccurrenceStore(new_db)
    new_audit = audit.AuditLog(new_db)
    new_command_history = command_history.CommandHistoryStore(new_db)
    new_favorites = command_history.CommandFavoritesStore(new_db)
    new_sflow = sflow_store.SFlowStore(new_db, source="switches")
    new_netflow = sflow_store.SFlowStore(new_db, source="firewall")
    new_ifindex = sflow_store.IfIndexMap(new_db)
    DB, STORE, RESULTS, TOPOLOGY_STORE, SCHEDULES, ALERT_RULES, INTERFACE_ALERT_RULES = (
        new_db, new_store, new_results, new_topology_store, new_schedules, new_alert_rules, new_interface_alert_rules
    )
    OCCURRENCES, AUDIT = new_occurrences, new_audit
    COMMAND_HISTORY, FAVORITES = new_command_history, new_favorites
    SFLOW = new_sflow
    NETFLOW = new_netflow
    SFLOW_IFINDEX = new_ifindex

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
    global LOKI_URL, DATABASE_URL, CONFIGURED, DB_ERROR, LOKI
    global ALERTMANAGER_URL, PROMETHEUS_URL, PROMETHEUS_RELOAD_URL, EXPORTER_URL
    global ALERTMANAGER, PAGER
    # Validate the DSN before committing any globals, so a failed update
    # (e.g. a typo'd Postgres URL) can't half-apply - the previously-working
    # DB connection is left untouched.
    _load_database(settings_dict["database_url"])
    _apply_service_settings(settings_dict)
    DATABASE_URL = settings_dict["database_url"]
    CONFIGURED = True
    DB_ERROR = None


def _apply_service_settings(settings_dict):
    """The non-database half of _apply_settings, split out because it
    cannot fail and must not be gated behind a working Postgres.

    That gating is the exact shape of a bug already fixed once here: the
    Settings page is where a broken deployment gets repaired, so anything
    on it that requires the database to already work is unreachable
    precisely when it's needed. An admin whose Postgres is down must still
    be able to correct the Alertmanager or Loki address."""
    global LOKI_URL, LOKI, ALERTMANAGER_URL, PROMETHEUS_URL, PROMETHEUS_RELOAD_URL
    global EXPORTER_URL, ALERTMANAGER, PAGER, SFLOW_COLLECTOR
    LOKI_URL = settings_dict.get("loki_url") or settings_store.DEFAULT_LOKI_URL
    LOKI = LokiClient(LOKI_URL)
    ALERTMANAGER_URL = settings_dict.get("alertmanager_url") or ALERTMANAGER_URL
    PROMETHEUS_URL = settings_dict.get("prometheus_url") or PROMETHEUS_URL
    PROMETHEUS_RELOAD_URL = settings_store.reload_url_for(settings_dict) or PROMETHEUS_RELOAD_URL
    EXPORTER_URL = settings_dict.get("exporter_url") or EXPORTER_URL
    # Blank is a legitimate value here ("not recorded"), so this one is
    # assigned as given rather than falling back to the previous value.
    SFLOW_COLLECTOR = settings_dict.get("sflow_collector", SFLOW_COLLECTOR) or ""
    # Rebuilt rather than mutated so a URL change takes effect immediately
    # instead of at the next restart. PAGER holds its own reference to the
    # client, so it has to be rebuilt too or it keeps talking to the old
    # address - a silent failure where holds would be placed nowhere.
    ALERTMANAGER = AlertmanagerClient(ALERTMANAGER_URL)
    PAGER = paging.PagingController(ALERTMANAGER, PAGE_DELAY_SECONDS)


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
    database_url: str
    loki_url: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    database_url: Optional[str] = None  # blank = keep current
    loki_url: Optional[str] = None
    alertmanager_url: Optional[str] = None
    prometheus_url: Optional[str] = None
    # Blank is meaningful here, unlike the others: it means "derive from
    # prometheus_url" (settings.reload_url_for).
    prometheus_reload_url: Optional[str] = None
    exporter_url: Optional[str] = None
    # Blank is meaningful: "collector address not recorded".
    sflow_collector: Optional[str] = None


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
    login yet to authenticate with (login is now handled by the external
    Keycloak instance, configured via env vars, not through this wizard),
    but locked out entirely once CONFIGURED so it can't be used to
    reconfigure a running deployment without an admin login."""
    if CONFIGURED:
        raise HTTPException(status_code=403, detail="Switchboard is already configured")
    if not req.database_url.strip():
        raise HTTPException(status_code=400, detail="a Postgres connection string is required")

    new_settings = {
        "database_url": req.database_url.strip(),
        "loki_url": (req.loki_url or "").strip() or settings_store.DEFAULT_LOKI_URL,
    }
    try:
        _apply_settings(new_settings)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not connect to Postgres: {e}")
    settings_store.save(new_settings)
    log.info("initial setup completed")
    return {"ok": True}


@app.get("/api/settings")
def api_get_settings(user: str = Depends(require_auth)):
    return {
        "database_url_display": settings_store.redact_dsn(DATABASE_URL) if DATABASE_URL else None,
        "loki_url": LOKI_URL,
        "alertmanager_url": ALERTMANAGER_URL,
        "prometheus_url": PROMETHEUS_URL,
        "prometheus_reload_url": PROMETHEUS_RELOAD_URL,
        "exporter_url": EXPORTER_URL,
        "sflow_collector": SFLOW_COLLECTOR,
        "db_error": DB_ERROR,
    }


def _probe(url, timeout=3):
    """One service health check. Returns (ok, detail).

    Deliberately treats any HTTP response as "reachable": a 404 from a
    wrong path still proves something is listening and answering, which is
    a different (and much more useful) diagnosis than a refused connection
    or a DNS failure. Reporting both as a bare "down" is what makes a
    typo'd path look identical to a dead host."""
    if not url:
        return False, "not configured"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return True, f"reachable, HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, str(e.reason)
    except Exception as e:  # socket timeouts, malformed URLs
        return False, str(e)


@app.get("/api/settings/health")
def api_settings_health(user: str = Depends(require_auth)):
    """Live reachability of every configured service.

    require_auth, not require_auth_and_db: this is a diagnostic page, and
    a broken database is exactly when someone needs to look at it. Gating
    it behind the DB would blank the whole panel at the one moment it
    matters - the same mistake that once made the Settings page itself
    unusable when Postgres was down."""
    checks = []

    if STORE is not None and DB_ERROR is None:
        checks.append({"name": "Postgres", "target": settings_store.redact_dsn(DATABASE_URL or ""),
                       "ok": True, "detail": "connected"})
    else:
        checks.append({"name": "Postgres", "target": settings_store.redact_dsn(DATABASE_URL or ""),
                       "ok": False, "detail": DB_ERROR or "not configured"})

    # Loki gets a second, separate check: whether anything is still
    # *arriving*. "/ready answers" and "syslog is flowing" are different
    # questions and only the first was ever asked - confirmed the hard
    # way when the Vector host stayed down for seven days while this panel
    # showed Loki reachable throughout, and the Syslog tab simply went
    # quiet with nothing anywhere saying why.
    # Always emitted, even unconfigured. A check that silently disappears
    # when something is wrong is the same class of failure this row exists
    # to catch - the panel must never look complete while quietly omitting
    # the one thing that was broken.
    if LOKI is None or not LOKI_URL:
        checks.append({"name": "Syslog flow", "target": LOKI_URL or None, "ok": False,
                       "detail": "not configured"})
    else:
        try:
            age = LOKI.newest_entry_age_seconds()
            if age is None:
                checks.append({"name": "Syslog flow", "target": LOKI_URL, "ok": False,
                               "detail": "no syslog received in the last 24h"})
            else:
                stale_after = SYSLOG_STALE_AFTER_SECONDS
                mins = age / 60
                checks.append({
                    "name": "Syslog flow", "target": LOKI_URL, "ok": age <= stale_after,
                    "detail": (f"last event {mins:.0f} min ago" if age > 90
                               else f"last event {age:.0f}s ago"),
                })
        except Exception as e:
            checks.append({"name": "Syslog flow", "target": LOKI_URL, "ok": False,
                           "detail": f"could not query Loki: {e}"})

    # sFlow health is "are flows arriving", not "can we reach the
    # collector": sfacctd listens on UDP and exposes no HTTP or TCP
    # endpoint, so a connection probe would read red while it worked
    # perfectly. Freshness also covers every failure mode - collector
    # down, switch stopped sampling, network path lost - rather than one.
    # Both flow pipelines get the same freshness check. NetFlow has an
    # extra way to go quiet that sFlow does not: v9 sends data records and
    # the templates describing them separately, so an exporter that stops
    # re-sending templates leaves the collector receiving traffic and
    # storing none of it.
    for label, store_ in (("sFlow flow", SFLOW), ("NetFlow flow", NETFLOW)):
        if store_ is None or STORE is None or DB_ERROR is not None:
            continue
        target = SFLOW_COLLECTOR or "collector address not set"
        try:
            age = store_.newest_age_seconds()
            if age is None:
                checks.append({"name": label, "target": target, "ok": False,
                               "detail": "no flow records have ever arrived"})
            else:
                checks.append({
                    "name": label, "target": target,
                    "ok": age <= SFLOW_STALE_AFTER_SECONDS,
                    "detail": (f"last flow {age/60:.0f} min ago" if age > 90
                               else f"last flow {age:.0f}s ago"),
                })
        except Exception as e:
            checks.append({"name": label, "target": target, "ok": False,
                           "detail": f"could not query flows: {e}"})

    for name, url, path in (
        ("Loki", LOKI_URL, "/ready"),
        ("Alertmanager", ALERTMANAGER_URL, "/-/healthy"),
        ("Prometheus", PROMETHEUS_URL, "/-/healthy"),
        ("Exporter", EXPORTER_URL, "/metrics"),
    ):
        base = (url or "").rstrip("/")
        ok_, detail = _probe(f"{base}{path}" if base else "")
        checks.append({"name": name, "target": url, "ok": ok_, "detail": detail})

    # Two of these ask "is data still arriving?", not "does the endpoint
    # answer?". A stale pipeline and an unreachable host are different
    # failures with different first moves, so the panel must not call
    # them by the same word - Loki answered /ready for the whole seven
    # days that no syslog was reaching it.
    flow_checks = {"Syslog flow", "sFlow flow", "NetFlow flow"}
    for c in checks:
        c["kind"] = "flow" if c["name"] in flow_checks else "reach"

    return {"checks": checks}


@app.put("/api/settings")
def api_update_settings(req: SettingsUpdateRequest, user: str = Depends(require_admin_no_db)):
    current = settings_store.load() or {}
    new_settings = dict(current)
    new_settings["database_url"] = (req.database_url or "").strip() or DATABASE_URL
    for key, _env, fallback in settings_store.SERVICE_SETTINGS:
        submitted = getattr(req, key, None)
        if submitted is None:
            continue  # field omitted entirely - keep whatever is stored
        submitted = submitted.strip()
        # prometheus_reload_url is legitimately blank (it derives from
        # prometheus_url); the rest fall back rather than being blanked.
        blank_ok = key in ("prometheus_reload_url", "sflow_collector")
        new_settings[key] = submitted or ("" if blank_ok else fallback)

    # The service URLs are applied and saved first, and never gated behind
    # Postgres: this page is where a broken deployment gets fixed, so an
    # unreachable database must not block correcting an unrelated address.
    _apply_service_settings(new_settings)
    settings_store.save(new_settings)

    try:
        _apply_settings(new_settings)
    except Exception as e:
        log.warning("user=%s saved settings, but Postgres is unreachable: %s", user, e)
        raise HTTPException(
            status_code=400,
            detail=f"Saved, but could not connect to Postgres: {e}",
        )
    log.info("user=%s updated deployment settings", user)
    return {"ok": True}


# Light per-IP throttle on the token-exchange endpoint - defense in depth,
# not the primary brute-force protection (that's Keycloak's job now, same
# as any OIDC-fronted app). In-memory, same proportionate spirit as other
# in-process state in this file (e.g. PENDING_HOLDS above) - losing it on
# restart just resets the window, not a security regression.
_auth_attempts = {}
_auth_attempts_lock = threading.Lock()


def _check_callback_rate_limit(ip):
    now = time.monotonic()
    with _auth_attempts_lock:
        attempts = [t for t in _auth_attempts.get(ip, []) if now - t < 60]
        attempts.append(now)
        _auth_attempts[ip] = attempts
        if len(attempts) > 10:
            raise HTTPException(status_code=429, detail="too many login attempts, try again shortly")


# A stale callback is the common case, not an error worth showing anyone.
# The OIDC state and nonce live in the session cookie; if that expired, was
# cleared, or the callback URL was re-opened from history, Authlib rejects
# the exchange even though nothing is actually wrong - and the user, who
# usually still has a live Keycloak SSO session, is one redirect away from
# being logged in. So the callback restarts the login instead of dead-ending
# on a JSON 401.
#
# The obvious hazard is a redirect loop: a genuinely broken setup (wrong
# client secret, clock skew, a revoked client) fails every time, and
# retrying forever would spin the browser between two hosts with nothing on
# screen. So attempts are counted in their own short-lived cookie - not the
# session, which is the very thing that may be missing - and once the count
# is exhausted the failure is shown as a real page.
LOGIN_RETRY_COOKIE = "switchboard_login_retry"
MAX_LOGIN_RETRIES = int(os.environ.get("OIDC_MAX_LOGIN_RETRIES", "2"))


def _login_retry_count(request):
    try:
        return max(0, int(request.cookies.get(LOGIN_RETRY_COOKIE, "0")))
    except (TypeError, ValueError):
        return 0


def _clear_login_retries(response):
    response.delete_cookie(LOGIN_RETRY_COOKIE, path="/")
    return response


def _retry_login(request, attempts, reason):
    """Send the browser back through Keycloak for one more attempt."""
    response = RedirectResponse(url="/api/auth/login", status_code=302)
    response.set_cookie(
        LOGIN_RETRY_COOKIE, str(attempts + 1),
        max_age=300, httponly=True, samesite="lax",
        secure=SESSION_COOKIE_SECURE, path="/",
    )
    log.info("OIDC callback failed (%s) - retrying login, attempt %d of %d",
             reason, attempts + 1, MAX_LOGIN_RETRIES)
    return response


@app.get("/api/auth/login")
async def api_auth_login(request: Request):
    if oidc_client is None:
        raise HTTPException(status_code=503, detail="OIDC is not configured (OIDC_ISSUER_URL/OIDC_CLIENT_SECRET missing)")
    redirect_uri = OIDC_REDIRECT_URI or str(request.url_for("api_auth_callback"))
    return await oidc_client.authorize_redirect(request, redirect_uri)


@app.get("/api/auth/callback")
async def api_auth_callback(request: Request):
    """Server-to-server code exchange + ID token validation (signature via
    JWKS, iss/aud/exp/nonce) all handled by Authlib - see auth.py's
    docstring for why that's not hand-rolled here."""
    if oidc_client is None:
        raise HTTPException(status_code=503, detail="OIDC is not configured")
    _check_callback_rate_limit(request.client.host if request.client else "unknown")
    attempts = _login_retry_count(request)
    try:
        token = await oidc_client.authorize_access_token(request)
    except Exception as e:
        if attempts < MAX_LOGIN_RETRIES:
            return _retry_login(request, attempts, e)
        # Retried and still failing, so this is not a stale cookie. Show a
        # page rather than a bare JSON 401: it names the underlying error,
        # and it carries a Log out button, which is the one control that
        # actually breaks the cycle - it ends the Keycloak session that
        # keeps silently re-authenticating into the same failure.
        log.warning("OIDC callback failed after %d retries: %s", attempts, e)
        return _clear_login_retries(
            RedirectResponse(url=f"/#/login-failed/{quote(str(e)[:200])}", status_code=302))
    claims = token.get("userinfo") or {}
    # Also check the /userinfo endpoint directly, not just the ID token - a
    # Keycloak client-role mapper can be scoped to one and not the other
    # independently, so a setup that only emits resource_access on one of
    # the two still works here rather than failing depending on which one
    # got configured (confirmed live: this exact gap is what caused
    # role_from_claims to see nothing at all during initial setup).
    try:
        userinfo_endpoint_claims = await oidc_client.userinfo(token=token)
    except Exception as e:
        userinfo_endpoint_claims = None
        log.warning("could not call /userinfo endpoint: %s", e)
    if userinfo_endpoint_claims and userinfo_endpoint_claims.get("resource_access") and not claims.get("resource_access"):
        claims = {**claims, "resource_access": userinfo_endpoint_claims["resource_access"]}
    username = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="OIDC token had no usable identity claim")
    role = auth.role_from_claims(claims, OIDC_CLIENT_ID)
    if role is None:
        # No session is created at all - a valid Keycloak login is not the
        # same as being granted anything in this app. Denied outright
        # rather than falling back to viewer, so "no role assigned" reads
        # as "no access", not "read-only access by default". Redirects to
        # a real page (not a bare JSON 403) so there's an actual logout
        # button to escape the loop instead of a dead end.
        if AUDIT is not None:
            AUDIT.record(username, "auth.denied", detail={"reason": "no client role assigned"})
        log.warning("user=%s authenticated via OIDC but has no switchboard client role - denying", username)
        return _clear_login_retries(RedirectResponse(url=f"/#/access-denied/{quote(username)}"))
    request.session["username"] = username
    request.session["email"] = claims.get("email")
    request.session["role"] = role
    # The raw client-role claim this role was computed from, not just the
    # end result - lets the Account page show exactly which roles were
    # assigned, which is the actual question when someone's permissions
    # look wrong.
    request.session["roles_claim"] = list(
        ((claims.get("resource_access") or {}).get(OIDC_CLIENT_ID) or {}).get("roles") or []
    )
    request.session["login_at"] = datetime.now(timezone.utc).isoformat()
    request.session["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).timestamp()
    # Kept only for RP-initiated logout's id_token_hint (see api_auth_logout)
    # - without it, clearing our own session cookie doesn't end Keycloak's
    # own SSO session, so an immediate re-login silently succeeds with no
    # prompt (confirmed live: this was exactly why logout looked broken
    # before this was added).
    request.session["id_token"] = token.get("id_token")
    # Keycloak's own SSO session id - lets a Back-Channel Logout call (see
    # api_auth_backchannel_logout) revoke exactly this session later
    # without needing a server-side session table for anything else.
    request.session["sid"] = claims.get("sid")
    if AUDIT is not None:
        AUDIT.record(username, "auth.login", detail={"role": role})
    log.info("user=%s logged in via OIDC (role=%s)", username, role)
    return _clear_login_retries(RedirectResponse(url="/"))


@app.get("/api/auth/logout")
async def api_auth_logout(request: Request):
    """Clearing our own session cookie alone isn't a real logout - Keycloak
    keeps its own SSO session in the browser, so the very next
    /api/auth/login would silently re-authenticate with no prompt at all
    (confirmed live - this was the exact bug reported: "logout doesn't
    work"). RP-Initiated Logout (the end_session_endpoint from OIDC
    discovery) is what actually ends that SSO session too; the browser is
    redirected there, not fetched, since a page navigation is what's
    needed to hit a different origin and come back.
    """
    username = request.session.get("username")
    id_token = request.session.get("id_token")
    request.session.clear()
    if username and AUDIT is not None:
        AUDIT.record(username, "auth.logout")
    if oidc_client is None:
        return RedirectResponse(url="/")
    try:
        metadata = await oidc_client.load_server_metadata()
        end_session_endpoint = metadata.get("end_session_endpoint")
    except Exception as e:
        log.warning("could not load OIDC server metadata for logout: %s", e)
        end_session_endpoint = None
    if not end_session_endpoint:
        # No RP-initiated logout support on this issuer - our own session is
        # already cleared above, which is the best we can do.
        return RedirectResponse(url="/")
    # Keycloak's SSO session cookie is scoped to wherever OIDC_REDIRECT_URI
    # points, so post-logout lands back at that same origin.
    origin = urlsplit(OIDC_REDIRECT_URI).scheme + "://" + urlsplit(OIDC_REDIRECT_URI).netloc + "/"
    params = {"client_id": OIDC_CLIENT_ID, "post_logout_redirect_uri": origin}
    if id_token:
        params["id_token_hint"] = id_token
    return RedirectResponse(url=f"{end_session_endpoint}?{urlencode(params)}")


@app.post("/api/auth/backchannel-logout")
async def api_auth_backchannel_logout(request: Request):
    """OIDC Back-Channel Logout 1.0 receiver - Keycloak calls this directly,
    server-to-server (no browser, no cookie), whenever a session ends any
    way other than clicking this app's own Log out button: an admin
    revoking a session in Keycloak, logging out from another app sharing
    the same SSO session, etc. Without this, only our own /api/auth/logout
    ends a session here - anyone whose Keycloak session ended some other
    way would keep a working Switchboard cookie until it naturally expires
    (SESSION_TTL_HOURS).

    Requires two things on the Keycloak side to ever actually fire: the
    switchboard client's "Backchannel logout URL" set to this exact route,
    and - the easy part to miss - that URL must be reachable from
    Keycloak's own server, not the user's browser. If Switchboard is only
    reachable at a private/localhost address from your machine and
    Keycloak runs elsewhere, Keycloak's server has no way to call back in;
    this isn't a bug here, it's the spec's own network requirement.
    """
    if oidc_client is None:
        raise HTTPException(status_code=503, detail="OIDC is not configured")
    form = await request.form()
    logout_token = form.get("logout_token")
    if not logout_token:
        raise HTTPException(status_code=400, detail="missing logout_token")
    try:
        metadata = await oidc_client.load_server_metadata()
        jwks_uri = metadata.get("jwks_uri")
        if not jwks_uri:
            raise ValueError("issuer metadata has no jwks_uri")
        sid, sub = await auth.verify_logout_token(logout_token, OIDC_ISSUER_URL, OIDC_CLIENT_ID, jwks_uri)
    except Exception as e:
        log.warning("rejected backchannel logout token: %s", e)
        raise HTTPException(status_code=400, detail="invalid logout_token")
    if sid:
        _revoke_sid(sid)
    log.info("backchannel logout: sid=%s sub=%s", sid, sub)
    if AUDIT is not None:
        AUDIT.record(sub or "unknown", "auth.backchannel_logout", detail={"sid": sid})
    # Spec requires 200 with no body on success - Keycloak treats anything
    # else as this endpoint having failed to process the logout.
    return Response(status_code=200)


@app.get("/api/auth/me")
def api_auth_me(request: Request, user: str = Depends(require_auth)):
    """Backs both the TopNav's username/role display and the Account page
    (see AccountPage.jsx) - the latter needs more than just
    username/role: the raw roles_claim so someone can see *why* they got
    the role they got, and account_url so the app can point at Keycloak's
    own self-service console without the frontend needing to know the
    issuer URL itself. Passwords/MFA/sessions are deliberately not
    manageable here - that's Keycloak's job, linked out to, not
    reimplemented."""
    return {
        "username": user,
        "email": request.session.get("email"),
        "role": request.session.get("role"),
        "roles_claim": request.session.get("roles_claim", []),
        "login_at": request.session.get("login_at"),
        "expires_at": request.session.get("expires_at"),
        "account_url": f"{OIDC_ISSUER_URL.rstrip('/')}/account" if OIDC_ISSUER_URL else None,
    }


@app.get("/api/devices")
def api_devices(user: str = Depends(require_auth_and_db)):
    return [d.to_public_dict() for d in DEVICES]


@app.post("/api/devices")
def api_create_device(req: DeviceCreateRequest, user: str = Depends(require_admin)):
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
def api_update_device(device_id: str, req: DeviceCreateRequest, user: str = Depends(require_admin)):
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
def api_delete_device(device_id: str, user: str = Depends(require_admin)):
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
def api_device_status_refresh(device_id: str, user: str = Depends(require_operator)):
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
def api_test_device(req: DeviceCreateRequest, user: str = Depends(require_operator)):
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


def _run_and_save(device, category_id, command_id, params, user, auto_saved=True, source="console"):
    """Shared by /api/run, bulk-run, and the scheduler - resolves the
    allowlisted command, runs it over the device's locked SSH session, and
    auto-saves the result the same way every code path expects. Raises
    CommandLookupError/SwitchSSHError/DeviceConfigError; callers decide
    whether to turn that into an HTTP error (single-device) or a per-device
    error entry (bulk/scheduled).

    This is also where command history is recorded, precisely because it's
    the one point all three run paths already funnel through - recording
    in the routes instead would have meant three call sites and a fourth
    one silently missing it the next time a run path is added. Failures
    are recorded as well as successes (see command_history.py)."""
    cmd = _resolve_command(device, category_id, command_id, params)
    log.info("user=%s device=%s running: %s", user, device.id, cmd)

    metrics.command_run_total.labels(device_id=device.id, platform=device.platform).inc()
    start = time.monotonic()
    try:
        with _session_locks[device.id]:
            switch = _get_session(device)
            output = switch.run(cmd)
    except Exception as e:
        if COMMAND_HISTORY is not None:
            COMMAND_HISTORY.record(
                user, device.id, device.name, category_id, command_id, cmd, params=params,
                status=command_history.STATUS_ERROR, error=str(e),
                duration_ms=int((time.monotonic() - start) * 1000), source=source,
            )
        raise
    finally:
        metrics.command_run_duration_seconds.labels(device_id=device.id).observe(time.monotonic() - start)

    duration_ms = int((time.monotonic() - start) * 1000)
    summary = summarize(device.platform, category_id, command_id, output)
    saved = RESULTS.save(
        device.id, device.name, device.host, category_id, command_id, cmd, summary, output,
        auto_saved=auto_saved, actor=user,
    )
    if COMMAND_HISTORY is not None:
        COMMAND_HISTORY.record(
            user, device.id, device.name, category_id, command_id, cmd, params=params,
            status=command_history.STATUS_OK, duration_ms=duration_ms,
            result_filename=saved["filename"], source=source,
        )
    if AUDIT is not None:
        # The audit trail's own entry for the same event - see
        # command_history.py's module docstring for why both exist.
        AUDIT.record(user, "command.run", device.id, cmd)
    return {"command": cmd, "output": output, "summary": summary, "saved_as": saved["filename"]}


@app.post("/api/run")
def api_run(req: RunRequest, user: str = Depends(require_operator)):
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
def api_bulk_run(req: BulkRunRequest, user: str = Depends(require_operator)):
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
def api_save_result(req: SaveResultRequest, user: str = Depends(require_operator)):
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
def api_create_schedule(req: ScheduleCreateRequest, user: str = Depends(require_operator)):
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
def api_update_schedule(schedule_id: str, req: ScheduleUpdateRequest, user: str = Depends(require_operator)):
    if SCHEDULES.get(schedule_id) is None:
        raise HTTPException(status_code=404, detail="unknown schedule")
    if req.interval_minutes is not None and req.interval_minutes < 5:
        raise HTTPException(status_code=400, detail="interval_minutes must be at least 5")
    return SCHEDULES.update(schedule_id, enabled=req.enabled, interval_minutes=req.interval_minutes)


@app.delete("/api/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: str, user: str = Depends(require_operator)):
    if not SCHEDULES.delete(schedule_id):
        raise HTTPException(status_code=404, detail="unknown schedule")
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/run")
def api_run_schedule_now(schedule_id: str, user: str = Depends(require_operator)):
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
def api_update_compliance_config(req: ComplianceConfigRequest, user: str = Depends(require_admin)):
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
def api_delete_result(filename: str, user: str = Depends(require_operator)):
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


@app.get("/api/devices/{device_id}/alarm-history")
def api_alarm_history(
    device_id: str,
    limit: int = 300,
    since_seconds: int = 604800,
    user: str = Depends(require_auth_and_db),
):
    """Historical hardware alarms for this device, read from Loki.

    Alarm classification is done via `hardware_alerting._classify_alarm`
    (shared with the live syslog-based fan/PSU alerting in that module -
    see its docstring for why this stayed a second implementation rather
    than trusting Vector, not a third one when live alerting was added),
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
        classified = hardware_alerting._classify_alarm(detail)
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
def api_ack_occurrence(occurrence_id: int, req: NoteRequest, user: str = Depends(require_operator)):
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
def api_unack_occurrence(occurrence_id: int, user: str = Depends(require_operator)):
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
    user: str = Depends(require_admin),
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
def api_get_alarm(occurrence_id: int, request: Request, user: str = Depends(require_auth_and_db)):
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
        "current_role": request.session.get("role"),
    }


@app.get("/api/alarms/{occurrence_id}/comments")
def api_list_comments(occurrence_id: int, user: str = Depends(require_auth_and_db)):
    _require_occurrence(occurrence_id)
    return OCCURRENCES.comments_for(occurrence_id)


@app.post("/api/alarms/{occurrence_id}/comments")
def api_add_comment(occurrence_id: int, req: CommentRequest, user: str = Depends(require_operator)):
    occurrence = _require_occurrence(occurrence_id)
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="comment cannot be empty")
    result = OCCURRENCES.add_comment(occurrence_id, user, body)
    AUDIT.record(user, "alert.comment", occurrence["alertname"], {"note": body},
                 occurrence["signature"], occurrence_id)
    return result


@app.delete("/api/alarms/{occurrence_id}/comments/{comment_id}")
def api_delete_comment(occurrence_id: int, comment_id: int, request: Request, user: str = Depends(require_auth_and_db)):
    """Authors can delete their own comments (a typo in a conversation is
    worth fixing); nobody else can delete them except an admin (RBAC
    bypass on this one existing rule), and the deletion itself is audited,
    so the record of what happened survives the message."""
    occurrence = _require_occurrence(occurrence_id)
    comment = OCCURRENCES.get_comment(occurrence_id, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="unknown comment")
    if comment["author"] != user and request.session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="you can only delete your own comments")
    OCCURRENCES.delete_comment(comment_id)
    AUDIT.record(user, "alert.comment_deleted", occurrence["alertname"], {"note": comment["body"]},
                 occurrence["signature"], occurrence_id)
    return {"ok": True}


class DelayRequest(BaseModel):
    seconds: int = 300


@app.post("/api/alarms/{occurrence_id}/page-now")
def api_page_now(occurrence_id: int, user: str = Depends(require_operator)):
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
def api_delay_page(occurrence_id: int, req: DelayRequest, user: str = Depends(require_operator)):
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
def api_narg(occurrence_id: int, req: NoteRequest, user: str = Depends(require_operator)):
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
def api_enable_paging(occurrence_id: int, user: str = Depends(require_operator)):
    """Undoes NARG - lifts the hold and lets the alarm page again."""
    occurrence = _require_occurrence(occurrence_id)
    PAGER.release(occurrence["silence_id"])
    updated = OCCURRENCES.set_paging_disabled(occurrence_id, False, None)
    OCCURRENCES.mark_paged(occurrence_id)
    AUDIT.record(user, "alert.paging_enabled", occurrence["alertname"], None,
                 occurrence["signature"], occurrence_id)
    return updated


@app.post("/api/alarms/{occurrence_id}/resolve")
def api_resolve_alarm(occurrence_id: int, user: str = Depends(require_operator)):
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
    elif labels.get("alertname") == hardware_alerting.ALERTNAME:
        HARDWARE_ALERT_CHECKER.forget(
            labels.get("device_id"), labels.get("kind"), labels.get("unit"), labels.get("bay")
        )

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
def api_create_silence(req: SilenceCreateRequest, user: str = Depends(require_operator)):
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
def api_delete_silence(silence_id: str, user: str = Depends(require_operator)):
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


@app.get("/api/command-history")
def api_command_history(
    request: Request,
    device_id: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    all_users: bool = False,
    user: str = Depends(require_auth_and_db),
):
    """This user's own command history by default. `all_users=true` is
    admin-only - history is personal, and letting any authenticated user
    read the whole fleet's would turn a convenience feature into an
    accidental disclosure of what everyone else has been doing. Admins can
    already see this via the audit log, so this isn't a new capability for
    them, just a more useful shape of it."""
    actor = user
    if all_users:
        if request.session.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin role required to view all users' history")
        actor = None
    items, total = COMMAND_HISTORY.list(
        actor=actor, device_id=device_id, status=status, q=q, limit=limit, offset=offset
    )
    return {"items": items, "total": total}


@app.get("/api/command-history/recent")
def api_command_history_recent(limit: int = 10, user: str = Depends(require_auth_and_db)):
    """Distinct recent commands for the Console's quick re-run list."""
    return COMMAND_HISTORY.recent_commands(user, limit=limit)


@app.delete("/api/command-history")
def api_clear_command_history(user: str = Depends(require_auth_and_db)):
    """Clears only the caller's own history. The audit_log entry for each
    run is untouched and admin-visible - this deliberately can't be used
    to erase the record of what someone ran."""
    COMMAND_HISTORY.clear(user)
    AUDIT.record(user, "command.history_cleared")
    return {"ok": True}


class FavoriteRequest(BaseModel):
    category_id: str
    command_id: str
    device_id: Optional[str] = None
    params: Optional[dict] = None
    label: Optional[str] = None


@app.get("/api/favorites")
def api_list_favorites(user: str = Depends(require_auth_and_db)):
    return FAVORITES.list(user)


@app.post("/api/favorites")
def api_add_favorite(req: FavoriteRequest, user: str = Depends(require_auth_and_db)):
    """Validates the command actually exists before pinning it - a
    favourite pointing at a command that was never in the tree would fail
    confusingly at run time instead of here. Device is optional (an
    "any device" favourite), so validation uses the named device's tree
    when given and any platform's when not."""
    if req.device_id:
        device = DEVICES_BY_ID.get(req.device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="unknown device")
        try:
            _resolve_command(device, req.category_id, req.command_id, req.params or {})
        except CommandLookupError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))
    elif not command_exists(req.category_id, req.command_id):
        raise HTTPException(status_code=404, detail="unknown command")

    fav = FAVORITES.add(
        user, req.category_id, req.command_id,
        device_id=req.device_id, params=req.params, label=req.label,
    )
    return fav


@app.delete("/api/favorites/{favorite_id}")
def api_delete_favorite(favorite_id: int, user: str = Depends(require_auth_and_db)):
    FAVORITES.delete(user, favorite_id)
    return {"ok": True}



# sFlow reports interfaces as SNMP ifIndex integers. Dell OS9 encodes them
# arithmetically (verified against the switch), but Junos's are irregular
# and have to be read off the device, so they're discovered here and
# cached in Postgres. Refreshed rarely - port-to-ifIndex mappings only
# change when hardware or config does - and a failure just leaves the
# previous map in place, or falls back to a raw ifIndex.
def _refresh_sflow_ifindex():
    if DB is None or SFLOW_IFINDEX is None:
        return
    # One command per device, parsed per platform. OS9 physical ports are
    # arithmetic, but its port-channels, management and VLAN interfaces sit
    # in unrelated ranges (Te 1/1 = 2097156, Po 1 = 1258291712,
    # Ma 1/1 = 9437185), so it is discovered too rather than half-covered.
    discovery = {
        "junos": ('show interfaces | match "Physical interface|SNMP ifIndex"',
                  junos_parsers.parse_junos_snmp_ifindex),
        "os9": ("show interfaces", parsers.parse_os9_ifindex),
    }
    for device in list(DEVICES_BY_ID.values()):
        plan = discovery.get(device.platform)
        if plan is None:
            continue  # no parser for this platform yet
        command, parse = plan
        try:
            with _session_locks[device.id]:
                switch = _get_session(device)
                out = switch.run(command)
            mapping = parse(out)
            if mapping:
                SFLOW_IFINDEX.save(device.id, mapping)
                log.info("sflow ifindex map refreshed for %s: %d entries", device.id, len(mapping))
            else:
                # Deliberately not saved: an empty parse is a failed read,
                # and wiping a good map loses every port name at once.
                log.warning("sflow ifindex refresh for %s parsed nothing - keeping previous map", device.id)
        except Exception:
            log.warning("could not refresh sflow ifindex map for %s", device.id, exc_info=True)


def _sflow_ifindex_loop():
    while True:
        if DB is None:
            time.sleep(60)
            continue
        try:
            _refresh_sflow_ifindex()
        except Exception:
            log.exception("sflow ifindex refresh failed")
        time.sleep(6 * 3600)


threading.Thread(target=_sflow_ifindex_loop, daemon=True, name="sflow-ifindex").start()


# --- sFlow (ROADMAP: traffic visibility) -----------------------------
# Read-only: rows are written by sfacctd on the collector LXC, never by
# this app (see sflow/README.md for the split and why).

def _sflow_platform_for(agent_ip):
    """ifIndex encoding is vendor-specific, so the decode needs to know
    which platform sent the flow.

    Returns None for an agent we can't identify, and that default matters:
    it used to fall back to "os9", which meant applying Dell's ifIndex
    arithmetic to flows from an unknown vendor and risking a real-looking
    but wrong port name - the exact failure the decode is careful to avoid
    elsewhere. An unidentified agent gets no vendor decode at all.

    Matching is deliberately not just `host`: an sFlow agent-id is often a
    loopback or router-id rather than the management address. The real
    EX3300 here reports agent 192.168.5.10 while being registered at
    192.168.4.1, so a host-only match silently found nothing."""
    if not agent_ip:
        return None
    for d in DEVICES_BY_ID.values():
        if agent_ip in _sflow_addresses_for(d):
            return d.platform
    return None


def _sflow_addresses_for(device):
    """Every address a device might legitimately use as its sFlow agent-id.
    Currently its management host plus any explicitly recorded agent IPs;
    kept as one place so adding more sources later doesn't scatter."""
    addrs = {device.host}
    extra = getattr(device, "sflow_agent_ip", None)
    if extra:
        addrs.add(extra)
    return addrs


def _sflow_cached_map_for(agent_ip):
    """The discovered ifIndex map for whichever device owns this agent IP."""
    for d in DEVICES_BY_ID.values():
        if agent_ip in _sflow_addresses_for(d):
            return _SFLOW_IFINDEX_CACHE.get(d.id)
    return None


_SFLOW_IFINDEX_CACHE = {}


def _sflow_device_id_for(agent_ip):
    for d in DEVICES_BY_ID.values():
        if agent_ip in _sflow_addresses_for(d):
            return d.id
    return None


def _sflow_agent_label(agent_ip):
    """Device name for an agent IP, or None if it isn't one we know."""
    for d in DEVICES_BY_ID.values():
        if agent_ip in _sflow_addresses_for(d):
            return d.name
    return None


# The widest absolute range the sFlow views will run. Not a retention
# limit - nothing is deleted, and all history is kept on purpose - but a
# query bound: these are GROUP BYs over every row in the span, and at
# ~340k rows/day a year's range is a hundred million rows and a page that
# appears to hang. A clamped request still succeeds and reports the window
# it actually used, so the UI can say so rather than quietly showing
# something narrower than was asked for.
SFLOW_MAX_SPAN_DAYS = int(os.environ.get("SFLOW_MAX_SPAN_DAYS", "92"))


def _sflow_ifaces_matching(q):
    """ifIndexes whose decoded port name matches `q`.

    Searching for "Te 1/37" has to work, and only this side of the app
    knows that 2101764 is called that: the name comes from a per-vendor
    decode over a map discovered by SSH, none of which exists in the
    database. So the name is resolved to numbers here and the numbers go
    into the query.
    """
    q = (q or "").strip().lower()
    if not q:
        return []
    found = set()
    for mapping in _SFLOW_IFINDEX_CACHE.values():
        for ifindex, port in (mapping or {}).items():
            if q in str(port).lower():
                found.add(int(ifindex))
    return sorted(found)


def _annotate_hostnames(payload):
    """Attach reverse-DNS names to the addresses in a flow payload.

    Done here rather than per-view so one batch of lookups covers the
    whole page - the same address usually appears in several panels, and
    resolving it once per panel would multiply the work by four for no
    extra information.
    """
    ips = set()
    for row in payload.get("top_talkers", []):
        ips.update((row.get("ip_src"), row.get("ip_dst")))
    for row in payload.get("top_hosts", []):
        ips.add(row.get("host"))
    names = DNS.reverse_many(ips)
    if not names:
        return payload
    for row in payload.get("top_talkers", []):
        row["ip_src_host"] = names.get(row.get("ip_src"))
        row["ip_dst_host"] = names.get(row.get("ip_dst"))
    for row in payload.get("top_hosts", []):
        row["host_name"] = names.get(row.get("host"))
    return payload


def _name_flow_ends(rows):
    """Reverse-DNS both endpoints of a list of raw flow rows."""
    ips = {r.get("ip_src") for r in rows} | {r.get("ip_dst") for r in rows}
    names = DNS.reverse_many(ips)
    for r in rows:
        r["ip_src_host"] = names.get(r.get("ip_src"))
        r["ip_dst_host"] = names.get(r.get("ip_dst"))
    return rows


def _flow_store(source):
    """The store for one vantage point, or a 400 for anything else.

    Never a default: which vantage point a number came from changes what
    it means, and silently picking one would let a caller read firewall
    figures believing they were switch figures.
    """
    if source not in sflow_store.FLOW_TABLES:
        raise HTTPException(400, f"unknown source: {source!r} (expected one of "
                                 f"{', '.join(sorted(sflow_store.FLOW_TABLES))})")
    store = SFLOW if source == "switches" else NETFLOW
    if store is None:
        raise HTTPException(503, "flow store not configured")
    return store


def _sflow_window(store, minutes, start, end):
    """Resolves the time range for one request, once, for every view.

    Returns (start, end, clamped). Absolute bounds win over `minutes`
    when both are given, since an explicit range is the more specific
    request.
    """
    def _parse(v):
        if not v:
            return None
        try:
            # The browser sends a trailing Z, which fromisoformat only
            # accepts from 3.11 - normalise rather than reject.
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"not a valid ISO 8601 timestamp: {v}")
        # A naive timestamp is ambiguous and guessing wrong shifts the
        # whole window silently. UTC is what the UI sends.
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    start_dt, end_dt = _parse(start), _parse(end)
    if start_dt and end_dt and start_dt >= end_dt:
        raise HTTPException(400, "start must be before end")

    minutes = max(1, min(int(minutes), 10080))
    start_dt, end_dt = store.resolve_window(since_minutes=minutes, start=start_dt, end=end_dt)

    clamped = False
    widest = timedelta(days=SFLOW_MAX_SPAN_DAYS)
    if end_dt - start_dt > widest:
        # Keep the end and move the start: someone asking for a very wide
        # range almost always wants the recent end of it.
        start_dt, clamped = end_dt - widest, True
    return start_dt, end_dt, clamped


@app.get("/api/sflow/overview")
def api_sflow_overview(
    minutes: int = 60,
    agent: Optional[str] = None,
    limit: int = 20,
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    source: str = "switches",
    user: str = Depends(require_auth_and_db),
):
    """Everything the sFlow page needs in one round trip - every view over
    the same time window, which is cheaper and more consistent than
    separate requests that could each land in a different window.

    `minutes` is the relative form, `start`/`end` (ISO 8601) an absolute
    range. Both resolve to one concrete pair before any query runs, and
    that pair comes back in the response so the page can state the span
    it is actually showing.
    """
    store = _flow_store(source)
    start_dt, end_dt, clamped = _sflow_window(store, minutes, start, end)
    win = {"start": start_dt, "end": end_dt}
    limit = max(1, min(int(limit), 200))
    # One query for every device's map, rather than per row.
    _SFLOW_IFINDEX_CACHE.clear()
    _SFLOW_IFINDEX_CACHE.update(SFLOW_IFINDEX.load_all())
    # The search runs in SQL, before ranking - see _match_clause. Applied
    # to the ranked rows instead it could only ever find what was already
    # in the top `limit`, which is how a host sitting 86th of 152 became
    # unfindable by typing its own address.
    q = (q or "").strip()[:100] or None
    # A hostname search resolves to addresses and matches those. Without
    # this, typing a name a table is already *showing* returns nothing,
    # because the name is annotation - only the address is in the table.
    q_hosts = DNS.forward(q) if (q and dns_cache.looks_like_hostname(q)) else []
    find = {"q": q, "q_ifaces": _sflow_ifaces_matching(q), "q_hosts": q_hosts}
    payload = {
        "available": store.available(),
        "source": source,
        # Flows whose byte counter hit the exporter's 32-bit field. Real
        # traffic, under-reported - the packet count on these stays
        # honest while the byte count stops at 4 GiB, which is why they
        # are surfaced rather than dropped: they are ~0.01% of rows but
        # ~40% of bytes, so hiding them would quietly delete most of the
        # volume they represent. Only NetFlow can hit this; sFlow's
        # counters are renormalized estimates, not exporter counters.
        "capped_rows": store.capped_rows(**win, agent_ip=agent, q=q,
                                         q_ifaces=find["q_ifaces"],
                                         q_hosts=find["q_hosts"]) if source == "firewall" else 0,
        # The window actually queried, not the one requested - they differ
        # when a span is clamped, and a page that cannot tell the two
        # apart will label a chart with a range it is not showing.
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "minutes": round((end_dt - start_dt).total_seconds() / 60),
        "clamped_to_days": SFLOW_MAX_SPAN_DAYS if clamped else None,
        "q": q,
        # So the page can say "matched the 3 addresses this name resolves
        # to" rather than appearing to search text it never searched.
        "q_resolved_to": q_hosts or None,
        # Built from the data, not the device registry: an agent whose
        # agent-id differs from its management IP would otherwise be
        # unselectable in the UI - which is exactly what happened with the
        # EX3300 reporting 192.168.5.10.
        "agents": [
            {**a,
             "device_name": _sflow_agent_label(a["peer_ip_src"]),
             "platform": _sflow_platform_for(a["peer_ip_src"])}
            for a in store.agents(**win)
        ],
        "top_talkers": store.top_talkers(agent_ip=agent, limit=limit, **win, **find),
        "top_hosts": store.top_hosts(agent_ip=agent, limit=limit, **win, **find),
        "protocol_mix": store.protocol_mix(agent_ip=agent, limit=limit, **win, **find),
        "per_port": store.per_port(agent_ip=agent, platform_for=_sflow_platform_for,
                                   cached_for=_sflow_cached_map_for, limit=limit, **win, **find),
        "totals": store.totals(agent_ip=agent, **win, **find),
        "timeseries": store.timeseries(agent_ip=agent, **win, **find),
    }
    return _annotate_hostnames(payload)


@app.get("/api/sflow/port/{iface}")
def api_sflow_port(
    iface: int,
    minutes: int = 60,
    agent: Optional[str] = None,
    limit: int = 20,
    start: Optional[str] = None,
    end: Optional[str] = None,
    source: str = "switches",
    user: str = Depends(require_auth_and_db),
):
    """Drill-down: what is actually crossing one interface."""
    store = _flow_store(source)
    start_dt, end_dt, _ = _sflow_window(store, minutes, start, end)
    return {
        "iface": iface,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "port": sflow_store.ifindex_to_port(
            iface, _sflow_platform_for(agent),
            cached=(SFLOW_IFINDEX.load(_sflow_device_id_for(agent)) if _sflow_device_id_for(agent) else None)),
        "flows": _name_flow_ends(store.port_detail(
            iface, agent_ip=agent, start=start_dt, end=end_dt,
            limit=max(1, min(int(limit), 200)))),
    }



@app.get("/api/sflow/host/{host}")
def api_sflow_host(
    host: str,
    minutes: int = 60,
    agent: Optional[str] = None,
    limit: int = 30,
    start: Optional[str] = None,
    end: Optional[str] = None,
    source: str = "switches",
    user: str = Depends(require_auth_and_db),
):
    """Everything involving one address, both directions - "what is this
    machine actually doing", which no aggregate view can answer."""
    store = _flow_store(source)
    start_dt, end_dt, _ = _sflow_window(store, minutes, start, end)
    return {
        "host": host,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "host_name": (DNS.reverse_many([host]) or {}).get(host),
        "flows": _name_flow_ends(store.host_detail(
            host, agent_ip=agent, start=start_dt, end=end_dt,
            limit=max(1, min(int(limit), 200)))),
    }


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
def api_update_alert_rule(name: str, req: AlertRuleUpdateRequest, user: str = Depends(require_admin)):
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
def api_update_interface_alert(device_id: str, req: InterfaceAlertUpdateRequest, user: str = Depends(require_admin)):
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
def api_save_topology_baseline(user: str = Depends(require_admin)):
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
def api_accept_topology_drift(req: TopologyBaselineAcceptRequest, user: str = Depends(require_admin)):
    """Manually folds specific drift into the baseline (e.g. "yes, that
    link was intentionally moved") without discarding the rest of the
    baseline the way a full relearn would."""
    TOPOLOGY_STORE.accept(req.added, req.removed, saved_by=user)
    log.info("user=%s accepted topology drift (+%d/-%d)", user, len(req.added), len(req.removed))
    return {"ok": True}


@app.delete("/api/topology/baseline")
def api_clear_topology_baseline(user: str = Depends(require_admin)):
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
def index():
    # Always unauthenticated - the SPA itself calls /api/auth/me on load
    # and redirects to /api/auth/login on a 401 (see api.js). Gating index.html
    # itself behind a session would be a chicken-and-egg problem: the
    # redirect-to-Keycloak logic lives in the JS this route serves.
    return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
