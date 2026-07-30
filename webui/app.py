import json
import logging
import os
import re
import secrets
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

from commands import COMMAND_TREE, find_command
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
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "switchboard.db"))
LEGACY_STORE_PATH = os.environ.get("DEVICE_STORE_FILE", str(BASE_DIR / "data" / "devices_store.json"))
LOKI_URL = os.environ.get("LOKI_URL", "http://192.168.0.145:3100")

WEBUI_USER = os.environ.get("WEBUI_USER")
WEBUI_PASS = os.environ.get("WEBUI_PASS")
if not WEBUI_USER or not WEBUI_PASS:
    raise RuntimeError("WEBUI_USER and WEBUI_PASS must be set - this tool runs commands against production network gear")

security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    user_ok = secrets.compare_digest(credentials.username, WEBUI_USER)
    pass_ok = secrets.compare_digest(credentials.password, WEBUI_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


app = FastAPI(title="Switchboard")
app.add_middleware(GZipMiddleware, minimum_size=500)

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
DB = Database(DB_PATH)
STORE = DeviceStore(DB)


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
    log.info("migrated %d device(s) from legacy %s into %s", len(legacy), LEGACY_STORE_PATH, DB_PATH)


_migrate_legacy_json_devices()

DEVICES = load_devices(DEVICES_PATH, STORE)
RESULTS = ResultsStore(DB)
LOKI = LokiClient(LOKI_URL)
DEVICES_BY_ID = {d.id: d for d in DEVICES}

# One persistent SSH session per device, reused across requests, rather than
# a fresh login per click. Dell OS9 only has a handful of concurrent vty
# (SSH) slots - opening/closing a new session per command reliably starved
# it under real use (confirmed live: most connection attempts failed with
# "Error reading SSH protocol banner" once the exporter's own persistent
# session plus a couple of clicks were in flight). A lock per device
# serializes command execution on that device's single shared session.
_sessions: dict[str, SwitchSSH] = {}
_session_locks: dict[str, threading.Lock] = {d.id: threading.Lock() for d in DEVICES}
_registry_lock = threading.Lock()


def _make_switch(device):
    return SwitchSSH(
        device.host,
        device.username,
        device.password,
        enable_password=device.enable_password,
        private_key=device.private_key,
        passphrase=device.passphrase,
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
# container or Prometheus being reachable.
STATUS = StatusPoller(get_session=_get_session, lock_for=lambda device_id: _session_locks[device_id])
for _d in DEVICES:
    STATUS.start(_d)


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


def _validate_device_request(req):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if not req.host.strip():
        raise HTTPException(status_code=400, detail="host is required")
    if not req.username.strip():
        raise HTTPException(status_code=400, detail="username is required")
    if req.auth_method == "password":
        if not req.password:
            raise HTTPException(status_code=400, detail="password is required for password auth")
    elif req.auth_method == "ssh_key":
        if not req.private_key:
            raise HTTPException(status_code=400, detail="private_key is required for SSH key auth")
    else:
        raise HTTPException(status_code=400, detail="auth_method must be 'password' or 'ssh_key'")


@app.get("/api/devices")
def api_devices(user: str = Depends(require_auth)):
    return [d.to_public_dict() for d in DEVICES]


@app.post("/api/devices")
def api_create_device(req: DeviceCreateRequest, user: str = Depends(require_auth)):
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
        }
        STORE.add(record)
        device = StoredDevice(record)
        DEVICES.append(device)
        DEVICES_BY_ID[device.id] = device
        _session_locks[device.id] = threading.Lock()
    STATUS.start(device)
    log.info("user=%s added device %s (%s)", user, device.id, device.host)
    return device.to_public_dict()


@app.delete("/api/devices/{device_id}")
def api_delete_device(device_id: str, user: str = Depends(require_auth)):
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
def api_device_status(device_id: str, interfaces: bool = False, user: str = Depends(require_auth)):
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    status = STATUS.get(device_id, include_interfaces=interfaces)
    if status is None:
        raise HTTPException(status_code=404, detail="status not yet available")
    return status


@app.post("/api/devices/{device_id}/status/refresh")
def api_device_status_refresh(device_id: str, user: str = Depends(require_auth)):
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
def api_test_device(req: DeviceCreateRequest, user: str = Depends(require_auth)):
    """Try connecting with the given draft device details, without saving
    anything. Best-effort: a failure here doesn't block Save, since the
    enable-mode handshake this checks is Dell OS9-specific and a device
    running something else may legitimately fail it while still being
    fine to store for later."""
    _validate_device_request(req)
    switch = SwitchSSH(
        req.host.strip(),
        req.username.strip(),
        req.password,
        enable_password=req.enable_password or req.password,
        private_key=req.private_key,
        passphrase=req.passphrase,
        timeout=8,
    )
    try:
        switch.connect(retries=1)
        switch.close()
    except SwitchSSHError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "message": "Connected and reached privileged EXEC mode."}


@app.get("/api/devices/{device_id}/values/{param_name}")
def api_device_param_values(device_id: str, param_name: str, user: str = Depends(require_auth)):
    device = DEVICES_BY_ID.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    return {"values": device.valid_values_for(param_name)}


@app.get("/api/commands")
def api_commands(user: str = Depends(require_auth)):
    return COMMAND_TREE


@app.post("/api/run")
def api_run(req: RunRequest, user: str = Depends(require_auth)):
    device = DEVICES_BY_ID.get(req.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")

    spec = find_command(req.category_id, req.command_id)
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

    summary = summarize(req.category_id, req.command_id, output)

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
def api_save_result(req: SaveResultRequest, user: str = Depends(require_auth)):
    device = DEVICES_BY_ID.get(req.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    result = RESULTS.save(
        device.id, device.name, device.host, req.category_id, req.command_id, req.command, req.summary, req.output
    )
    log.info("user=%s saved result %s", user, result["filename"])
    return result


@app.get("/api/results")
def api_list_results(device_id: Optional[str] = None, user: str = Depends(require_auth)):
    return RESULTS.list(device_id=device_id)


@app.get("/api/results/{filename}")
def api_get_result(filename: str, user: str = Depends(require_auth)):
    content = RESULTS.read(filename)
    if content is None:
        raise HTTPException(status_code=404, detail="unknown result")
    return {"filename": filename, "content": content}


@app.delete("/api/results/{filename}")
def api_delete_result(filename: str, user: str = Depends(require_auth)):
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
    user: str = Depends(require_auth),
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
    user: str = Depends(require_auth),
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
def index(user: str = Depends(require_auth)):
    return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
