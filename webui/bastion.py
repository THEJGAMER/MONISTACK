"""The console bastion: a recorded, policy-checked interactive SSH session.

Everywhere else in Switchboard, the device only ever receives a command
this repo wrote down first - that is what makes the Console safe to hand
to someone at 3am. The bastion is the deliberate exception, because
eventually something goes wrong that no allowlist anticipated and a
person needs a real prompt.

It is built so that being the exception costs as little as possible:

**Nobody learns the device's password.** The session authenticates with
the credentials Switchboard already holds, so access to a switch is
granted and revoked in Keycloak, not by sharing an enable secret that
then lives in somebody's notes forever.

**Read-only is enforced twice.** A read-only session refuses anything
`bastion_policy` does not recognise as read-only *and*, on Dell OS9,
never sends `enable` - so it sits in user EXEC where the device itself
rejects configuration. One of those has to be right; both have to be
wrong to cause damage.

**Text reaches the device only as whole, checked lines.** A read-only
session's keystrokes never leave the browser: the page echoes them
locally and submits a finished line, which is checked and then sent
prefixed with Ctrl-U so that whatever might be sitting in the device's
input buffer is wiped first. There is no way to assemble a command one
character at a time past the check.

**Everything is recorded.** Every submitted line and every byte the
device printed goes into `bastion_chunks` with timestamps, so a session
can be replayed afterwards at the speed it happened. The recording is
not optional and there is no flag to turn it off.

**A separate SSH connection, never the pooled one.** `app.py` keeps one
long-lived session per device that the status poller and the Console
share, and it works by reading up to a known prompt. An interactive
session would leave that parser mid-stream, so the bastion always dials
its own connection - and pays for it with hard caps on how many can be
open at once, because Dell OS9 has only a handful of vty slots.
"""
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import paramiko

from ssh_client import (
    JUNOS_SHELL_PROMPT_RE,
    OPNSENSE_MENU_PROMPT_RE,
    OPNSENSE_SHELL_PROMPT_RE,
    PROMPT_RE,
    SwitchSSHError,
    load_private_key,
)
import bastion_policy as policy

log = logging.getLogger("webui.bastion")

MODE_FULL = policy.MODE_FULL
MODE_READONLY = policy.MODE_READONLY

# Login banners do more than print text. Junos's shell runs the equivalent
# of `tabs`, which clears the tab stops and then sets one every eight
# columns by jumping the cursor to row 50 and back
# (ESC[3g, then ESC[50;9H ESC H, ESC[50;17H ESC H, ...). In a terminal
# shorter than fifty rows that lands the cursor on the last line, so
# everything printed afterwards - the whole session - scrolls up from the
# bottom of an otherwise blank screen. Confirmed live against a real
# EX3300: the first screenful sat two thirds of the way down the pane.
#
# Cursor positioning is therefore stripped out of the connect-time banner
# (and only the banner, which is a one-off blob of text reproduced for
# context, not a live screen). Colour and other attributes are left alone.
# The recording gets the cleaned version too, so a replay does not
# reproduce the same artefact.
_BANNER_CURSOR = re.compile(r"\x1b\[[0-9;]*[HfABCDdGJ]|\x1bH|\x1b\[[0-9]*g")


def clean_banner(text):
    return _BANNER_CURSOR.sub("", text or "").replace("\r\r", "\r")


# Ctrl-U. Sent before every approved line so the device's input buffer
# starts empty even if a previous keystroke, a tab completion or a `?`
# left something in it - the one thing that could otherwise make what the
# device runs differ from what was checked.
KILL_LINE = "\x15"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        log.warning("%s is not an integer - using %s", name, default)
        return default


def enabled():
    """The kill switch. A deployment that does not want a free-text path
    to its switches sets BASTION_ENABLED=0 and the routes 404."""
    return os.environ.get("BASTION_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


class Limits:
    """Read from the environment on every session open, not cached, so a
    deployment can tighten them without a restart."""

    @property
    def per_device(self):
        return _env_int("BASTION_MAX_SESSIONS_PER_DEVICE", 2)

    @property
    def total(self):
        return _env_int("BASTION_MAX_SESSIONS", 8)

    @property
    def idle_seconds(self):
        return _env_int("BASTION_IDLE_TIMEOUT_SECONDS", 900)

    @property
    def max_seconds(self):
        return _env_int("BASTION_MAX_SESSION_SECONDS", 14400)

    @property
    def max_record_bytes(self):
        return _env_int("BASTION_MAX_RECORD_BYTES", 8_000_000)


LIMITS = Limits()


# --- recording ---------------------------------------------------------

class Recorder:
    """Buffers the transcript and writes it in chunks.

    A chunk per keystroke would be an INSERT per keystroke, so output is
    coalesced until it is either big enough or old enough to be worth a
    write. Input lines are never coalesced - each one is its own chunk,
    because a command is the unit anyone replaying this actually looks
    for.

    Past `max_bytes` the output stream stops being stored and a note
    takes its place. A single `show tech-support` is megabytes, and a
    transcript nobody can load is worth less than a truthful note saying
    where it was cut.
    """

    FLUSH_BYTES = 4096
    FLUSH_SECONDS = 2.0

    def __init__(self, store, session_id, max_bytes):
        self.store = store
        self.session_id = session_id
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._buf = []
        self._buf_len = 0
        self._last_flush = time.monotonic()
        self._seq = 0
        self.recorded = 0
        self.truncated = False

    def _append(self, stream, text):
        self._seq += 1
        self.store.add_chunk(self.session_id, self._seq, _iso(_now()), stream, text)

    def output(self, text):
        if not text:
            return
        with self._lock:
            if self.truncated:
                return
            if self.recorded + len(text) > self.max_bytes:
                self._flush_locked()
                self.truncated = True
                self._append("note", f"-- recording stopped at {self.max_bytes} bytes; "
                                     "the session continued but its output is no longer stored --")
                return
            self.recorded += len(text)
            self._buf.append(text)
            self._buf_len += len(text)
            if self._buf_len >= self.FLUSH_BYTES or (time.monotonic() - self._last_flush) >= self.FLUSH_SECONDS:
                self._flush_locked()

    def line(self, text):
        with self._lock:
            self._flush_locked()          # keep the transcript in order
            self._append("in", text)

    def note(self, text):
        with self._lock:
            self._flush_locked()
            self._append("note", text)

    def tick(self):
        """Called from the reader loop so a quiet session's last few bytes
        do not sit unwritten until it closes."""
        with self._lock:
            if self._buf and (time.monotonic() - self._last_flush) >= self.FLUSH_SECONDS:
                self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf = []
        self._buf_len = 0
        self._last_flush = time.monotonic()
        try:
            self._append("out", text)
        except Exception:
            log.exception("could not write bastion transcript chunk for %s", self.session_id)


# --- the store ---------------------------------------------------------

class BastionStore:
    """Session headers and transcripts. Every write is best-effort: a
    database hiccup must not kill a live terminal, it must only cost the
    recording - which is loud in the transcript rather than silent,
    because `open()` refuses to start a session it cannot record."""

    def __init__(self, db):
        self.db = db

    def start(self, row):
        self.db.execute(
            "INSERT INTO bastion_sessions (id, device_id, device_name, host, platform, actor, role, mode, "
            "client_ip, started_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (row["id"], row["device_id"], row["device_name"], row["host"], row["platform"],
             row["actor"], row["role"], row["mode"], row["client_ip"], row["started_at"]),
        )

    def finish(self, session_id, ended_at, reason, bytes_in, bytes_out, commands, refused, truncated):
        try:
            self.db.execute(
                "UPDATE bastion_sessions SET ended_at=%s, end_reason=%s, bytes_in=%s, bytes_out=%s, "
                "commands=%s, refused=%s, truncated=%s WHERE id=%s",
                (ended_at, reason, bytes_in, bytes_out, commands, refused, 1 if truncated else 0, session_id),
            )
        except Exception:
            log.exception("could not close out bastion session %s", session_id)

    def add_chunk(self, session_id, seq, at, stream, data):
        self.db.execute(
            "INSERT INTO bastion_chunks (session_id, seq, at, stream, data) VALUES (%s,%s,%s,%s,%s)",
            (session_id, seq, at, stream, data),
        )

    def list(self, limit=100, actor=None, device_id=None):
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        if actor:
            clauses.append("actor = %s")
            params.append(actor)
        if device_id:
            clauses.append("device_id = %s")
            params.append(device_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self.db.query(
            f"SELECT * FROM bastion_sessions {where} ORDER BY started_at DESC LIMIT %s", tuple(params)
        )

    def get(self, session_id):
        return self.db.query_one("SELECT * FROM bastion_sessions WHERE id=%s", (session_id,))

    def chunks(self, session_id, limit=20000):
        return self.db.query(
            "SELECT seq, at, stream, data FROM bastion_chunks WHERE session_id=%s ORDER BY seq LIMIT %s",
            (session_id, limit),
        )

    def close_orphans(self):
        """A session with no `ended_at` after a process restart never
        ended - it died with the process. Left alone it shows as live
        forever, so it is closed on startup with an honest reason."""
        try:
            cur = self.db.execute(
                "UPDATE bastion_sessions SET ended_at=%s, end_reason='switchboard restarted' "
                "WHERE ended_at IS NULL", (_iso(_now()),)
            )
            n = getattr(cur, "rowcount", 0) or 0
            if n:
                log.info("closed %d bastion session(s) left open by a previous process", n)
        except Exception:
            log.exception("could not close orphaned bastion sessions")


# --- line assembly (full mode) -----------------------------------------

_BACKSPACE = ("\x7f", "\x08")


class LineAssembler:
    """Reconstructs submitted commands from a raw keystroke stream.

    Only used in full mode, and only for the recording - the device gets
    the keystrokes either way. It is not a security control and must not
    be mistaken for one: read-only mode never runs a keystroke stream at
    all, which is precisely why it does not need this to be perfect.
    """

    def __init__(self):
        self.pending = ""

    def feed(self, data):
        done = []
        for ch in data:
            if ch in ("\r", "\n"):
                if self.pending.strip():
                    done.append(self.pending.strip())
                self.pending = ""
            elif ch in _BACKSPACE:
                self.pending = self.pending[:-1]
            elif ch in ("\x15", "\x03"):   # Ctrl-U / Ctrl-C both abandon the line
                self.pending = ""
            elif ch == "\t":
                pass                        # completion: the echo in `out` is the truth
            elif ord(ch) >= 32:
                self.pending += ch
            if len(self.pending) > policy.MAX_LINE * 4:
                self.pending = self.pending[-policy.MAX_LINE:]
        return done


# --- a live session ----------------------------------------------------

class BastionError(Exception):
    pass


class BastionSession:
    """One interactive SSH connection, its recording, and its limits."""

    def __init__(self, device, actor, role, mode, client_ip, store, audit=None, limits=LIMITS):
        self.id = uuid.uuid4().hex
        self.device = device
        self.device_id = device.id
        self.actor = actor
        self.role = role
        self.mode = mode
        self.platform = getattr(device, "platform", "os9") or "os9"
        self.client_ip = client_ip
        self.store = store
        self.audit = audit
        self.limits = limits

        self.started_at = _now()
        self.last_activity = time.monotonic()
        self.bytes_in = 0
        self.bytes_out = 0
        self.commands = 0
        self.refused = 0
        self.end_reason = None

        self._client = None
        self._chan = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._reader = None
        self._on_output = None
        self._on_closed = None
        self._assembler = LineAssembler()
        self.recorder = Recorder(store, self.id, limits.max_record_bytes)

    # -- lifecycle --

    @property
    def read_only(self):
        return self.mode == MODE_READONLY

    def open(self, cols=120, rows=32):
        """Dial, log in, and return whatever the device printed getting
        there so the browser can paint a real first screen."""
        self.store.start({
            "id": self.id, "device_id": self.device_id,
            "device_name": getattr(self.device, "name", self.device_id),
            "host": getattr(self.device, "host", None), "platform": self.platform,
            "actor": self.actor, "role": self.role, "mode": self.mode,
            "client_ip": self.client_ip, "started_at": _iso(self.started_at),
        })
        try:
            banner = clean_banner(self._connect(cols, rows))
        except Exception as e:
            self.end_reason = f"could not connect: {e}"
            self.store.finish(self.id, _iso(_now()), self.end_reason, 0, 0, 0, 0, False)
            raise
        self.recorder.note(
            f"-- {self.mode} session opened by {self.actor} ({self.role}) "
            f"to {getattr(self.device, 'name', self.device_id)} [{self.platform}] from {self.client_ip} --"
        )
        self.recorder.output(banner)
        self._audit("bastion.open", detail={"mode": self.mode, "platform": self.platform,
                                            "session": self.id, "client_ip": self.client_ip})
        return banner

    def _connect(self, cols, rows):
        device = self.device
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = load_private_key(device.private_key, device.passphrase) if device.private_key else None
        client.connect(
            device.host, port=getattr(device, "port", 22) or 22, username=device.username,
            password=None if pkey else device.password, pkey=pkey,
            look_for_keys=False, allow_agent=False, timeout=15, banner_timeout=15, auth_timeout=15,
        )
        chan = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        chan.settimeout(0.3)
        self._client = client
        self._chan = chan
        try:
            return self._login()
        except Exception:
            self._hard_close()
            raise

    def _login(self):
        """Get from "connected" to "a usable prompt", per platform.

        The read-only branch of the OS9 path is the important one: it
        skips `enable` entirely, so the session lands in user EXEC and
        the device's own authorisation - not just this app's policy -
        stands between the person and a configuration change.
        """
        if self.platform == "junos":
            out = self._read_until(JUNOS_SHELL_PROMPT_RE, 20)
            out += self._send_and_read("cli", PROMPT_RE, 20)
            out += self._send_and_read("set cli screen-length 0", PROMPT_RE, 10)
            return out
        if self.platform == "opnsense":
            out = self._read_until(OPNSENSE_MENU_PROMPT_RE, 20)
            out += self._send_and_read("8", OPNSENSE_SHELL_PROMPT_RE, 20)
            return out

        out = self._read_until(PROMPT_RE, 20)
        if not self.read_only:
            out += self._send_and_read("enable", PROMPT_RE, 15, expect_password=True)
            if "assword" in out[-200:]:
                out += self._send_and_read(self.device.enable_password or self.device.password, PROMPT_RE, 15)
            if not out.rstrip().endswith("#"):
                raise SwitchSSHError("could not reach privileged EXEC - check the enable password")
        out += self._send_and_read("terminal length 0", PROMPT_RE, 10)
        return out

    def _send_and_read(self, text, prompt_re, timeout, expect_password=False):
        with self._send_lock:
            self._chan.send(text + "\n")
        return self._read_until(prompt_re, timeout, expect_password)

    def _read_until(self, prompt_re, timeout, expect_password=False):
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._chan.recv(65536).decode("utf-8", errors="replace")
            except Exception:
                chunk = ""
            if chunk:
                buf += chunk
                tail = buf[-400:]
                if prompt_re.search(tail) or (expect_password and "assword" in tail):
                    return buf
            else:
                time.sleep(0.05)
        return buf

    def attach(self, on_output, on_closed):
        """Wire the session to a transport and start pumping. `on_output`
        is called from the reader thread, so it must be cheap and
        thread-safe - the WebSocket handler hands it straight to a queue."""
        self._on_output = on_output
        self._on_closed = on_closed
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"bastion-{self.id[:8]}")
        self._reader.start()

    def _read_loop(self):
        while not self._closed.is_set():
            try:
                data = self._chan.recv(65536)
            except Exception:
                data = b""
            if data:
                text = data.decode("utf-8", errors="replace")
                self.bytes_out += len(data)
                self.recorder.output(text)
                if self._on_output:
                    try:
                        self._on_output(text)
                    except Exception:
                        log.exception("bastion %s could not deliver output", self.id)
            else:
                self.recorder.tick()
                if self._chan is None or self._chan.closed or self._chan.exit_status_ready():
                    self._finish("device disconnected")
                    return
                reason = self._expired()
                if reason:
                    self._finish(reason)
                    return
                time.sleep(0.02)

    def _expired(self):
        idle = self.limits.idle_seconds
        if idle > 0 and (time.monotonic() - self.last_activity) > idle:
            return f"idle for more than {idle // 60} minutes"
        cap = self.limits.max_seconds
        if cap > 0 and (_now() - self.started_at).total_seconds() > cap:
            return f"reached the {cap // 3600}-hour session limit"
        return None

    # -- input --

    def submit_line(self, text):
        """The read-only path. Returns (sent, message): `sent` says
        whether the device saw anything at all, `message` is what to put
        on the person's terminal when it did not."""
        self.last_activity = time.monotonic()
        text = (text or "").rstrip("\r\n")
        reason = policy.check_line(text, self.platform) if self.read_only else None
        if reason:
            self.refused += 1
            self.recorder.note(f"REFUSED: {text}\n  {reason}")
            self._audit("bastion.refused", target=text[:200],
                        detail={"session": self.id, "reason": reason, "mode": self.mode})
            return False, reason
        if text.strip():
            self.commands += 1
            self.recorder.line(text)
            self._audit("bastion.command", target=text[:400],
                        detail={"session": self.id, "mode": self.mode})
        # Ctrl-U first: whatever the device had half-typed is discarded,
        # so what runs is exactly what was checked.
        self._write(KILL_LINE + text + "\r")
        return True, None

    def send_raw(self, data):
        """The full-mode path: keystrokes straight through. Lines are
        reassembled only to keep the transcript readable."""
        if self.read_only:
            raise BastionError("a read-only session does not accept raw keystrokes")
        self.last_activity = time.monotonic()
        for line in self._assembler.feed(data):
            self.commands += 1
            self.recorder.line(line)
            self._audit("bastion.command", target=line[:400], detail={"session": self.id, "mode": self.mode})
        self._write(data)

    def send_key(self, name):
        """A named control key. The allowlist lives in bastion_policy so
        that what a read-only session may send is described in exactly one
        place."""
        seq = policy.key_bytes(name)
        if seq is None:
            raise BastionError(f"{name!r} is not a key this session may send")
        self.last_activity = time.monotonic()
        self._write(seq)

    def help_request(self, text):
        """Dell OS9 and Junos both answer `?` with context help. It is
        genuinely useful and completely safe *provided no newline follows
        it*, so this sends the partial line and a `?`, then wipes the
        device's input buffer again. Nothing is ever submitted.
        """
        text = (text or "").rstrip("\r\n")
        if self.platform in policy.SHELL_PLATFORMS:
            raise BastionError("there is no context help in a shell")
        if len(text) > policy.MAX_LINE:
            raise BastionError("line too long")
        if any(ord(c) < 32 for c in text):
            raise BastionError("control characters are not accepted")
        self.last_activity = time.monotonic()
        self._write(KILL_LINE + text + "?")
        threading.Timer(1.2, lambda: self._write_quietly(KILL_LINE)).start()

    def resize(self, cols, rows):
        try:
            cols = max(20, min(int(cols), 500))
            rows = max(5, min(int(rows), 200))
        except (TypeError, ValueError):
            return
        try:
            self._chan.resize_pty(width=cols, height=rows)
        except Exception:
            pass

    def _write(self, data):
        if self._closed.is_set() or self._chan is None:
            raise BastionError("this session has ended")
        with self._send_lock:
            self._chan.send(data)
        self.bytes_in += len(data)

    def _write_quietly(self, data):
        try:
            self._write(data)
        except Exception:
            pass

    # -- teardown --

    def close(self, reason="closed"):
        self._finish(reason)

    def _finish(self, reason):
        if self._closed.is_set():
            return
        self._closed.set()
        self.end_reason = reason
        try:
            self.recorder.note(f"-- session ended: {reason} --")
            self.recorder.flush()
        except Exception:
            log.exception("could not finish bastion recording %s", self.id)
        self.store.finish(self.id, _iso(_now()), reason, self.bytes_in, self.bytes_out,
                          self.commands, self.refused, self.recorder.truncated)
        self._audit("bastion.close", detail={"session": self.id, "reason": reason,
                                             "commands": self.commands, "refused": self.refused,
                                             "seconds": int((_now() - self.started_at).total_seconds())})
        self._hard_close()
        if self._on_closed:
            try:
                self._on_closed(reason)
            except Exception:
                log.exception("bastion %s could not signal close", self.id)

    def _hard_close(self):
        for obj in (self._chan, self._client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._chan = None
        self._client = None

    @property
    def closed(self):
        return self._closed.is_set()

    def _audit(self, action, target=None, detail=None):
        if self.audit is None:
            return
        try:
            self.audit.record(self.actor, action, target or self.device_id, detail)
        except Exception:
            log.exception("could not audit %s", action)

    def describe(self):
        return {
            "id": self.id, "device_id": self.device_id,
            "device_name": getattr(self.device, "name", self.device_id),
            "platform": self.platform, "actor": self.actor, "role": self.role, "mode": self.mode,
            "client_ip": self.client_ip, "started_at": _iso(self.started_at),
            "idle_seconds": int(time.monotonic() - self.last_activity),
            "commands": self.commands, "refused": self.refused,
            "bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
        }


# --- the registry ------------------------------------------------------

class BastionManager:
    """Tracks live sessions and enforces the caps.

    The per-device cap is not a policy preference, it is the vty limit:
    Dell OS9 has a handful of concurrent SSH slots and the app's own
    pooled session already holds one. Filling them with terminals would
    take the status poller and the Console down with it.
    """

    def __init__(self, store, audit=None, limits=LIMITS):
        self.store = store
        self.audit = audit
        self.limits = limits
        self._lock = threading.Lock()
        self._sessions = {}

    def open(self, device, actor, role, mode, client_ip, cols=120, rows=32):
        with self._lock:
            self._reap_locked()
            live = list(self._sessions.values())
            if len(live) >= self.limits.total:
                raise BastionError(
                    f"{len(live)} console sessions are already open across the fleet "
                    f"(the limit is {self.limits.total}) - close one and try again")
            on_device = [s for s in live if s.device_id == device.id]
            if len(on_device) >= self.limits.per_device:
                who = ", ".join(sorted({s.actor for s in on_device}))
                raise BastionError(
                    f"{getattr(device, 'name', device.id)} already has {len(on_device)} console "
                    f"session(s) open ({who}) and only allows {self.limits.per_device} - "
                    "switches have very few SSH slots")
            session = BastionSession(device, actor, role, mode, client_ip, self.store,
                                     audit=self.audit, limits=self.limits)
            self._sessions[session.id] = session
        try:
            banner = session.open(cols, rows)
        except Exception:
            with self._lock:
                self._sessions.pop(session.id, None)
            raise
        return session, banner

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def release(self, session_id):
        with self._lock:
            self._sessions.pop(session_id, None)

    def live(self):
        with self._lock:
            self._reap_locked()
            return [s.describe() for s in self._sessions.values()]

    def kill(self, session_id, by):
        session = self.get(session_id)
        if session is None:
            return False
        session.close(f"ended by {by}")
        self.release(session_id)
        return True

    def _reap_locked(self):
        for sid, s in list(self._sessions.items()):
            if s.closed:
                self._sessions.pop(sid, None)
