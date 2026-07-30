"""Persistent interactive-shell SSH client for the Dell S4048 (Dell OS9 / FTOS).

The switch does not reliably support paramiko's exec_command for `show`
commands - it needs an interactive shell (vty) session with paging disabled,
same as a human at a terminal. This client logs in, escalates to privileged
EXEC (`enable`), disables paging, and then lets the caller send arbitrary
`show` commands and read the response back up to the next prompt.
"""
import logging
import re
import socket
import time

import paramiko

log = logging.getLogger("ssh_client")

PROMPT_RE = re.compile(r"[\r\n]?\S+[>#]\s*$")


class SwitchSSHError(Exception):
    pass


class SwitchSSH:
    def __init__(self, host, username, password, port=22, enable_password=None, timeout=10):
        self.host = host
        self.username = username
        self.password = password
        self.enable_password = enable_password or password
        self.port = port
        self.timeout = timeout
        self._client = None
        self._chan = None

    def connected(self):
        return self._chan is not None and not self._chan.closed

    def connect(self):
        self.close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
        )
        chan = client.invoke_shell()
        chan.settimeout(self.timeout)
        self._client = client
        self._chan = chan
        self._read_until_prompt()  # initial ">" prompt

        self._raw_send("enable")
        resp = self._read_until_prompt(expect_password=True)
        if "assword" in resp:
            self._raw_send(self.enable_password)
            resp = self._read_until_prompt()
        if not resp.rstrip().endswith("#"):
            raise SwitchSSHError(f"failed to reach privileged EXEC mode, got: {resp!r}")

        self._raw_send("terminal length 0")
        self._read_until_prompt()
        log.info("connected and escalated to privileged EXEC on %s", self.host)

    def close(self):
        if self._chan is not None:
            try:
                self._chan.close()
            except Exception:
                pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._chan = None
        self._client = None

    def _raw_send(self, line):
        self._chan.send(line + "\n")

    def _read_until_prompt(self, expect_password=False, overall_timeout=15):
        buf = ""
        deadline = time.time() + overall_timeout
        while time.time() < deadline:
            try:
                if self._chan.recv_ready():
                    chunk = self._chan.recv(65536).decode("utf-8", errors="replace")
                    buf += chunk
                    if expect_password and buf.rstrip().endswith(":"):
                        return buf
                    if PROMPT_RE.search(buf):
                        # give it a brief moment in case more is trickling in
                        time.sleep(0.05)
                        if self._chan.recv_ready():
                            continue
                        return buf
                else:
                    time.sleep(0.05)
            except socket.timeout:
                break
        return buf

    def run(self, command, timeout=20):
        """Send a show command and return its output with the echoed
        command and trailing prompt stripped."""
        if not self.connected():
            self.connect()
        try:
            self._raw_send(command)
            raw = self._read_until_prompt(overall_timeout=timeout)
        except (socket.timeout, EOFError, paramiko.SSHException, OSError) as e:
            self.close()
            raise SwitchSSHError(f"error running {command!r}: {e}") from e

        lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[0].strip() == command.strip():
            lines = lines[1:]
        if lines and PROMPT_RE.match(lines[-1].strip() + " "):
            lines = lines[:-1]
        return "\n".join(lines).strip("\n")
