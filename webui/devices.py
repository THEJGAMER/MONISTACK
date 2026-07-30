import os

import yaml


class DeviceConfigError(Exception):
    pass


def _generate_ports(port_ranges):
    ports = []
    for spec in port_ranges or []:
        prefix = spec["prefix"]
        lo, hi = spec["range"]
        for n in range(lo, hi + 1):
            ports.append(f"{prefix} 1/{n}")
    return ports


def _generate_port_channels(spec):
    if not spec:
        return []
    lo, hi = spec["range"]
    return [str(n) for n in range(lo, hi + 1)]


class Device:
    """Common behavior for both statically-configured (devices.yaml) and
    UI-added (store.py) devices. Subclasses only need to implement the
    credential properties (host/username/password/private_key/...)."""

    source = "unknown"

    def __init__(self, id, name, platform, make="", model="", ports=None, port_channels=None):
        self.id = id
        self.name = name
        self.platform = platform
        self.make = make
        self.model = model
        self.valid_ports = _generate_ports(ports)
        self.valid_port_channels = _generate_port_channels(port_channels)
        self._param_values = {"port": self.valid_ports, "port_channel": self.valid_port_channels}

    def valid_values_for(self, param_name):
        return self._param_values.get(param_name, [])

    @property
    def auth_method(self):
        return "password"

    @property
    def private_key(self):
        return None

    @property
    def passphrase(self):
        return None

    def to_public_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "platform": self.platform,
            "make": self.make,
            "model": self.model,
            "auth_method": self.auth_method,
            "source": self.source,
        }


class StaticDevice(Device):
    """A device defined in devices.yaml - credentials come from env vars,
    named (not valued) in the yaml, so the yaml file itself holds no
    secrets and is safe to commit."""

    source = "static"

    def __init__(self, raw):
        super().__init__(
            id=raw["id"],
            name=raw["name"],
            platform=raw.get("platform", "os9"),
            make=raw.get("make", ""),
            model=raw.get("model", ""),
            ports=raw.get("ports"),
            port_channels=raw.get("port_channels"),
        )
        self._host_env = raw["host_env"]
        self._user_env = raw["user_env"]
        self._pass_env = raw["pass_env"]
        self._enable_pass_env = raw.get("enable_pass_env")

    @property
    def host(self):
        return _require_env(self._host_env, self.id)

    @property
    def username(self):
        return _require_env(self._user_env, self.id)

    @property
    def password(self):
        return _require_env(self._pass_env, self.id)

    @property
    def enable_password(self):
        if self._enable_pass_env:
            return os.environ.get(self._enable_pass_env) or self.password
        return self.password


class StoredDevice(Device):
    """A device added through the UI (Devices page) - credentials are
    stored inline in store.py's JSON file, either a password or an SSH
    private key (pasted PEM text) plus optional passphrase."""

    source = "added"

    def __init__(self, raw):
        super().__init__(
            id=raw["id"],
            name=raw["name"],
            platform=raw.get("platform", "os9"),
            make=raw.get("make", ""),
            model=raw.get("model", ""),
            ports=raw.get("ports"),
            port_channels=raw.get("port_channels"),
        )
        self._host = raw["host"]
        self._username = raw["username"]
        self._auth_method = raw.get("auth_method", "password")
        self._password = raw.get("password")
        self._private_key = raw.get("private_key")
        self._passphrase = raw.get("passphrase")
        self._enable_password = raw.get("enable_password")

    @property
    def host(self):
        return self._host

    @property
    def username(self):
        return self._username

    @property
    def auth_method(self):
        return self._auth_method

    @property
    def password(self):
        return self._password

    @property
    def private_key(self):
        return self._private_key

    @property
    def passphrase(self):
        return self._passphrase

    @property
    def enable_password(self):
        # Enable mode is independent of how you logged in over SSH - a
        # key-authenticated login can still need a typed enable password.
        return self._enable_password or self._password


def _require_env(name, device_id):
    val = os.environ.get(name)
    if not val:
        raise DeviceConfigError(f"env var {name} is not set (needed for device {device_id!r})")
    return val


def load_static_devices(path):
    with open(path) as f:
        raw = yaml.safe_load(f)
    return [StaticDevice(d) for d in raw["devices"]]


def load_devices(static_path, store):
    devices = load_static_devices(static_path)
    devices += [StoredDevice(d) for d in store.load()]
    if len(set(d.id for d in devices)) != len(devices):
        raise DeviceConfigError("duplicate device id across devices.yaml and the device store")
    return devices
