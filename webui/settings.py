"""Local, GUI-editable deployment settings (Postgres DSN, Loki URL) - stored
as a small JSON file on the `webui-data` volume rather than in Postgres
itself, since the Postgres connection string is one of the things being
configured here (can't store your DB address in the DB).

The env var (`DATABASE_URL`/`LOKI_URL`) still works as a first-boot seed -
handy for docker-compose deployments that want to keep configuring things
the old way - but once a settings file exists it wins, and the Settings
page in the UI is the source of truth from then on.

Login identity is no longer part of this file - it's handled by OIDC
against an external Keycloak instance (see auth.py), not stored here.
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", str(BASE_DIR / "data" / "settings.json")))

DEFAULT_LOKI_URL = "http://192.168.0.145:3100"

# Every service the webui talks to, so the Settings page can show and edit
# them in one place instead of them being env-only and invisible in the UI.
# Each is (key, env var, fallback) - the env var still seeds the very first
# boot, exactly like database_url/loki_url, and the saved file wins after
# that (see the module docstring for why).
SERVICE_SETTINGS = (
    ("loki_url", "LOKI_URL", DEFAULT_LOKI_URL),
    ("alertmanager_url", "ALERTMANAGER_URL", "http://alertmanager:9093"),
    ("prometheus_url", "PROMETHEUS_URL", "http://prometheus:9090"),
    # Blank means "derive from prometheus_url" - one less thing to keep in
    # sync by hand, and getting it wrong silently breaks the Rules tab's
    # live reload rather than erroring anywhere obvious.
    ("prometheus_reload_url", "PROMETHEUS_RELOAD_URL", ""),
    # Not used to scrape (Prometheus does that) - only so the Settings page
    # can show whether the exporter is actually up.
    ("exporter_url", "EXPORTER_URL", "http://s4048-exporter:9101"),
)


def reload_url_for(settings_dict):
    """The Prometheus reload endpoint, derived from prometheus_url unless
    explicitly overridden."""
    explicit = (settings_dict.get("prometheus_reload_url") or "").strip()
    if explicit:
        return explicit
    base = (settings_dict.get("prometheus_url") or "").strip().rstrip("/")
    return f"{base}/-/reload" if base else ""


def load():
    """Returns the stored settings dict, or None if nothing's been
    configured yet on this volume."""
    if not SETTINGS_FILE.exists():
        return None
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save(data):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_FILE)


def bootstrap_from_env():
    """Builds an initial settings dict from env vars, for deployments that
    still set DATABASE_URL in .env the old way. Only used the very first
    time (no settings.json yet) - once saved, the file is what's read and
    edited from then on, so later env var changes have no effect (avoids
    the settings page silently getting overridden on every container
    restart)."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    seeded = {"database_url": database_url}
    for key, env_var, fallback in SERVICE_SETTINGS:
        seeded[key] = os.environ.get(env_var, fallback)
    return seeded


def redact_dsn(dsn):
    """postgresql://user:secret@host:5432/db -> postgresql://user:***@host:5432/db"""
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    creds, host_part = rest.split("@", 1)
    user = creds.split(":", 1)[0] if ":" in creds else creds
    return f"{scheme}://{user}:***@{host_part}"
