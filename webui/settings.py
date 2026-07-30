"""Local, GUI-editable deployment settings (Postgres DSN, Loki URL, webui
login) - stored as a small JSON file on the `webui-data` volume rather than
in Postgres itself, since the Postgres connection string is one of the
things being configured here (can't store your DB address in the DB).

Env vars (`WEBUI_USER`/`WEBUI_PASS`/`DATABASE_URL`/`LOKI_URL`) still work
as a first-boot seed - handy for docker-compose deployments that want to
keep configuring things the old way - but once a settings file exists it
wins, and the Settings page in the UI is the source of truth from then on.
"""
import hashlib
import json
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).parent
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", str(BASE_DIR / "data" / "settings.json")))

DEFAULT_LOKI_URL = "http://192.168.0.145:3100"


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password, stored_hash):
    try:
        salt, _ = stored_hash.split("$", 1)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return secrets.compare_digest(f"{salt}${digest.hex()}", stored_hash)


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
    still set WEBUI_USER/WEBUI_PASS/DATABASE_URL in .env the old way. Only
    used the very first time (no settings.json yet) - once saved, the file
    is what's read and edited from then on, so later env var changes have
    no effect (avoids the settings page silently getting overridden on
    every container restart)."""
    user = os.environ.get("WEBUI_USER")
    password = os.environ.get("WEBUI_PASS")
    database_url = os.environ.get("DATABASE_URL")
    if not user or not password or not database_url:
        return None
    return {
        "webui_user": user,
        "webui_pass_hash": hash_password(password),
        "database_url": database_url,
        "loki_url": os.environ.get("LOKI_URL", DEFAULT_LOKI_URL),
    }


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
