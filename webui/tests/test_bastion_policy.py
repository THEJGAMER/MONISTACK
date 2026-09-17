"""What a read-only bastion session may send to a device.

This is the file to read first if you want to know whether the bastion is
safe. Everything here is about one question - given a line somebody typed,
does it reach the switch - and the cases are chosen to be the ways that
question has a surprising answer: abbreviations, pipes that write, a
second word that changes the verb's meaning, and a shell where the first
word tells you nothing at all.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import bastion_policy as policy  # noqa: E402


def allowed(line, platform="os9"):
    return policy.check_line(line, platform) is None


# --- who gets what ------------------------------------------------------

def test_an_admin_chooses_and_an_operator_does_not():
    assert policy.modes_for_role("admin") == ["full", "readonly"]
    assert policy.modes_for_role("operator") == ["readonly"]
    assert policy.modes_for_role("viewer") == []


def test_asking_for_more_than_your_role_gets_you_less_not_an_error():
    """A request is a ceiling, never an escalation - an operator asking
    for full access lands in read-only rather than being refused, because
    the role is the answer and the request is only a preference."""
    assert policy.resolve_mode("operator", "full") == "readonly"
    assert policy.resolve_mode("admin", "full") == "full"
    assert policy.resolve_mode("admin", "readonly") == "readonly"


def test_an_unknown_or_missing_role_gets_nothing():
    assert policy.resolve_mode("viewer", "readonly") is None
    assert policy.resolve_mode(None, "full") is None
    assert policy.resolve_mode("", "readonly") is None


def test_a_role_that_asks_for_nonsense_gets_the_weakest_mode_it_may_have():
    assert policy.resolve_mode("admin", "godmode") == "readonly"
    assert policy.resolve_mode("admin", None) == "readonly"


# --- the ordinary cases -------------------------------------------------

@pytest.mark.parametrize("line", [
    "show version",
    "show interfaces status",
    "show running-config",
    "show ip route",
    "ping 192.168.0.1",
    "traceroute 8.8.8.8",
    "dir flash:",
    "",                       # a bare Enter just redraws the prompt
    "   ",
])
def test_reading_the_device_is_allowed(line):
    assert allowed(line)


@pytest.mark.parametrize("line", [
    "configure terminal",
    "reload",
    "write memory",
    "copy running-config startup-config",
    "clear counters",
    "interface TenGigabitEthernet 1/41",
    "shutdown",
    "delete flash://startup-config",
    "no shutdown",
    "erase startup-config",
])
def test_changing_the_device_is_not(line):
    assert not allowed(line)


# --- abbreviations ------------------------------------------------------
# Every network CLI takes prefixes, so a check that only understood full
# words would be refused by reality within about ten seconds of real use.

@pytest.mark.parametrize("line", ["sh ver", "sho int", "show ru", "trace 1.1.1.1"])
def test_the_abbreviations_people_actually_type_work(line):
    assert allowed(line)


@pytest.mark.parametrize("line", ["s", "c", "sh_ow version", "shows version"])
def test_an_abbreviation_too_short_or_too_long_to_be_show_is_refused(line):
    """One character is never enough: on OS9 `s` is as much the start of
    `ssh` or `start` as of `show`. And a *longer* word is not an
    abbreviation at all - `shows` is not `show`."""
    assert not allowed(line)


def test_config_abbreviations_do_not_sneak_through():
    for line in ["conf", "conf t", "co", "config"]:
        assert not allowed(line), line


# --- pipes --------------------------------------------------------------
# The one way a `show` command writes to storage.

@pytest.mark.parametrize("line", [
    "show running-config | grep hostname",
    "show int | except down",
    "show log | find ERROR",
    "show version | no-more",
    'show int | match "Te 1/4[12]"',      # a pipe inside quotes is not a stage
])
def test_filters_are_fine(line):
    assert allowed(line)


@pytest.mark.parametrize("line", [
    "show running-config | save flash://stolen.txt",
    "show version | tee /tmp/x",
    "show config | append /var/tmp/x",
    "show version | request system reboot",
    "show version | sh",
])
def test_a_pipe_that_writes_is_refused(line):
    reason = policy.check_line(line, "junos")
    assert reason and "pipe" in reason


def test_a_line_may_not_start_with_a_pipe():
    assert not allowed("| grep x")


# --- second words -------------------------------------------------------

def test_terminal_length_is_cosmetic_and_terminal_monitor_is_not():
    """`terminal monitor` turns the session into a firehose of the
    device's own logging, which is a denial of service against whoever is
    using it."""
    assert allowed("terminal length 0")
    assert allowed("terminal width 0")
    assert not allowed("terminal monitor")
    assert not allowed("terminal")


def test_junos_file_reads_but_does_not_delete():
    assert policy.check_line("file list /var/log", "junos") is None
    assert policy.check_line("file show /var/log/messages", "junos") is None
    assert policy.check_line("file delete /var/log/messages", "junos") is not None
    assert policy.check_line("file copy a b", "junos") is not None


def test_junos_monitor_stops_short_of_packet_capture():
    """`monitor traffic` is tcpdump, and tcpdump takes `write-file`."""
    assert policy.check_line("monitor interface traffic", "junos") is None
    assert policy.check_line("monitor traffic interface ge-0/0/0", "junos") is not None


# --- shells -------------------------------------------------------------
# A shell is the case where a verb allowlist is not enough, because the
# first word of `ifconfig; rm -rf /` is `ifconfig`.

@pytest.mark.parametrize("line", [
    "ifconfig",
    "ifconfig -a",
    "ifconfig igb0",
    "netstat -rn",
    "arp -an",
    "pfctl -s rules",
    "pfctl -si",
    "sysctl hw.model",
    "uptime",
    "ps auxw",
    "df -h",
    "ping -c 3 192.168.0.1",
])
def test_reading_a_firewall_is_allowed(line):
    assert allowed(line, "opnsense")


@pytest.mark.parametrize("line", [
    "ifconfig; rm -rf /",
    "ifconfig && reboot",
    "ifconfig | sh",
    "echo x > /etc/rc.conf",
    "cat /conf/config.xml",
    "ifconfig `reboot`",
    "ifconfig $(reboot)",
    "pfctl -d",
    "pfctl -F all",
    "pfctl -k 10.0.0.1",
    "sysctl net.inet.ip.forwarding=0",
    "reboot",
    "shutdown -r now",
    "rm /var/log/system.log",
    "ping -f 192.168.0.1",       # a flood ping, from a shell running as root
])
def test_a_shell_is_treated_as_a_shell(line):
    assert not allowed(line, "opnsense")


def test_the_refusal_says_which_character_was_the_problem():
    """The reason lands on the person's terminal, so it has to be worth
    reading."""
    reason = policy.check_line("ifconfig; reboot", "opnsense")
    assert "';'" in reason


# --- the edges ----------------------------------------------------------

def test_an_unknown_platform_is_refused_rather_than_guessed():
    assert policy.check_line("show version", "cisco-ios-xr") is not None


def test_a_very_long_line_is_refused():
    assert not allowed("show " + "x" * policy.MAX_LINE)


def test_control_characters_never_reach_a_command_line():
    """A newline in the middle of a checked line would be a second,
    unchecked command."""
    assert not allowed("show version\rconfigure terminal")
    assert not allowed("show version\nreload")


def test_case_does_not_matter():
    assert allowed("SHOW VERSION")
    assert not allowed("CONFIGURE TERMINAL")


def test_only_the_named_control_keys_can_be_sent():
    assert policy.key_bytes("ctrl-c") == "\x03"
    assert policy.key_bytes("ctrl-d") is None, "Ctrl-D would submit / log out unchecked"
    assert policy.key_bytes("enter") is None, "the only way to submit is a checked line"
    assert policy.key_bytes(None) is None
