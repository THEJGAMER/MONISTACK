"""Param-injection test for /api/run (ROADMAP.md 0.2) - this was verified
by hand during development ("does a bogus port name actually get
rejected") and is now a real test: a `params` value that isn't one of the
device's own server-generated valid values (see devices.py's
`valid_values_for`) must be rejected with 400 *before* anything reaches
the device over SSH, not just eventually fail against the switch.

Imports `app` directly rather than spinning up the real stack (no
Postgres, no live device needed) - `require_operator` (the RBAC dependency
`/api/run` actually requires - see app.py) is overridden via FastAPI's own
dependency-injection mechanism, and `DEVICES_BY_ID`/`_session_locks`/
`_get_session` (plain module globals `app.py` reads at request time) are
monkeypatched with a fake device and a session stub that raises if it's
ever reached, so a passing test actually proves the rejection happens in
`api_run` itself, not that the fake SSH layer happened to also fail.
"""
import threading

import pytest
from fastapi.testclient import TestClient

import app as app_module
from devices import Device


class _FakeDevice(Device):
    @property
    def username(self):
        return "test"

    @property
    def password(self):
        return "test"

    @property
    def enable_password(self):
        return None

    @property
    def private_key(self):
        return None

    @property
    def passphrase(self):
        return None

    @property
    def host(self):
        return "192.0.2.1"  # TEST-NET-1, never a real device


@pytest.fixture
def client(monkeypatch):
    device = _FakeDevice(
        "testdev", "Test Device", "os9",
        ports=[{"prefix": "Te", "range": [1, 2]}],
    )
    monkeypatch.setattr(app_module, "DEVICES_BY_ID", {"testdev": device})
    monkeypatch.setattr(app_module, "_session_locks", {"testdev": threading.Lock()})

    def _unreachable(_device):
        raise AssertionError("a rejected param must never reach _get_session/SSH")

    monkeypatch.setattr(app_module, "_get_session", _unreachable)
    app_module.app.dependency_overrides[app_module.require_operator] = lambda: "test-user"
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def test_bogus_param_value_is_rejected_before_reaching_device(client):
    resp = client.post("/api/run", json={
        "device_id": "testdev",
        "category_id": "interfaces",
        "command_id": "if_transceiver",
        "params": {"port": "Te 1/99; reload"},  # not a real generated port
    })
    assert resp.status_code == 400


def test_missing_param_is_rejected(client):
    resp = client.post("/api/run", json={
        "device_id": "testdev",
        "category_id": "interfaces",
        "command_id": "if_transceiver",
        "params": {},
    })
    assert resp.status_code == 400


def test_valid_param_value_is_accepted(client, monkeypatch):
    """Confirms the rejection above is actually about the *value*, not
    something else failing first - the exact same request shape with a
    real, server-generated port value reaches the (stubbed) SSH layer."""
    class _FakeSwitch:
        def run(self, cmd):
            assert cmd == "show interfaces Te 1/1 transceiver"
            return "fake output"

    class _FakeResults:
        def save(self, *args, **kwargs):
            return {"filename": "fake.md"}

    monkeypatch.setattr(app_module, "_get_session", lambda device: _FakeSwitch())
    monkeypatch.setattr(app_module, "RESULTS", _FakeResults())

    resp = client.post("/api/run", json={
        "device_id": "testdev",
        "category_id": "interfaces",
        "command_id": "if_transceiver",
        "params": {"port": "Te 1/1"},
    })
    assert resp.status_code == 200


def test_unknown_command_id_is_rejected(client):
    resp = client.post("/api/run", json={
        "device_id": "testdev",
        "category_id": "interfaces",
        "command_id": "not_a_real_command",
    })
    assert resp.status_code == 404


def test_unknown_device_id_is_rejected(client):
    resp = client.post("/api/run", json={
        "device_id": "not-a-real-device",
        "category_id": "system",
        "command_id": "version",
    })
    assert resp.status_code == 404
