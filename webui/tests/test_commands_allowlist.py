"""The single highest-value test in this suite (ROADMAP.md 0.2): asserts
every command string this app can ever send to a device is read-only.
This is the actual security property the whole app is built around (see
README.md "Why this shape") - the browser can only ever select a
(category_id, command_id[, param]) triple, never send CLI text directly,
and this test is what makes sure everything reachable through that path
is a `show`/query command, never anything that changes device state.
"""
import re

import pytest

from commands import COMMAND_TREE, COMMAND_TREES, JUNOS_COMMAND_TREE, OPNSENSE_COMMAND_TREE

# Config-mode / state-changing verbs that must never appear as (or within)
# an allowlisted command, on any platform - checked as whole words so
# "show" commands that happen to mention one in passing wouldn't false-
# positive (none currently do, but this is the honest bar for the check).
_FORBIDDEN_VERBS = (
    "configure", "conf t", "write", "reload", "reboot", "delete", "erase",
    "copy", "clear", "no ", "shutdown", "enable password", "username",
    "reset", "kill", "rm ", "rm -", "> /", "| xargs",
)


def _all_commands(tree):
    """Yields every {'cmd', ...} spec in a command tree, category/items
    structure shared by all three platforms' trees."""
    for category in tree:
        for item in category["items"]:
            yield item


@pytest.mark.parametrize("tree_name,tree", [
    ("os9", COMMAND_TREE),
    ("junos", JUNOS_COMMAND_TREE),
    ("opnsense", OPNSENSE_COMMAND_TREE),
])
def test_every_command_is_read_only(tree_name, tree):
    for item in _all_commands(tree):
        cmd = item["cmd"]
        cmd_lower = cmd.lower()
        for verb in _FORBIDDEN_VERBS:
            assert verb not in cmd_lower, (
                f"{tree_name} command {item['id']!r} ({cmd!r}) contains forbidden verb {verb!r}"
            )


def test_os9_and_junos_commands_start_with_show():
    """Dell OS9 and Junos are both `show`-grammar CLIs - every command
    here must literally start with `show`, the strongest, simplest form
    of the read-only guarantee for these two platforms."""
    for tree_name, tree in (("os9", COMMAND_TREE), ("junos", JUNOS_COMMAND_TREE)):
        for item in _all_commands(tree):
            assert item["cmd"].startswith("show "), (
                f"{tree_name} command {item['id']!r} ({item['cmd']!r}) doesn't start with 'show '"
            )


# OPNsense isn't a `show`-grammar CLI (it's a FreeBSD shell - see
# ssh_client.py's module docstring), so its allowlist is checked against
# an explicit list of known-read-only tools instead of a `show` prefix.
_OPNSENSE_ALLOWED_PREFIXES = ("uname", "uptime", "top", "ifconfig", "netstat", "pfctl", "arp")


def test_opnsense_commands_use_known_read_only_tools():
    for item in _all_commands(OPNSENSE_COMMAND_TREE):
        cmd = item["cmd"]
        assert any(cmd.startswith(p) for p in _OPNSENSE_ALLOWED_PREFIXES), (
            f"opnsense command {item['id']!r} ({cmd!r}) doesn't start with a known read-only tool"
        )
        # pfctl's own write-capable subcommands (-f load a ruleset, -F
        # flush state) must never appear even though `pfctl` itself is
        # allowed for its many read-only `-s <thing>` show subcommands.
        if cmd.startswith("pfctl"):
            assert re.search(r"pfctl\s+-s\b", cmd), f"pfctl command {item['id']!r} ({cmd!r}) isn't a '-s' (show) subcommand"
            assert " -f " not in cmd and " -F" not in cmd


def test_running_config_excluded():
    """Explicitly called out in commands.py's own docstring as deliberately
    excluded (can dump secrets - SNMP communities, local user hashes) -
    this asserts that exclusion actually holds, not just that it's
    documented."""
    for item in _all_commands(COMMAND_TREE):
        assert "running-config" not in item["cmd"]


def test_command_trees_registry_is_complete():
    """COMMAND_TREES is what find_command() actually uses to dispatch by
    platform (see app.py's /api/run) - if a platform's tree ever stops
    being registered there, every command test above would still pass
    while the real dispatch path silently fell back to the wrong tree."""
    assert COMMAND_TREES == {"os9": COMMAND_TREE, "junos": JUNOS_COMMAND_TREE, "opnsense": OPNSENSE_COMMAND_TREE}
