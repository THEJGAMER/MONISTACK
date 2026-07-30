import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import settings as settings_store
from commands import COMMAND_TREES, find_command
from db import Database
from devices import DeviceConfigError, StoredDevice, load_devices
from loki_client import LokiClient, LokiError
from results_store import ResultsStore
from ssh_client import SwitchSSH, SwitchSSHError
from status_poller import StatusPoller
from store import DeviceStore
from summarize import summarize

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("webui")

BASE_DIR = Path(__file__).parent
DEVICES_PATH = os.environ.get("DEVICES_FILE", str(BASE_DIR / "devices.yaml"))
LEGACY_STORE_PATH = os.environ.get("DEVICE_STORE_FILE", str(BASE_DIR / "data" / "devices_store.json"))
LEGACY_SQLITE_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "switchboard.db"))

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

DB = None
STORE = None
RESULTS = None
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

    if not RESULTS.list(limit=1):
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
STATUS = StatusPoller(get_session=_get_session, lock_for=lambda device_id: _session_locks[device_id])


def _load_database(dsn):
    """Connects to Postgres, runs one-time legacy migrations, and (re)loads
    devices + status polling from it. Raises on a bad DSN/unreachable host
    so callers (setup wizard, Settings save) can report a clear error
    without disturbing whatever was working before the attempt."""
    global DB, STORE, RESULTS, DEVICES, DEVICES_BY_ID
    new_db = Database(dsn)
    new_store = DeviceStore(new_db)
    new_results = ResultsStore(new_db)
    DB, STORE, RESULTS = new_db, new_store, new_results

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


@app.post("/api/run")
def api_run(req: RunRequest, user: str = Depends(require_auth_and_db)):
    device = DEVICES_BY_ID.get(req.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")

    spec = find_command(req.category_id, req.command_id, device.platform)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown command")

    cmd = spec["cmd"]
    if "param" in spec:
        param_name = spec["param"]
        value = (req.params or {}).get(param_name)
        if value not in device.valid_values_for(param_name):
            raise HTTPException(status_code=400, detail=f"invalid or missing {param_name!r}")
        cmd = cmd.format(**{param_name: value})

    log.info("user=%s device=%s running: %s", user, device.id, cmd)

    try:
        with _session_locks[device.id]:
            switch = _get_session(device)
            output = switch.run(cmd)
    except DeviceConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except SwitchSSHError as e:
        raise HTTPException(status_code=502, detail=str(e))

    summary = summarize(device.platform, req.category_id, req.command_id, output)

    # Every run is auto-saved - no separate "Save result" click needed.
    # (Kept the manual POST below too, for scripted/API use.)
    saved = RESULTS.save(
        device.id, device.name, device.host, req.category_id, req.command_id, cmd, summary, output, auto_saved=True
    )

    return {
        "device": device.id,
        "command": cmd,
        "output": output,
        "summary": summary,
        "saved_as": saved["filename"],
    }


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
def api_list_results(device_id: Optional[str] = None, user: str = Depends(require_auth_and_db)):
    return RESULTS.list(device_id=device_id)


@app.get("/api/results/{filename}")
def api_get_result(filename: str, user: str = Depends(require_auth_and_db)):
    content = RESULTS.read(filename)
    if content is None:
        raise HTTPException(status_code=404, detail="unknown result")
    return {"filename": filename, "content": content}


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


app.mount("/static/assets", ImmutableCachedStaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


@app.get("/")
def index(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    # Unauthenticated (and un-cached) until setup is complete, so the SPA
    # can boot and show the setup wizard - once CONFIGURED, this behaves
    # exactly like the old always-authenticated index route.
    if CONFIGURED:
        _check_auth(credentials)
    return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
