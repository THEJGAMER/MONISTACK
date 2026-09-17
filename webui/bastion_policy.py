"""What a read-only bastion session is allowed to send to a device.

This is the security core of the console bastion and it is deliberately
its own module: everything here is a pure function of (line, platform),
so it can be tested exhaustively without a switch, a socket or a
database anywhere near it.

**Allowlist, never blocklist.** A line is refused unless its first word
is a verb this file names as read-only for that platform. That is the
only shape that fails safe: a blocklist of dangerous words is wrong the
moment a platform gains a command nobody here had heard of, and network
operating systems gain those constantly. Anything unrecognised is
refused, which is occasionally annoying and never dangerous.

Three things make a line dangerous even when it starts with `show`:

1. **Pipes with side effects.** Both Dell OS9 and Junos will happily
   write the output of a `show` somewhere: `| save flash://...` on OS9,
   `| save`, `| tee`, `| append`, `| request` on Junos. Each pipe stage
   is checked against its own allowlist of pure filters.
2. **Abbreviations.** Every network CLI accepts prefixes - `sh ver` is
   `show version` - so the check has to accept them too or it is useless
   in practice. Each verb therefore carries a minimum length, chosen so
   the abbreviation cannot also be a prefix of something that writes:
   `sh` only ever means `show`, but a bare `s` could be `show`, `ssh`,
   `start` or `snmp-server`, so one character is never enough.
3. **A second word that changes everything.** `terminal length` is
   harmless, `terminal monitor` floods the session with the device's own
   logging; `file show` reads, `file delete` does not. Those verbs carry
   an explicit set of permitted second words.

Shell platforms (OPNsense lands in a real FreeBSD shell) get no verb
allowlist at all, because in a shell the first word is not the whole
story - `ifconfig; rm -rf /` starts with `ifconfig`. They are matched
against whole-command patterns instead, with every shell metacharacter
refused outright.

The second half of the defence lives outside this file: a read-only
session on Dell OS9 never sends `enable`, so it sits in user EXEC and
the *device* refuses configuration too. This module is what stops the
session from getting there; the device is what stops it if this module
is wrong.
"""
import re

MODE_FULL = "full"
MODE_READONLY = "readonly"
MODES = (MODE_FULL, MODE_READONLY)

# Which modes each role may open. A viewer gets no bastion session at
# all - the bastion is free text to a live device, which is categorically
# not a read-only-user capability even in read-only mode (it can still
# ping, flood the vty slots, and read anything the device will print).
_MODES_BY_ROLE = {
    "admin": (MODE_FULL, MODE_READONLY),
    "operator": (MODE_READONLY,),
    "viewer": (),
}


def modes_for_role(role):
    """The modes this role may choose between, strongest first."""
    return list(_MODES_BY_ROLE.get((role or "").lower(), ()))


def resolve_mode(role, requested):
    """What mode this person actually gets. Returns None if they may not
    open a session at all.

    A request is a *ceiling request*, never an escalation: an admin who
    asks for read-only gets read-only, and an operator who asks for full
    gets read-only rather than an error, because the role is the answer
    and the request is only a preference.
    """
    allowed = modes_for_role(role)
    if not allowed:
        return None
    if requested in allowed:
        return requested
    # Asked for something they cannot have (or asked for nothing): give
    # them the weakest mode their role allows, not the strongest.
    return allowed[-1]


# --- the verb tables ---------------------------------------------------
#
# (canonical, min_prefix_len, allowed_second_words or None)

_OS9_VERBS = [
    ("show", 2, None),
    ("ping", 4, None),
    ("traceroute", 5, None),
    ("dir", 3, None),
    # `terminal monitor` turns the session into a firehose of the
    # device's own logging, which is a denial of service against the
    # person using it; length/width are just cosmetics.
    ("terminal", 4, {"length", "width"}),
    ("exit", 4, None),
    ("quit", 4, None),
    ("logout", 6, None),
]

_JUNOS_VERBS = [
    ("show", 2, None),
    ("ping", 4, None),
    ("traceroute", 5, None),
    # `monitor traffic` is tcpdump, and tcpdump takes `write-file`.
    # `monitor start` writes a log follow into the session and needs
    # `monitor stop` to undo, so allow the stop too.
    ("monitor", 4, {"interface", "list", "stop"}),
    ("file", 4, {"list", "show", "compare", "checksum"}),
    ("help", 4, None),
    ("exit", 4, None),
    ("quit", 4, None),
]

VERBS = {"os9": _OS9_VERBS, "junos": _JUNOS_VERBS}

# Pipe stages that only ever filter or reformat what is already on the
# screen. Everything absent from this list - `save`, `tee`, `append`,
# `request`, a shell name - is refused, which is the point: this is the
# one place a `show` command can write to storage.
_PIPE_FILTERS = {
    "grep", "match", "except", "find", "count", "display", "last", "trim",
    "no-more", "nomore", "more", "include", "exclude", "begin", "section",
    "until", "resolve", "refresh", "hold", "first", "wide",
}

# A real shell: the first word tells you nothing, so match whole lines.
# Each pattern is anchored and must describe a command that reads.
_SHELL_PATTERNS = [
    r"ifconfig(\s+-[a-zA-Z]+)?(\s+[A-Za-z0-9_.]+)?",
    r"netstat(\s+-[a-zA-Z]+)*(\s+-[a-zA-Z]+\s+[A-Za-z0-9_.]+)?",
    r"arp\s+-[an]+",
    r"route\s+-n?\s*show\s+[A-Za-z0-9_.:]+",
    r"ps(\s+-?[auxwj]+)?",
    r"sockstat(\s+-[46lcu]+)?",
    r"uptime|date|whoami|id|hostname|uname(\s+-[a-zA-Z]+)?",
    r"df(\s+-[hikm]+)?",
    r"swapinfo(\s+-[hkm]+)?",
    r"vmstat(\s+-[a-zA-Z]+)?",
    r"dmesg(\s+-a)?",
    # Only the reporting side of pfctl. -d disables the firewall, -F
    # flushes rules, -k kills states, -T modifies tables; none of those
    # can match because the flag set is spelled out.
    r"pfctl\s+-s\s*[a-zA-Z]+",
    r"pfctl\s+-s[a-zA-Z]+",
    r"pfctl\s+-vs[a-zA-Z]+",
    # A read is `sysctl name` or `sysctl -a`; a write is `sysctl name=v`,
    # which cannot match because `=` is not in the character class.
    r"sysctl(\s+-[aden]+)?(\s+[A-Za-z0-9_.]+)?",
    # Flags spelled out rather than a general -[a-z]+, because `ping -f`
    # is a flood ping and this shell runs as root: count, size, TTL and
    # timeout are the ones anybody diagnosing a link actually needs.
    r"ping6?(\s+-[cstW]\s*\d+)*\s+[A-Za-z0-9_.:-]+",
    r"traceroute6?(\s+-[a-zA-Z]+\s*\d*)*\s+[A-Za-z0-9_.:-]+",
    r"exit|logout",
]
_SHELL_RE = re.compile(r"\A(?:%s)\Z" % "|".join(f"(?:{p})" for p in _SHELL_PATTERNS))

SHELL_PLATFORMS = {"opnsense"}

# Anything that could chain, redirect, substitute or glob. Checked on
# shell platforms before the pattern match, so a refusal says *why*
# rather than just "not recognised".
_SHELL_METACHARS = set(";&|<>`$(){}[]\\!*?~'\"\n\r")

MAX_LINE = 512


def _split_pipeline(line):
    """Split on `|`, but not on a `|` inside quotes - `show | match
    "a|b"` is one filter stage with a regex in it, not two stages."""
    stages, current, quote = [], [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            current.append(ch)
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch == "|":
            stages.append("".join(current))
            current = []
        else:
            current.append(ch)
    stages.append("".join(current))
    return [s.strip() for s in stages]


def _match_verb(token, table):
    """The verb this token abbreviates, or None. Case-insensitive, and a
    token longer than the canonical form never matches - `shows` is not
    `show`."""
    t = token.lower()
    for canonical, min_len, seconds in table:
        if len(t) >= min_len and canonical.startswith(t):
            return canonical, seconds
    return None, None


def _check_shell(line):
    bad = sorted(set(line) & _SHELL_METACHARS)
    if bad:
        return (
            "this is a shell, so %s could chain or redirect a command - "
            "shell metacharacters are not accepted in a read-only session"
            % ", ".join(repr(c) for c in bad)
        )
    if not _SHELL_RE.match(line):
        return (
            "not one of the read-only shell commands this session allows "
            "(ifconfig, netstat, arp, route show, ps, sockstat, pfctl -s..., "
            "sysctl, df, dmesg, ping, traceroute, uptime)"
        )
    return None


def check_line(line, platform="os9"):
    """None if a read-only session may send this line, else the reason it
    may not - phrased for the person who typed it, since that string is
    what lands on their terminal."""
    text = (line or "").strip()
    if not text:
        return None  # a bare Enter: the device just redraws its prompt
    if len(text) > MAX_LINE:
        return f"line is longer than {MAX_LINE} characters"
    if any(ord(c) < 32 for c in text):
        return "control characters are not accepted in a command line"

    if platform in SHELL_PLATFORMS:
        return _check_shell(text)

    table = VERBS.get(platform)
    if table is None:
        return f"read-only sessions are not supported on {platform!r} devices"

    stages = _split_pipeline(text)
    head, filters = stages[0], stages[1:]
    if not head:
        return "a command cannot start with a pipe"

    tokens = head.split()
    canonical, seconds = _match_verb(tokens[0], table)
    if canonical is None:
        return (
            f"{tokens[0]!r} is not a read-only command - this session may only send "
            + ", ".join(sorted(v for v, _, _ in table))
        )
    if seconds is not None:
        if len(tokens) < 2:
            return f"{canonical} needs one of: " + ", ".join(sorted(seconds))
        nxt = tokens[1].lower()
        if not any(s.startswith(nxt) for s in seconds):
            return (
                f"{canonical} {tokens[1]} is not read-only - allowed here: "
                + ", ".join(sorted(seconds))
            )

    for stage in filters:
        if not stage:
            return "empty pipe stage"
        name = stage.split()[0].lower()
        if name not in _PIPE_FILTERS:
            return (
                f"{name!r} after a pipe can write to the device - only filters are "
                "allowed here (" + ", ".join(sorted(_PIPE_FILTERS)) + ")"
            )
    return None


# Control keys a read-only session may send as themselves. Everything a
# person needs to get out of a stuck command, and nothing that can submit
# text: the line itself always arrives as a checked line, never as
# keystrokes, so there is no way to smuggle a command in one character at
# a time.
KEYS = {
    "ctrl-c": "\x03",   # abort whatever is running
    "ctrl-q": "\x11",   # release an accidental Ctrl-S (XOFF)
}


def key_bytes(name):
    return KEYS.get((name or "").lower())
