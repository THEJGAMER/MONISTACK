"""The bastion session itself: what reaches the wire, and what is kept.

`bastion_policy` decides whether a line is allowed; these tests are about
whether that decision is actually honoured - that a refused line puts no
bytes on the socket, that an allowed one is preceded by a Ctrl-U so the
device cannot be running something other than what was checked, that the
recording is written whether or not anyone is watching, and that the
caps on concurrent sessions hold (a switch has a handful of SSH slots and
the status poller is already using one).

Nothing here opens a socket: the session is built around a fake channel
so the bytes it would have sent can be read back directly.
"""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import bastion  # noqa: E402
import bastion_policy as policy  # noqa: E402


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.resized = None

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def recv(self, n):
        return b""

    def close(self):
        self.closed = True

    def exit_status_ready(self):
        return False

    def resize_pty(self, width=None, height=None):
        self.resized = (width, height)

    @property
    def wire(self):
        return "".join(self.sent)


class FakeStore:
    def __init__(self):
        self.started = []
        self.finished = []
        self.chunks = []

    def start(self, row):
        self.started.append(row)

    def finish(self, sid, ended_at, reason, bi, bo, commands, refused, truncated):
        self.finished.append({"id": sid, "reason": reason, "commands": commands,
                              "refused": refused, "truncated": truncated})

    def add_chunk(self, sid, seq, at, stream, data):
        self.chunks.append({"seq": seq, "stream": stream, "data": data})

    def streams(self, stream):
        return [c["data"] for c in self.chunks if c["stream"] == stream]


class FakeAudit:
    def __init__(self):
        self.entries = []

    def record(self, actor, action, target=None, detail=None):
        self.entries.append({"actor": actor, "action": action, "target": target, "detail": detail})

    def actions(self):
        return [e["action"] for e in self.entries]


class FakeDevice:
    def __init__(self, id="s4048", platform="os9"):
        self.id = id
        self.name = "S4048 core"
        self.platform = platform
        self.host = "192.168.0.1"
        self.username = "admin"
        self.password = "pw"
        self.enable_password = "pw"
        self.private_key = None
        self.passphrase = None
        self.port = 22


def make_session(mode=bastion.MODE_READONLY, platform="os9", store=None, audit=None):
    store = store or FakeStore()
    session = bastion.BastionSession(FakeDevice(platform=platform), "jacob", "operator", mode,
                                     "10.0.0.5", store, audit=audit)
    session._chan = FakeChannel()
    return session


# --- what reaches the wire ----------------------------------------------

def test_an_allowed_line_is_sent_behind_a_kill_line():
    """Ctrl-U first, every time. Whatever the device might have sitting in
    its input buffer - a stray keystroke, a half-finished completion - is
    discarded before the checked text goes in, so what the device runs
    cannot differ from what was checked."""
    session = make_session()

    sent, reason = session.submit_line("show version")

    assert (sent, reason) == (True, None)
    assert session._chan.wire == bastion.KILL_LINE + "show version\r"


def test_a_refused_line_puts_nothing_on_the_wire():
    session = make_session()

    sent, reason = session.submit_line("configure terminal")

    assert sent is False and "not a read-only command" in reason
    assert session._chan.sent == [], "the device must not see a single byte of a refused command"
    assert session.refused == 1 and session.commands == 0


def test_a_full_session_sends_what_a_read_only_one_would_refuse():
    session = make_session(mode=bastion.MODE_FULL)

    sent, reason = session.submit_line("configure terminal")

    assert sent is True and reason is None
    assert "configure terminal" in session._chan.wire


def test_a_read_only_session_refuses_raw_keystrokes_outright():
    """The whole guarantee rests on text arriving as whole, checked lines.
    A raw keystroke path in read-only mode would be a way around it, so
    there isn't one."""
    session = make_session()

    with pytest.raises(bastion.BastionError):
        session.send_raw("configure terminal\r")
    assert session._chan.sent == []


def test_only_the_allowlisted_control_keys_go_through():
    session = make_session()

    session.send_key("ctrl-c")
    with pytest.raises(bastion.BastionError):
        session.send_key("ctrl-d")

    assert session._chan.wire == "\x03"


def test_context_help_is_sent_without_a_newline():
    """`?` is how anyone finds their way around a switch CLI, and it is
    safe precisely because nothing is submitted - the partial line and a
    question mark, then the buffer is wiped again."""
    session = make_session()

    session.help_request("show ip ")

    wire = session._chan.wire
    assert wire == bastion.KILL_LINE + "show ip ?"
    assert "\r" not in wire and "\n" not in wire


def test_context_help_cannot_smuggle_a_newline():
    session = make_session()

    with pytest.raises(bastion.BastionError):
        session.help_request("show version\rconfigure terminal")
    assert session._chan.sent == []


def test_a_closed_session_refuses_to_write():
    session = make_session()
    session.close("done")

    with pytest.raises(bastion.BastionError):
        session.submit_line("show version")


def test_resize_is_clamped_to_something_a_terminal_could_be():
    session = make_session()

    session.resize(99999, -4)

    assert session._chan.resized == (500, 5)


# --- the recording ------------------------------------------------------

def test_every_submitted_line_is_recorded_as_its_own_chunk():
    store = FakeStore()
    session = make_session(store=store)

    session.submit_line("show version")
    session.submit_line("show interfaces status")

    assert store.streams("in") == ["show version", "show interfaces status"]


def test_a_refusal_is_recorded_with_its_reason():
    """A session where somebody repeatedly tried to reconfigure a switch
    and was stopped is exactly the session someone will want to read back
    later, so the attempt is kept, not just the successes."""
    store = FakeStore()
    session = make_session(store=store)

    session.submit_line("reload")

    note = "\n".join(store.streams("note"))
    assert "REFUSED: reload" in note


def test_output_is_coalesced_but_not_lost():
    store = FakeStore()
    recorder = bastion.Recorder(store, "sid", max_bytes=1_000_000)

    recorder.output("hello ")
    recorder.output("world")
    recorder.flush()

    assert store.streams("out") == ["hello world"]


def test_a_runaway_session_stops_recording_and_says_so():
    """`show tech-support` is megabytes. A transcript nobody can load is
    worth less than a short one that admits where it was cut."""
    store = FakeStore()
    recorder = bastion.Recorder(store, "sid", max_bytes=100)

    recorder.output("x" * 80)
    recorder.output("y" * 80)
    recorder.output("z" * 80)

    assert recorder.truncated is True
    assert "".join(store.streams("out")) == "x" * 80
    assert any("recording stopped" in n for n in store.streams("note"))


def test_closing_writes_the_header_row_out():
    store = FakeStore()
    session = make_session(store=store)
    session.submit_line("show version")
    session.submit_line("reload")

    session.close("browser closed")

    assert store.finished == [{"id": session.id, "reason": "browser closed",
                               "commands": 1, "refused": 1, "truncated": False}]


def test_closing_twice_only_counts_once():
    store = FakeStore()
    session = make_session(store=store)

    session.close("first")
    session.close("second")

    assert len(store.finished) == 1 and store.finished[0]["reason"] == "first"


# --- the audit trail ----------------------------------------------------

def test_commands_and_refusals_both_reach_the_audit_log():
    audit = FakeAudit()
    session = make_session(audit=audit)

    session.submit_line("show version")
    session.submit_line("configure terminal")
    session.close("done")

    assert audit.actions() == ["bastion.command", "bastion.refused", "bastion.close"]
    refusal = audit.entries[1]
    assert refusal["target"] == "configure terminal"
    assert "not a read-only command" in refusal["detail"]["reason"]


def test_a_broken_audit_log_does_not_break_the_terminal():
    """Recording matters, but a database hiccup must cost the recording,
    not the session someone is using to fix an outage."""
    class Broken:
        def record(self, *a, **kw):
            raise RuntimeError("postgres is down")

    session = make_session(audit=Broken())

    assert session.submit_line("show version")[0] is True


# --- line reassembly (full mode transcripts) ----------------------------

def test_keystrokes_are_reassembled_into_the_commands_they_spelled():
    assembler = bastion.LineAssembler()

    done = assembler.feed("show ver")
    done += assembler.feed("sion\rconf")

    assert done == ["show version"]
    assert assembler.pending == "conf"


def test_backspace_and_ctrl_u_are_honoured_in_the_transcript():
    assembler = bastion.LineAssembler()

    assert assembler.feed("show verx\x7fsion\r") == ["show version"]
    assert assembler.feed("reload\x15show version\r") == ["show version"]


def test_an_abandoned_line_is_not_recorded_as_a_command():
    assembler = bastion.LineAssembler()

    assert assembler.feed("reload\x03") == []
    assert assembler.pending == ""


# --- the caps -----------------------------------------------------------

class Limits:
    """A fixed set of caps, standing in for the env-read ones."""

    def __init__(self, per_device=2, total=3):
        self.per_device = per_device
        self.total = total
        self.idle_seconds = 900
        self.max_seconds = 14400
        self.max_record_bytes = 1_000_000


def manager_with(monkeypatch, limits):
    """A manager whose sessions never dial anything - open() is replaced
    by one that just reports a banner."""
    monkeypatch.setattr(bastion.BastionSession, "open", lambda self, cols=120, rows=32: "S4048#")
    return bastion.BastionManager(FakeStore(), audit=FakeAudit(), limits=limits)


def test_a_device_only_gives_up_so_many_ssh_slots(monkeypatch):
    """Not a policy preference: Dell OS9 has a handful of vty slots and
    the app's own pooled session already holds one. Filling them with
    terminals would take the status poller down with it."""
    manager = manager_with(monkeypatch, Limits(per_device=1, total=5))
    device = FakeDevice()
    manager.open(device, "jacob", "admin", "full", "10.0.0.5")

    with pytest.raises(bastion.BastionError) as e:
        manager.open(device, "sam", "admin", "full", "10.0.0.6")

    assert "already has 1 console session" in str(e.value)
    assert "jacob" in str(e.value), "say who is holding it, so the answer is to go ask them"


def test_the_fleet_wide_cap_holds_too(monkeypatch):
    manager = manager_with(monkeypatch, Limits(per_device=5, total=2))
    manager.open(FakeDevice("a"), "jacob", "admin", "full", "ip")
    manager.open(FakeDevice("b"), "jacob", "admin", "full", "ip")

    with pytest.raises(bastion.BastionError):
        manager.open(FakeDevice("c"), "jacob", "admin", "full", "ip")


def test_a_closed_session_gives_its_slot_back(monkeypatch):
    manager = manager_with(monkeypatch, Limits(per_device=1, total=5))
    device = FakeDevice()
    session, _ = manager.open(device, "jacob", "admin", "full", "ip")

    session.close("done")
    manager.open(device, "sam", "admin", "full", "ip")   # must not raise

    assert len(manager.live()) == 1


def test_a_session_that_fails_to_open_does_not_hold_a_slot(monkeypatch):
    def explode(self, cols=120, rows=32):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(bastion.BastionSession, "open", explode)
    manager = bastion.BastionManager(FakeStore(), limits=Limits(per_device=1))

    with pytest.raises(RuntimeError):
        manager.open(FakeDevice(), "jacob", "admin", "full", "ip")

    assert manager.live() == []


def test_an_admin_can_end_somebody_elses_session(monkeypatch):
    manager = manager_with(monkeypatch, Limits())
    session, _ = manager.open(FakeDevice(), "jacob", "operator", "readonly", "ip")
    session._chan = FakeChannel()

    assert manager.kill(session.id, "sam") is True
    assert session.closed and session.end_reason == "ended by sam"
    assert manager.kill("not-a-session", "sam") is False


# --- the kill switch ----------------------------------------------------

def test_the_bastion_can_be_switched_off_entirely(monkeypatch):
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("BASTION_ENABLED", value)
        assert bastion.enabled() is False
    monkeypatch.setenv("BASTION_ENABLED", "1")
    assert bastion.enabled() is True
    monkeypatch.delenv("BASTION_ENABLED")
    assert bastion.enabled() is True, "on by default - the flag exists to turn it off"


# --- login banners ------------------------------------------------------

def test_a_login_banner_that_moves_the_cursor_is_flattened_to_text():
    """Junos's shell sets tab stops by jumping the cursor to row 50 and
    back. Left in, that parks the cursor on the last line of a shorter
    terminal and the whole session then scrolls up from the bottom of a
    blank screen - which is exactly how it looked against a real EX3300
    before this existed."""
    raw = ("Last login: Thu Sep 17\r\r\n--- JUNOS 15.1R7.9 built 2018-09-11 ---\r\n"
           "\r\x1b[3g\x1b[50;9H\x1bH\x1b[50;17H\x1bH\r\rroot@:RE:0% cli\r\n{master:0}\r\nroot> ")

    cleaned = bastion.clean_banner(raw)

    assert "\x1b[50;9H" not in cleaned and "\x1b[3g" not in cleaned and "\x1bH" not in cleaned
    assert "JUNOS 15.1R7.9 built 2018-09-11" in cleaned
    assert cleaned.endswith("root> ")


def test_colour_survives_the_banner_clean():
    """Only cursor movement is the problem; a device that colours its
    banner should still come out coloured."""
    assert bastion.clean_banner("\x1b[32mS4048\x1b[0m>") == "\x1b[32mS4048\x1b[0m>"


# --- sessions that outstay their welcome --------------------------------

def test_a_session_left_alone_is_closed():
    """An abandoned tab holds one of a switch's few SSH slots until
    something reclaims it."""
    import time as _time
    session = make_session()
    session.limits = Limits()
    session.limits.idle_seconds = 5
    session.last_activity = _time.monotonic() - 10

    assert "idle" in session._expired()


def test_a_busy_session_is_not_mistaken_for_an_idle_one():
    session = make_session()
    session.limits = Limits()
    session.limits.idle_seconds = 900
    session.submit_line("show version")

    assert session._expired() is None


def test_a_session_cannot_run_forever():
    from datetime import timedelta
    session = make_session()
    session.limits = Limits()
    session.limits.max_seconds = 3600
    session.started_at = session.started_at - timedelta(hours=2)

    assert "1-hour session limit" in session._expired()


def test_zero_disables_a_limit_rather_than_meaning_immediately():
    from datetime import timedelta
    import time as _time
    session = make_session()
    session.limits = Limits()
    session.limits.idle_seconds = 0
    session.limits.max_seconds = 0
    session.last_activity = _time.monotonic() - 100_000
    session.started_at = session.started_at - timedelta(days=3)

    assert session._expired() is None
