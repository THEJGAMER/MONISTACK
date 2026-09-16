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
import audit
import auth
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
import command_history
import compliance
import retention
import api_tokens
import dns_cache
import event_catalog
import event_detect
import event_reconcile
import events
import eventstore
import fastpath
import insights as insights_module
import push as push_module
import syslog_alerting
import sflow_store
import webhooks as webhooks_module
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

# How quiet the syslog pipeline may go before the Settings health panel
# calls it stale. Generous by default: a small fleet can genuinely be
# silent for a while, and this should flag "the pipeline is dead", not
# "the switches had nothing to say for ten minutes".
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
EXPORTER_URL = os.environ.get("EXPORTER_URL", "http://s4048-exporter:9101")
SYSLOG_STALE_AFTER_SECONDS = int(os.environ.get("SYSLOG_STALE_AFTER_SECONDS", "1800"))
# Same idea for sFlow. Shorter than syslog's window because sFlow is
# continuous by nature - a switch with any traffic at all samples
# constantly, so silence means the pipeline is broken rather than "nothing
# happened to be said".
SFLOW_STALE_AFTER_SECONDS = int(os.environ.get("SFLOW_STALE_AFTER_SECONDS", "600"))

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


# A per-process JWKS cache for validating Keycloak-issued bearer tokens.
# Fetched from the issuer's metadata on first use and refreshed hourly (or
# once immediately on a signature failure - the normal way a key rotation
# shows up). Never touched by the session-cookie path, which needs no
# network at all.
_JWKS = {"keyset": None, "fetched_at": 0.0}
_JWKS_TTL = 3600


def _jwks():
    now = time.monotonic()
    if _JWKS["keyset"] is not None and now - _JWKS["fetched_at"] < _JWKS_TTL:
        return _JWKS["keyset"]
    import httpx
    from joserfc.jwk import KeySet
    meta = httpx.get(f"{OIDC_ISSUER_URL.rstrip('/')}/.well-known/openid-configuration", timeout=10).json()
    resp = httpx.get(meta["jwks_uri"], timeout=10)
    resp.raise_for_status()
    _JWKS["keyset"] = KeySet.import_key_set(resp.json())
    _JWKS["fetched_at"] = now
    return _JWKS["keyset"]


def _identity_from_keycloak_jwt(token):
    """(username, role) from a Keycloak access token, or None if it is not
    one of ours. The same checks an ID token gets at login - signature
    against the realm's keys, issuer, expiry - and the role comes from the
    same client-role claim, so a script authenticates exactly as a person
    does, with exactly the access Keycloak says it has."""
    if not OIDC_ISSUER_URL:
        return None
    from joserfc import jwt as jose_jwt
    from joserfc.errors import JoseError
    try:
        try:
            claims = jose_jwt.decode(token, _jwks()).claims
        except JoseError:
            _JWKS["fetched_at"] = 0.0
            claims = jose_jwt.decode(token, _jwks()).claims
    except Exception as e:
        log.info("bearer token rejected as a Keycloak JWT: %s", e)
        return None
    if str(claims.get("iss", "")).rstrip("/") != OIDC_ISSUER_URL.rstrip("/"):
        return None
    exp = claims.get("exp")
    if not exp or exp < time.time():
        return None
    username = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    role = auth.role_from_claims(claims, OIDC_CLIENT_ID)
    if not username or role is None:
        return None
    return username, role


def _identity_from_bearer(request):
    """(username, role) for an Authorization: Bearer header, or None when
    there is no such header.

    Two token shapes: our own `sb_...` API tokens (a hash lookup, no
    network) and Keycloak access tokens (a JWT validated against the
    realm's keys). Tried in that order because the first is cheap and
    unambiguous - an API token can never parse as a JWT. A header that is
    present but accepted by neither is a 401, not a fall-through to the
    cookie: the caller said how it wants to be identified."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None
    if api_tokens.looks_like_token(token):
        if API_TOKENS is None:
            raise HTTPException(status_code=503, detail="API tokens unavailable: database not configured")
        row = API_TOKENS.verify(token)
        if row is None:
            raise HTTPException(status_code=401, detail="API token is invalid, expired or revoked")
        request.state.auth_via = f"api-token:{row['name']}"
        return f"token:{row['name']}", row["role"]
    ident = _identity_from_keycloak_jwt(token)
    if ident is None:
        raise HTTPException(status_code=401, detail="Bearer token was not accepted")
    request.state.auth_via = "keycloak-jwt"
    return ident


def require_auth(request: Request):
    bearer = _identity_from_bearer(request)
    if bearer is not None:
        request.state.auth_user, request.state.auth_role = bearer
        return bearer[0]
    session = request.session
    if not session.get("username") or not session.get("role") or _session_expired(session):
        raise HTTPException(status_code=401, detail="Not logged in")
    if session.get("sid") and _is_sid_revoked(session["sid"]):
        raise HTTPException(status_code=401, detail="Session was ended")
    request.state.auth_user, request.state.auth_role = session["username"], session["role"]
    return session["username"]


def _role_of(request):
    """The role of whoever this request is - from the bearer token when
    there was one, else the session. Role checks must read this, not the
    session directly, or a token-authenticated request is judged by a
    cookie it did not present."""
    return getattr(request.state, "auth_role", None) or request.session.get("role")


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
        role = _role_of(request)
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
        role = _role_of(request)
        if not auth.role_meets(role, min_role):
            raise HTTPException(status_code=403, detail=f"requires {min_role} role, you have {role}")
        return user
    return _dep


require_admin_no_db = require_role_no_db("admin")


API_VERSION = "1.0"
API_DESCRIPTION = """
Switchboard's HTTP API. Everything the web UI does goes through these
endpoints, and they are the same endpoints scripts and integrations use.

**Authentication.** Three ways, all yielding the same identity and role:

- the browser session cookie (what the UI uses);
- `Authorization: Bearer sb_...` with an API token created on the
  Settings page (or `POST /api/tokens`). Tokens carry a role of their own
  and never exceed their creator's;
- `Authorization: Bearer <Keycloak access token>` - a JWT issued by the
  same realm the UI logs in against. The token's `resource_access`
  client roles decide the role, exactly as at interactive login.

**Roles.** `viewer` reads; `operator` runs commands and works alarms;
`admin` changes configuration. Each endpoint states the floor it needs.

**Versioning.** This is v1. Paths are stable; new fields may be added to
responses at any time and clients should ignore fields they do not know.
Breaking changes will arrive under a new prefix, not silently here.

**Webhooks.** Register a URL under `/api/webhooks` and Switchboard POSTs
events to it, signed with `X-Switchboard-Signature: sha256=<hmac>` over
the raw body. `GET /api/events` lists the event names and what they mean.
"""

app = FastAPI(
    title="Switchboard API",
    version=API_VERSION,
    description=API_DESCRIPTION,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
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
COMMAND_HISTORY = None
FAVORITES = None
DNS = dns_cache.DnsCache()
API_TOKENS = None
WEBHOOKS = None
WEBHOOK_DISPATCHER = None
PUSH_SUBS = None
PUSH_NOTIFIER = None
PUSH_KEYS = None

SFLOW = None
NETFLOW = None
SFLOW_IFINDEX = None
AUDIT = None

# The syslog fast path (fastpath.py): Vector POSTs events here as they
# arrive; the token is what lets it. Blank = endpoint answers 503 and
# detection stays on the Loki poll and SSH fallbacks.
SYSLOG_INGEST_TOKEN = (os.environ.get("SYSLOG_INGEST_TOKEN") or "").strip()
SYSLOG_RECEIVER = (os.environ.get("SYSLOG_RECEIVER") or "").strip()
FAST_PATH = fastpath.FastPathStats()
# Event-driven monitoring (see event_catalog.py): built once the database
# is up. The syslog detector and the SSH reconciler both write into one
# event store; nothing sits between a signal and an event but this process.
SYSLOG_RULES = None      # syslog_alerting.SyslogRuleStore
EVENTS = None            # eventstore.EventStore
EVENT_SETTINGS = None    # event_catalog.EventSettings
PORT_SETTINGS = None     # event_catalog.PortSettings
SYSLOG_DETECTOR = None   # event_detect.SyslogDetector
SSH_RECONCILER = None    # event_reconcile.SshReconciler
_LIST_CACHE = {}
_STARTED_AT = datetime.now(timezone.utc)
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


def _device_name_for(device_id):
    device = DEVICES_BY_ID.get(device_id)
    return device.name if device else device_id


# Fast path for "immediate" mode - a 30s-bounded SSH poll cycle isn't
# what a human means by "alert me immediately" (confirmed live: the
# switch's own syslog reports a link-down transition within ~1-2s, real
# users noticed the ~10-30s gap between that and this alerting). Vector
# already ships that same event to Loki in real time (syslog/vector.yaml),
# so this polls Loki - a single cheap HTTP query, not an SSH round trip -
# on a much tighter interval instead of waiting on the device poll cycle.
class PollBackoff:
    """Sleep schedule for a poll loop that talks to something which can be
    overloaded.

    The 3-second cadence is right when Loki is healthy - a fan or PSU
    fault should page within seconds. It is exactly wrong when Loki is
    behind: two loops on two instances kept firing every 3 seconds into a
    full queue, each timing out and retrying, so a brief overflow became a
    sustained one (2026-09-15, 112 errors/min with one Console tab open).
    Doubling the interval on each consecutive failure, capped, and
    snapping back on the first success gives the queue room to drain
    without giving up the fast path when nothing is wrong.
    """

    def __init__(self, base=3.0, cap=60.0):
        self.base, self.cap, self.failures = base, cap, 0

    def ok(self):
        self.failures = 0

    def failed(self):
        self.failures += 1

    @property
    def delay(self):
        return min(self.cap, self.base * (2 ** self.failures))




# --- the syslog fast path: evaluate on arrival ---------------------------
# Everything below runs on Vector's POST (see api_ingest_syslog): the same
# checkers the Loki polls above feed, given the event the moment it lands
# instead of up to three seconds later, plus the syslog rules. Each checker
# keeps a timestamp cursor, so the poll behind this sees what the fast
# path already handled as done.



# --- event-driven monitoring ------------------------------------------------
# Syslog first: Vector POSTs each parsed line to /api/ingest/syslog the
# moment it arrives (fastpath.py) and event_detect turns it into an event
# transition. The Loki poll behind it engages only while the fast path is
# silent. The SSH poll (status_poller) is the fallback for what syslog
# never said, reconciled every cycle by event_reconcile. Timers close what
# nothing else can. Everything lands in one event store; the store's hooks
# put every transition on the bus for push and webhooks.

def _cached_list(key, ttl, fn):
    """A per-line DB read for the rule list would be one query per syslog
    line under load; a few seconds of staleness for a rule edit is nothing."""
    now = time.monotonic()
    hit = _LIST_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _LIST_CACHE[key] = (now, value)
    return value


def _device_for_event(event):
    """(device_id or "", display name) for a syslog line - by source
    address first (that is what the devices table records), then by the
    hostname the device wrote into the line, else the hostname itself so a
    sender we do not manage still raises under its own name."""
    host = event.get("source_ip") or event.get("device_host") or event.get("host") or ""
    for d in DEVICES_BY_ID.values():
        if d.host == host:
            return d.id, d.name
    name = str(event.get("device_host") or event.get("host") or host or "unknown")
    for d in DEVICES_BY_ID.values():
        if d.host == name or d.name.lower() == name.lower():
            return d.id, d.name
    return "", name


def _handle_syslog_events(events, source="syslog"):
    """Run the detectors over freshly arrived lines. Returns transitions."""
    if not events or SYSLOG_DETECTOR is None:
        return 0
    rules = _cached_list("syslog_rules", 5, lambda: SYSLOG_RULES.list(enabled_only=True) if SYSLOG_RULES is not None else [])
    ordered = sorted(events, key=lambda e: int(e.get("_timestamp_ns") or 0))
    return SYSLOG_DETECTOR.process(ordered, _device_for_event, rules=rules, source=source)


def _event_timers_loop():
    """Every 10s: expire timer-resolved kinds and rule events; raise or
    resolve "syslog silent" per device from what the fast path has seen."""
    while True:
        time.sleep(10)
        if EVENTS is None:
            continue
        try:
            for entry in event_catalog.CATALOG:
                if entry.get("ttl_seconds"):
                    EVENTS.expire(entry["kind"], entry["ttl_seconds"])
            if SYSLOG_RULES is not None:
                syslog_alerting.expire_rules(EVENTS, _cached_list("syslog_rules", 5, lambda: SYSLOG_RULES.list(enabled_only=True)))
        except Exception:
            log.exception("event timer pass failed")
        try:
            _check_syslog_silence()
        except Exception:
            log.exception("syslog-silence check failed")


def _check_syslog_silence():
    """A device whose syslog has gone quiet for longer than the threshold
    raises device.syslog_silent; the next line from it resolves. Only
    meaningful once the fast path has heard *something* since start -
    before that, silence is ours, not the device's."""
    if not SYSLOG_INGEST_TOKEN or FAST_PATH.total == 0 or EVENT_SETTINGS is None:
        return
    minutes = int(EVENT_SETTINGS.params_for("device.syslog_silent").get("minutes", 30))
    threshold = minutes * 60
    now = datetime.now(timezone.utc)
    if (now - _STARTED_AT).total_seconds() < threshold:
        return
    for d in list(DEVICES):
        last = FAST_PATH.last_by_host.get(d.host) or FAST_PATH.last_by_host.get(d.name)
        subject = "syslog"
        if last is None or (now - last).total_seconds() > threshold:
            sev = EVENT_SETTINGS.severity_for("device.syslog_silent")
            if sev == "ignore":
                continue
            EVENTS.raise_event("device.syslog_silent", sev, d.id, d.name, subject,
                               f"No syslog from {d.name} for {minutes} min",
                               detail=("never since start" if last is None else f"last line {last.isoformat()}"), source="switchboard")
        else:
            open_ev = EVENTS.open_kind("device.syslog_silent", d.id, subject)
            if open_ev:
                EVENTS.resolve(open_ev["signature"], by="syslog", detail="a line arrived")


threading.Thread(target=_event_timers_loop, daemon=True, name="event-timers").start()


def _ssh_reconcile_loop():
    """Every 15s, reconcile each device's cached SSH poll with the open
    events (event_reconcile.py) - never an extra SSH round trip."""
    while True:
        time.sleep(15)
        if SSH_RECONCILER is None:
            continue
        for d in list(DEVICES):
            try:
                SSH_RECONCILER.reconcile(d.id, d.name, STATUS.get(d.id, include_interfaces=True))
            except Exception:
                log.exception("ssh reconcile failed for %s", d.id)


threading.Thread(target=_ssh_reconcile_loop, daemon=True, name="ssh-reconcile").start()


def _syslog_fallback_loop():
    """The Loki poll behind the fast path, engaged only while the fast
    path has been silent for 30s (or was never configured). While lines
    are flowing this costs Loki nothing; when they are not, events still
    happen, a few seconds late. The detector's cursor makes the two paths
    one stream."""
    backoff = PollBackoff(base=5.0)
    while True:
        time.sleep(backoff.delay)
        if LOKI is None or SYSLOG_DETECTOR is None:
            continue
        last = FAST_PATH.last_received_at
        if last is not None and (datetime.now(timezone.utc) - last).total_seconds() < 30:
            backoff.ok()
            continue
        try:
            events_ = LOKI.query_range(filters=None, limit=200, since_seconds=20)
        except Exception:
            backoff.failed()
            continue
        backoff.ok()
        try:
            _handle_syslog_events(events_, source="loki")
        except Exception:
            log.exception("syslog fallback evaluation failed")


threading.Thread(target=_syslog_fallback_loop, daemon=True, name="syslog-fallback").start()


def _wire_event_hooks(store):
    """Every event transition goes onto the bus from the store itself -
    webhooks and push subscribe there. Exactly once per transition,
    whichever path (syslog, SSH, timer, a person) drove it."""
    store.on_raised = lambda ev: events.BUS.emit("event.raised", event_data=ev)
    store.on_resolved = lambda ev: events.BUS.emit("event.resolved", event_data=ev)


def _wire_event_bus():
    """Subscribe the webhook dispatcher and push notifier to the event bus.
    Runs on every (re)configuration; the bus is process-global, so the
    previous subscribers are removed first rather than accumulated."""
    global WEBHOOK_DISPATCHER, PUSH_NOTIFIER, PUSH_KEYS
    if WEBHOOK_DISPATCHER is not None:
        events.BUS.unsubscribe(WEBHOOK_DISPATCHER)
    if PUSH_NOTIFIER is not None:
        events.BUS.unsubscribe(PUSH_NOTIFIER)
    WEBHOOK_DISPATCHER = webhooks_module.WebhookDispatcher(WEBHOOKS)
    events.BUS.subscribe(WEBHOOK_DISPATCHER)
    if PUSH_KEYS is None:
        # VAPID's `sub` claim, derived the way PROXMON does it: the https
        # origin if any configured URL is https, else a mailto: on the
        # site's real hostname, else a placeholder on a real domain.
        # PUSH_VAPID_SUBJECT overrides. Never a bare IP or localhost -
        # Apple's push service rejects those outright.
        subject = push_module.default_vapid_subject(
            [os.environ.get("PUBLIC_URL"), OIDC_REDIRECT_URI],
            override=os.environ.get("PUSH_VAPID_SUBJECT"))
        PUSH_KEYS = push_module.VapidKeys(BASE_DIR / "data" / "push_vapid.json", subject)
    PUSH_NOTIFIER = push_module.PushNotifier(PUSH_SUBS, PUSH_KEYS)
    events.BUS.subscribe(PUSH_NOTIFIER)


def _load_database(dsn):
    """Connects to Postgres, runs one-time legacy migrations, and (re)loads
    devices + status polling from it. Raises on a bad DSN/unreachable host
    so callers (setup wizard, Settings save) can report a clear error
    without disturbing whatever was working before the attempt."""
    global DB, STORE, RESULTS, TOPOLOGY_STORE, SCHEDULES
    global AUDIT, DEVICES, DEVICES_BY_ID, COMMAND_HISTORY, FAVORITES, SFLOW, SFLOW_IFINDEX, NETFLOW, API_TOKENS, WEBHOOKS, PUSH_SUBS
    global SYSLOG_RULES, EVENTS, EVENT_SETTINGS, PORT_SETTINGS, SYSLOG_DETECTOR, SSH_RECONCILER
    new_db = Database(dsn)
    new_store = DeviceStore(new_db)
    new_results = ResultsStore(new_db)
    new_topology_store = TopologyStore(new_db)
    new_schedules = ScheduleStore(new_db)
    new_audit = audit.AuditLog(new_db)
    new_command_history = command_history.CommandHistoryStore(new_db)
    new_favorites = command_history.CommandFavoritesStore(new_db)
    new_sflow = sflow_store.SFlowStore(new_db, source="switches")
    new_netflow = sflow_store.SFlowStore(new_db, source="firewall")
    new_ifindex = sflow_store.IfIndexMap(new_db)
    DB, STORE, RESULTS, TOPOLOGY_STORE, SCHEDULES = new_db, new_store, new_results, new_topology_store, new_schedules
    AUDIT = new_audit
    COMMAND_HISTORY, FAVORITES = new_command_history, new_favorites
    SFLOW = new_sflow
    NETFLOW = new_netflow
    SFLOW_IFINDEX = new_ifindex
    API_TOKENS = api_tokens.ApiTokenStore(new_db)
    WEBHOOKS = webhooks_module.WebhookStore(new_db)
    PUSH_SUBS = push_module.PushSubscriptionStore(new_db)
    SYSLOG_RULES = syslog_alerting.SyslogRuleStore(new_db)
    EVENTS = eventstore.EventStore(new_db)
    EVENT_SETTINGS = event_catalog.EventSettings(new_db)
    PORT_SETTINGS = event_catalog.PortSettings(new_db)
    SYSLOG_DETECTOR = event_detect.SyslogDetector(EVENTS, EVENT_SETTINGS, PORT_SETTINGS)
    SSH_RECONCILER = event_reconcile.SshReconciler(EVENTS, EVENT_SETTINGS, PORT_SETTINGS)
    _LIST_CACHE.clear()
    try:
        SYSLOG_RULES.seed_defaults()
    except Exception:
        log.exception("syslog rules: seeding failed - rules still evaluate")
    _wire_event_hooks(EVENTS)
    try:
        _wire_event_bus()
    except Exception:
        # Found live: OIDC_REDIRECT_URI is None on a dev instance, and one
        # AttributeError here left the app with no devices and no push -
        # everything after this line in the configuration never ran.
        log.exception("event bus wiring failed - webhooks/push disabled, everything else continues")

    _migrate_legacy_json_devices()
    _migrate_legacy_sqlite()

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
    global DATABASE_URL, CONFIGURED, DB_ERROR
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
    global LOKI_URL, LOKI, PROMETHEUS_URL, EXPORTER_URL, SFLOW_COLLECTOR, SYSLOG_RECEIVER
    LOKI_URL = settings_dict.get("loki_url") or settings_store.DEFAULT_LOKI_URL
    LOKI = LokiClient(LOKI_URL)
    PROMETHEUS_URL = settings_dict.get("prometheus_url") or PROMETHEUS_URL
    EXPORTER_URL = settings_dict.get("exporter_url") or EXPORTER_URL
    # Blank is a legitimate value here ("not recorded"), so this one is
    # assigned as given rather than falling back to the previous value.
    SFLOW_COLLECTOR = settings_dict.get("sflow_collector", SFLOW_COLLECTOR) or ""
    SYSLOG_RECEIVER = settings_dict.get("syslog_receiver", SYSLOG_RECEIVER) or ""


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
    notes: str = ""
    runbook_url: str = ""


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
    prometheus_url: Optional[str] = None
    exporter_url: Optional[str] = None
    # Blank is meaningful: "collector address not recorded".
    sflow_collector: Optional[str] = None
    # Blank is meaningful: "no receiver recorded" (the fast-path self-test
    # then says so instead of sending into the void).
    syslog_receiver: Optional[str] = None


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
        "prometheus_url": PROMETHEUS_URL,
        "exporter_url": EXPORTER_URL,
        "sflow_collector": SFLOW_COLLECTOR,
        "syslog_receiver": SYSLOG_RECEIVER,
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

    # The fast path is "are events arriving straight from Vector", the
    # difference between paging in under a second and paging when the
    # Loki poll gets round to it. Configured-but-silent is the case to
    # name: the token is set here but Vector's sink is not pointed here,
    # or carries a different token, and everything still *works* - slowly.
    snap = FAST_PATH.snapshot()
    if not SYSLOG_INGEST_TOKEN:
        checks.append({"name": "Syslog fast path", "target": "/api/ingest/syslog", "ok": False,
                       "detail": "not configured - set SYSLOG_INGEST_TOKEN and point Vector's switchboard_fast "
                                 "sink here (install-stack.sh --install syslog); detection is on the Loki poll"})
    elif snap["last_received_at"] is None:
        checks.append({"name": "Syslog fast path", "target": "/api/ingest/syslog", "ok": False,
                       "detail": "configured, but no event has arrived since start - is Vector's switchboard_fast "
                                 "sink pointed here with the same token? Use 'Send a test' on the Alerts page"})
    else:
        age = (datetime.now(timezone.utc) - FAST_PATH.last_received_at).total_seconds()
        lat = f"; Vector to Switchboard median {snap['transport_ms_median']:.0f} ms" if snap["transport_ms_median"] is not None else ""
        checks.append({"name": "Syslog fast path", "target": "/api/ingest/syslog", "ok": age <= SYSLOG_STALE_AFTER_SECONDS,
                       "detail": (f"last event {age/60:.0f} min ago" if age > 90 else f"last event {age:.0f}s ago")
                                 + f" from {snap['last_host'] or '?'}; {snap['events_last_minute']}/min{lat}"})

    for name, url, path in (
        ("Loki", LOKI_URL, "/ready"),
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
        blank_ok = key in ("sflow_collector", "syslog_receiver")
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
# in-process state in this file - losing it on
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
            "notes": req.notes.strip(),
            "runbook_url": req.runbook_url.strip(),
            "port_channels": req.port_channels,
        }
        STORE.add(record)
        device = StoredDevice(record)
        events.BUS.emit("device.created", device_id=device.id, device=device.name, by=user)
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
            "notes": req.notes.strip(),
            "runbook_url": req.runbook_url.strip(),
            "port_channels": req.port_channels,
        }
        STORE.update(device_id, record)
        new_device = StoredDevice(record)
        events.BUS.emit("device.updated", device_id=device_id, device=new_device.name, by=user)
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
        events.BUS.emit("device.deleted", device_id=device_id, by=user)
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
        events.BUS.emit("command.ran", device_id=device.id, device=device.name, command=cmd, by=user)
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
        events = LOKI.query_range(filters=filters, limit=limit, since_seconds=_clamp_window(since_seconds))
    except LokiError as e:
        raise HTTPException(status_code=502, detail=f"Loki unreachable: {e}")

    if host_filter is not None:
        events = [e for e in events if e.get("source_ip") == host_filter or e.get("device_host") == host_filter]
    return events[:limit]


# Widest window a Loki-backed read may ask for. Not retention (Loki keeps
# everything, by decision - see loki/README.md) but a fan-out bound: at a
# 24h split a 30-day query is 30 pieces, which is fine; unbounded, a
# mistyped or hostile since_seconds turns into thousands.
LOKI_MAX_WINDOW_SECONDS = 30 * 24 * 3600


def _clamp_window(since_seconds):
    try:
        v = int(since_seconds)
    except (TypeError, ValueError):
        v = 3600
    return max(60, min(v, LOKI_MAX_WINDOW_SECONDS))


@app.get("/api/audit-log")
def api_get_audit_log(
    limit: int = 200,
    action_prefix: Optional[str] = None,
    fingerprint: Optional[str] = None,
    user: str = Depends(require_admin),
):
    return AUDIT.list(limit=limit, action_prefix=action_prefix, fingerprint=fingerprint)


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


def _gather_device_topology(device):
    """Every topology input for one device - LLDP, ARP, MAC table,
    port-channel membership - in one pass under that device's own lock.
    Returns (raw_lldp, lldp_error, arp_rows, mac_rows, pc_members); any of
    the optional ones is None when the platform has no such command or the
    fetch failed, and a failure of one does not cost the others."""
    lldp_raw = lldp_err = arp_rows = mac_rows = pc_members = None
    with _session_locks[device.id]:
        switch = _get_session(device)
        cmd = _LLDP_COMMAND.get(device.platform)
        if cmd:
            try:
                lldp_raw = switch.run(cmd)
            except SwitchSSHError as e:
                lldp_err = str(e)
            except Exception:
                log.exception("unexpected error fetching LLDP for topology from %s", device.id)
                lldp_err = "internal error"
        for name, table, parser in (("ARP", _ARP_COMMAND, _ARP_PARSER),
                                    ("MAC table", _MAC_TABLE_COMMAND, _MAC_TABLE_PARSER),
                                    ("port-channel membership", _PORT_CHANNEL_COMMAND, _PORT_CHANNEL_PARSER)):
            cmd = table.get(device.platform)
            if not cmd:
                continue
            try:
                parsed = parser[device.platform](switch.run(cmd))
            except Exception:
                log.warning("could not fetch %s from %s for topology", name, device.id, exc_info=True)
                continue
            if name == "ARP":
                arp_rows = parsed
            elif name == "MAC table":
                mac_rows = parsed
            else:
                pc_members = parsed
    return lldp_raw, lldp_err, arp_rows, mac_rows, pc_members


# The topology page used to crawl every device on every load and again
# every 30 seconds: four SSH commands per device, one device at a time,
# so three devices was twelve sequential round trips before anything
# rendered. Now one background thread crawls on its own cadence - devices
# concurrently, each device's four commands serialised under its own
# lock - and the page reads the cache. Topology changes on the scale of
# minutes; a diagram sixty seconds old is not a stale diagram, and a
# forced refresh is one click for the moment it is.
_TOPOLOGY_CACHE = {"result": None, "fetched_at": None, "error": None, "refreshing": False}
_TOPOLOGY_LOCK = threading.Lock()
TOPOLOGY_REFRESH_SECONDS = int(os.environ.get("TOPOLOGY_REFRESH_SECONDS", "60"))


def _fetch_live_topology():
    """Fetches live LLDP from every device, builds the graph, and overlays
    current link state + (where the platform has it) Mbps utilization from
    the already-running status poller - no extra SSH round trip for that
    part, just whatever's already cached. Per-device SSH failures don't
    fail the whole call - that device just shows up with no edges and
    `lldp_error` set, same partial-failure tolerance as the rest of this
    app's multi-device endpoints."""
    raw_by_device, errors_by_device = {}, {}
    arp_rows_by_device, mac_table_by_device, port_channel_members_by_device = {}, {}, {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(DEVICES) or 1)), thread_name_prefix="topology") as pool:
        for device, fut in [(d, pool.submit(_gather_device_topology, d)) for d in DEVICES]:
            try:
                lldp_raw, lldp_err, arp_rows, mac_rows, pc_members = fut.result()
            except Exception:
                log.exception("topology gather failed for %s", device.id)
                errors_by_device[device.id] = "internal error"
                continue
            if lldp_raw is not None:
                raw_by_device[device.id] = lldp_raw
            if lldp_err is not None:
                errors_by_device[device.id] = lldp_err
            if arp_rows is not None:
                arp_rows_by_device[device.id] = arp_rows
            if mac_rows is not None:
                mac_table_by_device[device.id] = mac_rows
            if pc_members is not None:
                port_channel_members_by_device[device.id] = pc_members
    mac_to_ip = topology.merge_mac_to_ip(arp_rows_by_device)
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


def _refresh_topology_cache():
    """One crawl into the cache. Safe to call from the loop and from a
    request; a crawl already in progress is not doubled up."""
    with _TOPOLOGY_LOCK:
        if _TOPOLOGY_CACHE["refreshing"]:
            return False
        if not DEVICES:
            # Nothing to crawl is not a result. Caching an empty topology
            # here (seen live: the first request landed before devices had
            # loaded) would serve "no links" for a full refresh interval.
            _TOPOLOGY_CACHE["error"] = "no devices loaded yet"
            return False
        _TOPOLOGY_CACHE["refreshing"] = True
    try:
        result = _fetch_live_topology()
        result["lag_health"] = _lag_health(result["edges"])
        with _TOPOLOGY_LOCK:
            _TOPOLOGY_CACHE.update(result=result, fetched_at=datetime.now(timezone.utc), error=None)
        return True
    except Exception as e:
        log.exception("topology refresh failed")
        with _TOPOLOGY_LOCK:
            _TOPOLOGY_CACHE["error"] = str(e)
        return False
    finally:
        with _TOPOLOGY_LOCK:
            _TOPOLOGY_CACHE["refreshing"] = False


def _topology_refresh_loop():
    while True:
        if DB is not None and DEVICES:
            _refresh_topology_cache()
        time.sleep(TOPOLOGY_REFRESH_SECONDS)


threading.Thread(target=_topology_refresh_loop, daemon=True, name="topology-refresh").start()


# --- the syslog fast path -----------------------------------------------
# Vector's switchboard_fast sink (syslog/vector.yaml) POSTs each parsed
# event here the moment it arrives. Not a session route: the sink
# authenticates with SYSLOG_INGEST_TOKEN. The work runs on a worker
# thread so a slow Alertmanager forward never stalls the event loop.


# --- the syslog fast path: Vector -> here ---------------------------------------

def _ingest_syslog_sync(body, content_type):
    try:
        lines = fastpath.parse_events(body, content_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"unreadable events: {e}")
    FAST_PATH.record(lines, datetime.now(timezone.utc))
    acted = _handle_syslog_events(lines)
    return {"accepted": len(lines), "acted": acted}


@app.post("/api/ingest/syslog", tags=["ingest"], summary="Syslog fast path: lines straight from Vector")
async def api_ingest_syslog(request: Request):
    """Receives interpreted syslog lines from Vector's `http` sink (a JSON
    array per batch; a single object or NDJSON also work) and evaluates
    them immediately. Authenticate with `Authorization: Bearer
    <SYSLOG_INGEST_TOKEN>`."""
    if not SYSLOG_INGEST_TOKEN:
        raise HTTPException(status_code=503, detail="fast path not configured: set SYSLOG_INGEST_TOKEN")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-switchboard-token", "")
    if not token or not secrets.compare_digest(token, SYSLOG_INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="bad ingest token")
    body = await request.body()
    if len(body) > 4_000_000:
        raise HTTPException(status_code=413, detail="batch too large")
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_ingest_syslog_sync, body, request.headers.get("content-type", ""))


# --- events ---------------------------------------------------------------------

def _require_events():
    if EVENTS is None:
        raise HTTPException(status_code=503, detail="database not configured")
    return EVENTS


@app.get("/api/events", tags=["events"], summary="Events, newest first")
def api_list_events(open_only: int = 0, severity: Optional[str] = None, device_id: Optional[str] = None,
                    kind: Optional[str] = None, group: Optional[str] = None, q: Optional[str] = None,
                    since_seconds: Optional[int] = None, before_id: Optional[int] = None, limit: int = 200,
                    user: str = Depends(require_auth_and_db)):
    """`severity` may be a comma list. `open_only=1` for what is live now.
    Page older history with `before_id` (the smallest id you have)."""
    store = _require_events()
    sev = [x.strip() for x in severity.split(",") if x.strip()] if severity else None
    since = (datetime.now(timezone.utc) - timedelta(seconds=int(since_seconds))).isoformat() if since_seconds else None
    return {"events": store.list(open_only=bool(open_only), severity=sev, device_id=device_id or None, kind=kind or None,
                                 group=group or None, q=q or None, since=since, before_id=before_id, limit=limit),
            "summary": store.summary()}


@app.get("/api/events/summary", tags=["events"], summary="Open and recent counts by severity")
def api_events_summary(user: str = Depends(require_auth_and_db)):
    return _require_events().summary()


# Insights are several aggregate passes over metric_samples (1.3M rows and
# growing), so they are computed on a timer and served from memory - the
# same shape as the topology cache, and for the same reason.
_INSIGHTS_CACHE = {"result": None, "at": None, "error": None}
_INSIGHTS_LOCK = threading.Lock()
INSIGHTS_REFRESH_SECONDS = int(os.environ.get("INSIGHTS_REFRESH_SECONDS", "300"))


def _compute_insights():
    if EVENTS is None or DB is None:
        return None
    return insights_module.Insights(
        DB, EVENTS, EVENT_SETTINGS, list(DEVICES),
        lambda device_id: STATUS.get(device_id, include_interfaces=True),
        syslog_seen=dict(FAST_PATH.last_by_host),
    ).run()


def _refresh_insights():
    with _INSIGHTS_LOCK:
        try:
            result = _compute_insights()
        except Exception as e:
            _INSIGHTS_CACHE["error"] = str(e)
            log.exception("insights refresh failed")
            return _INSIGHTS_CACHE["result"]
        if result is not None:
            _INSIGHTS_CACHE.update({"result": result, "at": datetime.now(timezone.utc), "error": None})
        return _INSIGHTS_CACHE["result"]


def _insights_loop():
    # Warm the cache once the database and the first SSH poll have had a
    # chance to land, rather than leaving the first visitor after a
    # restart to compute six weeks of samples themselves (measured: ~9s).
    time.sleep(45)
    while True:
        try:
            _refresh_insights()
        except Exception:
            log.exception("insights loop failed")
        time.sleep(INSIGHTS_REFRESH_SECONDS)


threading.Thread(target=_insights_loop, daemon=True, name="insights-refresh").start()


@app.get("/api/insights", tags=["events"], summary="What is worth knowing about the network right now")
def api_insights(refresh: int = 0, user: str = Depends(require_auth_and_db)):
    """Derived from the trend samples, the SSH poller's live state and the
    event history - nothing here talks to a device. Served from a cache
    refreshed every few minutes; `?refresh=1` recomputes."""
    cached = _INSIGHTS_CACHE["result"]
    if refresh or cached is None:
        cached = _refresh_insights()
    if cached is None:
        raise HTTPException(status_code=503, detail=_INSIGHTS_CACHE["error"] or "insights are not available yet")
    return {**cached, "cached_at": _INSIGHTS_CACHE["at"].isoformat() if _INSIGHTS_CACHE["at"] else None,
            "refresh_seconds": INSIGHTS_REFRESH_SECONDS, "error": _INSIGHTS_CACHE["error"]}


@app.get("/api/events/catalog", tags=["events"], summary="Every event kind and the site's severity for it")
def api_events_catalog(user: str = Depends(require_auth_and_db)):
    if EVENT_SETTINGS is None:
        raise HTTPException(status_code=503, detail="database not configured")
    descriptions = {
        "port": "What the switch says about its ports. Per-port overrides on the Ports tab.",
        "env": "Fans, power supplies and temperature - from syslog, and from show environment on the SSH poll.",
        "compute": "CPU, memory and memory errors - the SSH poll's CPU/memory readings, and what the device logs.",
        "device": "The device as a whole: reachability, restarts, configuration changes, silence.",
        "protocol": "Spanning tree and routing protocol events from syslog.",
        "syslog": "Your own rules (next tab) raise these; severity is set on each rule.",
        "switchboard": "Switchboard's own signals.",
    }
    return {"groups": [{"key": k, "name": n, "description": descriptions.get(k, "")} for k, n in event_catalog.GROUPS],
            "kinds": EVENT_SETTINGS.all()}


class EventKindUpdateRequest(BaseModel):
    severity: Optional[str] = None
    params: Optional[dict] = None


@app.put("/api/events/catalog/{kind}", tags=["events"], summary="Set a kind's severity or thresholds")
def api_update_event_kind(kind: str, req: EventKindUpdateRequest, user: str = Depends(require_operator)):
    if EVENT_SETTINGS is None:
        raise HTTPException(status_code=503, detail="database not configured")
    try:
        entry = EVENT_SETTINGS.set(kind, severity=req.severity, params=req.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    AUDIT.record(user, "event_kind.updated", kind, {"severity": entry["severity"], "params": entry["params"]})
    return entry


@app.delete("/api/events/catalog/{kind}", tags=["events"], summary="Back to the catalogue default")
def api_reset_event_kind(kind: str, user: str = Depends(require_operator)):
    if EVENT_SETTINGS is None:
        raise HTTPException(status_code=503, detail="database not configured")
    try:
        entry = EVENT_SETTINGS.reset(kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    AUDIT.record(user, "event_kind.reset", kind, None)
    return entry


@app.get("/api/events/ports/{device_id}", tags=["events"], summary="Per-port link-down severity")
def api_port_settings(device_id: str, user: str = Depends(require_auth_and_db)):
    if PORT_SETTINGS is None:
        raise HTTPException(status_code=503, detail="database not configured")
    if device_id not in DEVICES_BY_ID:
        raise HTTPException(status_code=404, detail="unknown device")
    overrides = PORT_SETTINGS.list(device_id)
    status = STATUS.get(device_id, include_interfaces=True) or {}
    ports = []
    seen = set()
    for iface in status.get("interfaces") or []:
        port = iface.get("port")
        if not port:
            continue
        seen.add(port)
        ports.append({"port": port, "port_state": iface.get("port_state"), "description": iface.get("description"),
                      "severity": overrides.get(port)})
    for port, sev in overrides.items():
        if port not in seen:
            ports.append({"port": port, "port_state": None, "description": None, "severity": sev})
    return {"device_id": device_id, "default_severity": EVENT_SETTINGS.severity_for("port.link_down"), "ports": ports}


class PortSeverityRequest(BaseModel):
    severity: Optional[str] = None


@app.put("/api/events/ports/{device_id}/{port:path}", tags=["events"], summary="Set one port's link-down severity")
def api_set_port_severity(device_id: str, port: str, req: PortSeverityRequest, user: str = Depends(require_operator)):
    if PORT_SETTINGS is None:
        raise HTTPException(status_code=503, detail="database not configured")
    if device_id not in DEVICES_BY_ID:
        raise HTTPException(status_code=404, detail="unknown device")
    try:
        sev = PORT_SETTINGS.set(device_id, port, req.severity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    AUDIT.record(user, "port_severity.updated", f"{device_id} {port}", {"severity": sev or "default"})
    return {"device_id": device_id, "port": port, "severity": sev}


@app.get("/api/events/fast-path", tags=["events"], summary="Syslog fast-path and SSH fallback status")
def api_fast_path_status(user: str = Depends(require_auth_and_db)):
    snap = FAST_PATH.snapshot()
    snap.update({
        "configured": bool(SYSLOG_INGEST_TOKEN),
        "receiver": SYSLOG_RECEIVER,
        "syslog_transitions": SYSLOG_DETECTOR.acted if SYSLOG_DETECTOR else 0,
        "ignored": SYSLOG_DETECTOR.ignored if SYSLOG_DETECTOR else 0,
        "ssh_transitions": SSH_RECONCILER.acted if SSH_RECONCILER else 0,
        "rule_events_open": EVENTS.open_events(kind_prefix="syslog.") if EVENTS else [],
        "stale_after_seconds": SYSLOG_STALE_AFTER_SECONDS,
    })
    return snap


class FastPathTestRequest(BaseModel):
    severity: str = "warning"


@app.post("/api/events/fast-path/test", tags=["events"], summary="Send a syslog self-test and time it")
def api_fast_path_test(req: FastPathTestRequest, user: str = Depends(require_operator)):
    """Sends one syslog line to the configured receiver (Vector) and times
    it back: received by the ingest endpoint, raised as an event, pushed
    to phones. The severity you pick becomes the self-test kind's setting.
    The event resolves itself after a minute."""
    if not SYSLOG_INGEST_TOKEN:
        raise HTTPException(status_code=400, detail="fast path not configured: set SYSLOG_INGEST_TOKEN in webui.env")
    if not SYSLOG_RECEIVER:
        raise HTTPException(status_code=400, detail="set the syslog receiver address (host:port) in Settings first")
    if req.severity not in event_catalog.SEVERITIES:
        raise HTTPException(status_code=400, detail=f"severity must be one of {', '.join(event_catalog.SEVERITIES)}")
    store = _require_events()
    host, _, port = SYSLOG_RECEIVER.rpartition(":")
    if not host:
        host, port = SYSLOG_RECEIVER, "514"
    try:
        port = int(port)
    except ValueError:
        raise HTTPException(status_code=400, detail="syslog receiver must be host:port")
    EVENT_SETTINGS.set("switchboard.selftest", severity=req.severity)
    signature = eventstore.signature_for("switchboard.selftest", "switchboard", "selftest")
    store.resolve(signature, by="switchboard", detail="superseded by a new test")   # a lingering one would look instant

    import socket
    nonce = secrets.token_hex(4)
    line = fastpath.selftest_line(nonce, sender="switchboard")
    sent_at = datetime.now(timezone.utc)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.sendto(line.encode(), (host, port))
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"could not send to {host}:{port}: {e}")

    def ms_since(dt):
        return round((dt - sent_at).total_seconds() * 1000) if dt else None

    def _dt(v):
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    received_at = ev = None
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        received_at = received_at or FAST_PATH.selftest_received_at(nonce)
        if received_at:
            cand = store.open_for(signature)
            if cand and (cand.get("labels") or {}).get("nonce") == nonce:
                ev = cand
                break
        time.sleep(0.02)
    pushed, first_push = 0, None
    if ev and PUSH_SUBS is not None:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            fresh = [_dt(r.get("last_used_at")) for r in PUSH_SUBS.list()]
            fresh = [t for t in fresh if t and t >= sent_at]
            if fresh:
                pushed, first_push = len(fresh), min(fresh)
                if len(fresh) == len(PUSH_SUBS.list()):
                    break
            time.sleep(0.1)
    result = {
        "ok": ev is not None, "sent_at": sent_at.isoformat(), "receiver": f"{host}:{port}",
        "received_ms": ms_since(received_at), "event_ms": ms_since(_dt(ev["raised_at"])) if ev else None,
        "push_ms": ms_since(first_push), "pushed_devices": pushed, "event_id": ev["id"] if ev else None,
        "severity": req.severity, "by": user,
    }
    if not ev:
        result["detail"] = ("the line never arrived on /api/ingest/syslog - check Vector's switchboard_fast sink and token"
                            if received_at is None else "received, but no event was raised - is the self-test kind set to ignore?")
    FAST_PATH.last_test = result
    AUDIT.record(user, "events.fast_path_test", "Switchboard fast-path self-test", result)
    log.info("user=%s fast-path self-test: %s", user, result)
    return result


class ResolveRequest(BaseModel):
    note: Optional[str] = None


@app.get("/api/events/{event_id}", tags=["events"], summary="One event")
def api_get_event(event_id: int, user: str = Depends(require_auth_and_db)):
    ev = _require_events().get(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="no such event")
    return ev


@app.post("/api/events/{event_id}/resolve", tags=["events"], summary="Resolve an event by hand")
def api_resolve_event(event_id: int, req: ResolveRequest, user: str = Depends(require_operator)):
    """A correction, not an action: if the condition is still true the
    device raises it again as a new event."""
    store = _require_events()
    ev = store.resolve_id(event_id, by=user, detail=(req.note or "").strip() or "resolved by hand")
    if ev is None:
        current = store.get(event_id)
        if current is None:
            raise HTTPException(status_code=404, detail="no such event")
        return current
    AUDIT.record(user, "event.resolved", ev["title"], {"note": req.note} if req.note else None)
    log.info("user=%s resolved event %s (%s)", user, event_id, ev["title"])
    return ev


@app.get("/api/devices/{device_id}/events", tags=["events"], summary="A device's recent events")
def api_device_events(device_id: str, limit: int = 50, user: str = Depends(require_auth_and_db)):
    if device_id not in DEVICES_BY_ID:
        raise HTTPException(status_code=404, detail="unknown device")
    return _require_events().list(device_id=device_id, limit=limit)


# --- syslog rules ----------------------------------------------------------

class SyslogRuleRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    severity: Optional[str] = None
    facility: Optional[str] = None
    mnemonic: Optional[str] = None
    pattern: Optional[str] = None
    clear_pattern: Optional[str] = None
    per_interface: Optional[bool] = None
    auto_resolve_seconds: Optional[int] = None


class SyslogRuleMatchRequest(BaseModel):
    message: str
    facility: Optional[str] = None
    mnemonic: Optional[str] = None


def _require_syslog_rules():
    if SYSLOG_RULES is None:
        raise HTTPException(status_code=503, detail="database not configured")
    return SYSLOG_RULES


@app.get("/api/syslog-rules", tags=["events"], summary="List syslog rules")
def api_list_syslog_rules(user: str = Depends(require_auth_and_db)):
    return {"rules": _require_syslog_rules().list(), "active": EVENTS.open_events(kind_prefix="syslog.") if EVENTS else []}


@app.post("/api/syslog-rules", tags=["events"], summary="Create a syslog rule")
def api_create_syslog_rule(req: SyslogRuleRequest, user: str = Depends(require_operator)):
    try:
        rule = _require_syslog_rules().create(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _LIST_CACHE.pop("syslog_rules", None)
    AUDIT.record(user, "syslog_rule.created", rule["name"], {k: rule[k] for k in ("severity", "facility", "mnemonic", "pattern")})
    return rule


@app.put("/api/syslog-rules/{rule_id}", tags=["events"], summary="Update a syslog rule")
def api_update_syslog_rule(rule_id: int, req: SyslogRuleRequest, user: str = Depends(require_operator)):
    try:
        rule = _require_syslog_rules().update(rule_id, req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if rule is None:
        raise HTTPException(status_code=404, detail="no such rule")
    _LIST_CACHE.pop("syslog_rules", None)
    if not rule["enabled"] and EVENTS is not None:
        EVENTS.resolve_open(rule_id=rule_id, by="switchboard", detail="rule disabled")
    AUDIT.record(user, "syslog_rule.updated", rule["name"], {k: v for k, v in req.model_dump().items() if v is not None})
    return rule


@app.delete("/api/syslog-rules/{rule_id}", tags=["events"], summary="Delete a syslog rule")
def api_delete_syslog_rule(rule_id: int, user: str = Depends(require_operator)):
    store = _require_syslog_rules()
    rule = store.get(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="no such rule")
    store.delete(rule_id)
    _LIST_CACHE.pop("syslog_rules", None)
    if EVENTS is not None:
        EVENTS.resolve_open(rule_id=rule_id, by="switchboard", detail="rule deleted")
    AUDIT.record(user, "syslog_rule.deleted", rule["name"], None)
    return {"ok": True}


_DELL_SHAPE = re.compile(r"%(?P<facility>[A-Z0-9]+)-(?P<severity_num>\d)-(?P<mnemonic>[A-Z0-9_-]+):")


@app.post("/api/syslog-rules/match", tags=["events"], summary="Which rules would a line fire or clear?")
def api_match_syslog_rules(req: SyslogRuleMatchRequest, user: str = Depends(require_auth_and_db)):
    """Dry run for the rule editor: paste a real log line and see what it
    would do. A Dell-shaped line has its facility/mnemonic parsed the way
    Vector's interpreter would; otherwise pass them explicitly."""
    event = {"message": req.message, "detail": req.message,
             "facility": (req.facility or "").upper(), "mnemonic": (req.mnemonic or "").upper()}
    m = _DELL_SHAPE.search(req.message)
    if m and not req.facility:
        event["facility"], event["mnemonic"] = m.group("facility"), m.group("mnemonic")
    out = []
    for rule in _require_syslog_rules().list():
        verdict = syslog_alerting.matches(rule, event)
        if verdict is not None:
            out.append({"id": rule["id"], "name": rule["name"], "enabled": rule["enabled"],
                        "verdict": "fires" if verdict else "clears"})
    return {"parsed": {"facility": event["facility"], "mnemonic": event["mnemonic"]}, "matches": out}


@app.get("/api/topology", tags=["topology"], summary="Fleet topology (cached)")
def api_topology(refresh: int = 0, user: str = Depends(require_auth_and_db)):
    """Fleet-wide topology from LLDP, ARP and MAC-table data.

    Served from a cache that a background thread refreshes every
    TOPOLOGY_REFRESH_SECONDS (default 60). `?refresh=1` forces a live crawl
    first - a few seconds of SSH round trips - for the moment a link was
    just moved and sixty seconds is too long to wait. The response carries
    `fetched_at` and `age_seconds` so the page can say how old it is."""
    if refresh or _TOPOLOGY_CACHE["result"] is None:
        _refresh_topology_cache()
    with _TOPOLOGY_LOCK:
        cached, fetched_at, error = _TOPOLOGY_CACHE["result"], _TOPOLOGY_CACHE["fetched_at"], _TOPOLOGY_CACHE["error"]
        refreshing = _TOPOLOGY_CACHE["refreshing"]
    if cached is None:
        raise HTTPException(status_code=503, detail=f"topology not available yet: {error or 'first crawl still running'}")
    result = dict(cached)
    result["fetched_at"] = fetched_at.isoformat() if fetched_at else None
    result["age_seconds"] = round((datetime.now(timezone.utc) - fetched_at).total_seconds()) if fetched_at else None
    result["refreshing"] = refreshing
    result["refresh_seconds"] = TOPOLOGY_REFRESH_SECONDS
    result["last_error"] = error

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


# ------------------------------------------------------------ API tokens

class ApiTokenCreateRequest(BaseModel):
    name: str
    role: str = "viewer"
    expires_in_days: Optional[int] = None


@app.get("/api/tokens", tags=["auth"], summary="List API tokens")
def api_list_tokens(user: str = Depends(require_admin)):
    return API_TOKENS.list()


@app.post("/api/tokens", tags=["auth"], summary="Create an API token", status_code=201)
def api_create_token(req: ApiTokenCreateRequest, request: Request, user: str = Depends(require_admin)):
    """The clear-text token is in this response and nowhere else. A token
    never carries more than the role of the account creating it."""
    expires_at = None
    if req.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(req.expires_in_days))
    try:
        row, token = API_TOKENS.create(req.name, req.role, user, _role_of(request), expires_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    AUDIT.record(user, "token.create", req.name, {"role": req.role})
    return {**row, "token": token}


@app.delete("/api/tokens/{token_id}", tags=["auth"], summary="Revoke an API token")
def api_revoke_token(token_id: int, user: str = Depends(require_admin)):
    if not API_TOKENS.revoke(token_id):
        raise HTTPException(status_code=404, detail="no such active token")
    AUDIT.record(user, "token.revoke", str(token_id), None)
    return {"ok": True}


# ------------------------------------------------------------ events + webhooks

@app.get("/api/webhooks/events", tags=["webhooks"], summary="Bus event names a webhook can subscribe to")
def api_list_events(user: str = Depends(require_auth)):
    return [{"name": k, "description": v} for k, v in events.EVENTS.items()]


class WebhookRequest(BaseModel):
    name: str
    url: str
    events: list = ["*"]
    enabled: bool = True


@app.get("/api/webhooks", tags=["webhooks"], summary="List webhooks")
def api_list_webhooks(user: str = Depends(require_admin)):
    return WEBHOOKS.list()


@app.post("/api/webhooks", tags=["webhooks"], summary="Create a webhook", status_code=201)
def api_create_webhook(req: WebhookRequest, user: str = Depends(require_admin)):
    """The signing secret is in this response and nowhere else."""
    try:
        row = WEBHOOKS.create(req.name, req.url, req.events, user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not req.enabled:
        WEBHOOKS.update(row["id"], enabled=False)
        row["enabled"] = False
    AUDIT.record(user, "webhook.create", req.name, {"url": req.url})
    return row


@app.put("/api/webhooks/{webhook_id}", tags=["webhooks"], summary="Update a webhook")
def api_update_webhook(webhook_id: int, req: WebhookRequest, user: str = Depends(require_admin)):
    unknown = [e for e in req.events if e != "*" and e not in events.EVENTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown event(s): {', '.join(unknown)}")
    row = WEBHOOKS.update(webhook_id, name=req.name, url=req.url, events=req.events, enabled=req.enabled)
    if row is None:
        raise HTTPException(status_code=404, detail="no such webhook")
    AUDIT.record(user, "webhook.update", req.name, {"url": req.url, "enabled": req.enabled})
    return row


@app.delete("/api/webhooks/{webhook_id}", tags=["webhooks"], summary="Delete a webhook")
def api_delete_webhook(webhook_id: int, user: str = Depends(require_admin)):
    if not WEBHOOKS.delete(webhook_id):
        raise HTTPException(status_code=404, detail="no such webhook")
    AUDIT.record(user, "webhook.delete", str(webhook_id), None)
    return {"ok": True}


@app.post("/api/webhooks/{webhook_id}/test", tags=["webhooks"], summary="Send a test delivery")
def api_test_webhook(webhook_id: int, user: str = Depends(require_admin)):
    result = WEBHOOK_DISPATCHER.test(webhook_id)
    if result is None:
        raise HTTPException(status_code=404, detail="no such webhook")
    return result


# ------------------------------------------------------------ web push (the in-house pager)

class PushSubscribeRequest(BaseModel):
    subscription: dict
    min_severity: str = "warning"
    notify_resolved: bool = True
    label: Optional[str] = None


class PushPrefsRequest(BaseModel):
    endpoint: str
    min_severity: str = "warning"
    notify_resolved: bool = True


class PushEndpointRequest(BaseModel):
    endpoint: str


@app.get("/api/push/config", tags=["push"], summary="Push availability + this user's devices")
def api_push_config(user: str = Depends(require_auth_and_db)):
    enabled = bool(PUSH_KEYS and PUSH_KEYS.available)
    return {"enabled": enabled, "public_key": PUSH_KEYS.public_key if enabled else None,
            "subscriptions": PUSH_SUBS.list(username=user)}


@app.post("/api/push/subscribe", tags=["push"], summary="Register this browser for paging")
def api_push_subscribe(req: PushSubscribeRequest, user: str = Depends(require_auth_and_db)):
    if not (PUSH_KEYS and PUSH_KEYS.available):
        raise HTTPException(status_code=503, detail="push is not available on this server")
    try:
        row = PUSH_SUBS.upsert(req.subscription, user, req.label, req.min_severity, req.notify_resolved,
                               req.repeat_minutes, req.max_repeats)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    AUDIT.record(user, "push.subscribe", (req.label or "")[:60], {"min_severity": req.min_severity})
    return row


@app.delete("/api/push/subscribe", tags=["push"], summary="Unregister a browser")
def api_push_unsubscribe(req: PushEndpointRequest, request: Request, user: str = Depends(require_auth_and_db)):
    mine = {r["endpoint"] for r in PUSH_SUBS.list(username=user)}
    if req.endpoint not in mine and not auth.role_meets(_role_of(request), "admin"):
        raise HTTPException(status_code=403, detail="not your subscription")
    PUSH_SUBS.remove(req.endpoint)
    return {"ok": True}


@app.post("/api/push/prefs", tags=["push"], summary="Change how a device is paged")
def api_push_prefs(req: PushPrefsRequest, request: Request, user: str = Depends(require_auth_and_db)):
    mine = {r["endpoint"] for r in PUSH_SUBS.list(username=user)}
    if req.endpoint not in mine and not auth.role_meets(_role_of(request), "admin"):
        raise HTTPException(status_code=403, detail="not your subscription")
    try:
        row = PUSH_SUBS.update_prefs(req.endpoint, req.min_severity, req.notify_resolved)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="no such subscription")
    return row


@app.get("/api/push/subscriptions", tags=["push"], summary="Every subscribed browser (admin)")
def api_push_subscriptions(user: str = Depends(require_admin)):
    return PUSH_SUBS.list()


@app.post("/api/push/test", tags=["push"], summary="Send a test page to one browser")
def api_push_test(req: PushEndpointRequest, request: Request, user: str = Depends(require_auth_and_db)):
    mine = {r["endpoint"] for r in PUSH_SUBS.list(username=user)}
    if req.endpoint not in mine and not auth.role_meets(_role_of(request), "admin"):
        raise HTTPException(status_code=403, detail="not your subscription")
    result = PUSH_NOTIFIER.test(req.endpoint)
    if result is None:
        raise HTTPException(status_code=404, detail="no such subscription")
    return result


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


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    path = FRONTEND_DIST / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404)
    # Service-Worker-Allowed lets a worker served here claim "/" scope;
    # no-cache so a new build takes effect on the next visit, not a day later.
    return FileResponse(str(path), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    path = FRONTEND_DIST / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(path), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/icons/{name}", include_in_schema=False)
def pwa_icon(name: str):
    if "/" in name or ".." in name or not name.endswith(".png"):
        raise HTTPException(status_code=404)
    path = FRONTEND_DIST / "icons" / name
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(path), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", include_in_schema=False)
def index():
    # Always unauthenticated - the SPA itself calls /api/auth/me on load
    # and redirects to /api/auth/login on a 401 (see api.js). Gating index.html
    # itself behind a session would be a chicken-and-egg problem: the
    # redirect-to-Keycloak logic lives in the JS this route serves.
    return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
