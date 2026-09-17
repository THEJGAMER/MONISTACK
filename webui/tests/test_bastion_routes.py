"""The door to the bastion: who gets a terminal, and who does not.

A WebSocket handshake is not a fetch. It carries the session cookie, it
is not subject to CORS, and there is no preflight - so a page on another
site can try to open one in a logged-in person's browser and the only
thing that stops it is the server checking Origin. That check, and the
role gate beside it, are what these tests pin.

The socket never reaches a device here: BastionManager.open is replaced,
so what is under test is the decision to let the handshake through at
all.
"""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import itsdangerous
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import app as app_module  # noqa: E402
import bastion as bastion_module  # noqa: E402


def _cookie(role, username="jacob", hours=1):
    signer = itsdangerous.TimestampSigner(str(app_module.SESSION_SECRET_KEY))
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp()
    return signer.sign(base64.b64encode(json.dumps(
        {"username": username, "role": role, "expires_at": expires}).encode())).decode()


class FakeDevice:
    id = "s4048"
    name = "S4048 core"
    platform = "os9"
    host = "192.168.0.1"


class FakeSession:
    """Stands in for a live SSH session - records what the route asked it
    to do rather than doing it."""

    def __init__(self, mode):
        self.id = "sess-1"
        self.mode = mode
        self.platform = "os9"
        self.read_only = mode == "readonly"
        self.closed = False
        self.lines = []
        self.raw = []
        self.on_output = None

    def attach(self, on_output, on_closed):
        self.on_output = on_output

    def submit_line(self, text):
        self.lines.append(text)
        if text.startswith("configure"):
            return False, "'configure' is not a read-only command"
        self.on_output(f"{text}\r\nS4048#")
        return True, None

    def send_raw(self, data):
        self.raw.append(data)

    def send_key(self, name):
        pass

    def help_request(self, text):
        pass

    def resize(self, cols, rows):
        pass

    def close(self, reason="closed"):
        self.closed = True


class FakeManager:
    def __init__(self):
        self.store = None
        self.opened = []
        self.session = None

    def open(self, device, actor, role, mode, client_ip, cols=120, rows=32):
        self.opened.append({"device": device.id, "actor": actor, "role": role, "mode": mode,
                            "ip": client_ip, "cols": cols, "rows": rows})
        self.session = FakeSession(mode)
        return self.session, "S4048#"

    def release(self, sid):
        pass

    def live(self):
        return []


@pytest.fixture
def manager(monkeypatch):
    fake = FakeManager()
    monkeypatch.setattr(app_module, "BASTION", fake)
    monkeypatch.setattr(app_module, "DEVICES", [FakeDevice()])
    monkeypatch.setattr(app_module, "DEVICES_BY_ID", {"s4048": FakeDevice()})
    monkeypatch.setattr(app_module, "STORE", object())
    monkeypatch.setattr(app_module, "DB_ERROR", None)
    monkeypatch.setenv("BASTION_ENABLED", "1")
    return fake


@pytest.fixture
def client(manager):
    return TestClient(app_module.app)


def connect(client, role, params="device=s4048&mode=readonly", origin="http://testserver"):
    headers = {"Origin": origin} if origin else {}
    client.cookies.set("switchboard_session", _cookie(role))
    return client.websocket_connect(f"/api/bastion/ws?{params}", headers=headers)


# --- who gets in --------------------------------------------------------

def test_an_operator_gets_a_read_only_terminal(client, manager):
    with connect(client, "operator") as ws:
        ready = ws.receive_json()

    assert ready["t"] == "ready" and ready["read_only"] is True
    assert manager.opened[0]["mode"] == "readonly"


def test_an_admin_asking_for_full_access_gets_it(client, manager):
    with connect(client, "admin", "device=s4048&mode=full") as ws:
        assert ws.receive_json()["mode"] == "full"


def test_an_operator_asking_for_full_access_gets_read_only(client, manager):
    """Silently downgraded rather than refused: the role is the answer and
    the request is only a preference. The socket is opened here, so the
    check that matters is what mode it was opened in."""
    with connect(client, "operator", "device=s4048&mode=full") as ws:
        assert ws.receive_json()["mode"] == "readonly"
    assert manager.opened[0]["mode"] == "readonly"


def test_a_viewer_gets_no_terminal_at_all(client, manager):
    with pytest.raises(Exception):
        with connect(client, "viewer") as ws:
            ws.receive_json()
    assert manager.opened == []


def test_no_session_no_terminal(client, manager):
    client.cookies.clear()
    with pytest.raises(Exception):
        with client.websocket_connect("/api/bastion/ws?device=s4048&mode=readonly") as ws:
            ws.receive_json()
    assert manager.opened == []


def test_an_expired_session_is_not_a_session(client, manager):
    client.cookies.set("switchboard_session", _cookie("admin", hours=-1))
    with pytest.raises(Exception):
        with client.websocket_connect("/api/bastion/ws?device=s4048&mode=full") as ws:
            ws.receive_json()
    assert manager.opened == []


def test_an_unknown_device_is_refused(client, manager):
    with pytest.raises(Exception):
        with connect(client, "admin", "device=not-a-switch&mode=full") as ws:
            ws.receive_json()
    assert manager.opened == []


# --- the cross-site case ------------------------------------------------

def test_another_site_cannot_open_a_terminal_in_your_browser(client, manager):
    """The one attack a WebSocket is uniquely exposed to: no preflight, no
    CORS, and the cookie goes along for the ride. Origin is the check."""
    with pytest.raises(Exception):
        with connect(client, "admin", origin="https://evil.example.com") as ws:
            ws.receive_json()
    assert manager.opened == []


def test_a_request_with_no_origin_is_not_a_browser_and_is_allowed(client, manager):
    """A script with a cookie jar sends no Origin; a browser always does.
    Refusing this would break every non-browser client without stopping
    the attack it is aimed at."""
    with connect(client, "admin", origin=None) as ws:
        assert ws.receive_json()["t"] == "ready"


# --- the conversation ---------------------------------------------------

def test_a_line_is_forwarded_and_its_output_comes_back(client, manager):
    with connect(client, "operator") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"t": "line", "data": "show version"}))
        out = ws.receive_json()

    assert out["t"] == "out" and "show version" in out["data"]
    assert manager.session.lines == ["show version"]


def test_a_refusal_comes_back_with_its_reason(client, manager):
    with connect(client, "operator") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"t": "line", "data": "configure terminal"}))
        msg = ws.receive_json()

    assert msg["t"] == "refused" and "not a read-only command" in msg["reason"]


def test_nonsense_on_the_socket_is_ignored_rather_than_fatal(client, manager):
    with connect(client, "operator") as ws:
        ws.receive_json()
        ws.send_text("this is not json")
        ws.send_text(json.dumps({"t": "who-knows"}))
        ws.send_text(json.dumps({"t": "line", "data": "show version"}))
        assert ws.receive_json()["t"] == "out"


def test_closing_the_browser_closes_the_ssh_session(client, manager):
    with connect(client, "operator") as ws:
        ws.receive_json()

    assert manager.session.closed is True, "an abandoned tab must not hold an SSH slot"


def test_the_terminal_size_the_browser_asked_for_is_used(client, manager):
    with connect(client, "admin", "device=s4048&mode=full&cols=200&rows=50") as ws:
        ws.receive_json()
    assert (manager.opened[0]["cols"], manager.opened[0]["rows"]) == (200, 50)


def test_an_absurd_terminal_size_is_clamped(client, manager):
    with connect(client, "admin", "device=s4048&mode=full&cols=99999&rows=0") as ws:
        ws.receive_json()
    assert (manager.opened[0]["cols"], manager.opened[0]["rows"]) == (500, 5)


# --- the kill switch ----------------------------------------------------

def test_a_deployment_can_switch_the_whole_thing_off(client, manager, monkeypatch):
    monkeypatch.setenv("BASTION_ENABLED", "0")

    with pytest.raises(Exception):
        with connect(client, "admin") as ws:
            ws.receive_json()

    assert manager.opened == []


# --- the recordings -----------------------------------------------------

class FakeStore:
    rows = [
        {"id": "a", "actor": "jacob", "device_id": "s4048", "mode": "readonly"},
        {"id": "b", "actor": "sam", "device_id": "s4048", "mode": "full"},
    ]

    def __init__(self):
        self.list_calls = []

    def list(self, limit=100, actor=None, device_id=None):
        self.list_calls.append(actor)
        return [r for r in self.rows if actor is None or r["actor"] == actor]

    def get(self, sid):
        return next((r for r in self.rows if r["id"] == sid), None)

    def chunks(self, sid, limit=20000):
        return [{"seq": 1, "at": "2026-09-17T00:00:00Z", "stream": "out", "data": "S4048#"}]


def test_an_operator_only_sees_their_own_recordings(client, manager):
    manager.store = FakeStore()
    client.cookies.set("switchboard_session", _cookie("operator", "jacob"))

    rows = client.get("/api/bastion/sessions").json()

    assert [r["id"] for r in rows] == ["a"]
    assert manager.store.list_calls == ["jacob"]


def test_an_admin_sees_everybodys(client, manager):
    manager.store = FakeStore()
    client.cookies.set("switchboard_session", _cookie("admin", "jacob"))

    rows = client.get("/api/bastion/sessions").json()

    assert [r["id"] for r in rows] == ["a", "b"]


def test_one_operator_cannot_read_anothers_transcript(client, manager):
    """A recording is somebody's keystrokes. If every operator could read
    every other operator's, the recording itself would become a reason not
    to use the bastion."""
    manager.store = FakeStore()
    client.cookies.set("switchboard_session", _cookie("operator", "jacob"))

    assert client.get("/api/bastion/sessions/a").status_code == 200
    assert client.get("/api/bastion/sessions/b").status_code == 403


def test_an_admin_can_read_any_transcript(client, manager):
    manager.store = FakeStore()
    client.cookies.set("switchboard_session", _cookie("admin", "jacob"))

    assert client.get("/api/bastion/sessions/b").status_code == 200


def test_a_viewer_cannot_read_recordings_at_all(client, manager):
    manager.store = FakeStore()
    client.cookies.set("switchboard_session", _cookie("viewer", "jacob"))

    assert client.get("/api/bastion/sessions").status_code == 403


def test_access_tells_the_page_what_it_may_offer(client, manager):
    client.cookies.set("switchboard_session", _cookie("operator", "jacob"))

    body = client.get("/api/bastion/access").json()

    assert body["modes"] == ["readonly"]
    assert [d["id"] for d in body["devices"]] == ["s4048"]
    assert body["limits"]["per_device"] >= 1
