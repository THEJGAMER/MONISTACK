"""Tests for vector.yaml's `interpret_switch_event` VRL transform
(ROADMAP.md 0.2) - this is how the alarm-severity/category/link-state
logic was validated by hand against real captured syslog lines during
development (see vector.yaml's own comments for the exact "cleared" vs
"major alarm" ordering bug and the CHMGR PS_UP/PS_DOWN severity-digit
quirk this transform works around); now automated instead.

Runs the real VRL source extracted straight out of vector.yaml through
the actual `vector vrl` CLI (confirmed live: `vector vrl -p <program> -i
<events.jsonl> -q -o` prints one JSON result object per line to stdout,
with vector's own startup log on stderr, not mixed into the output) rather
than re-implementing the transform's logic in Python - a second
hand-written implementation could drift from vector.yaml and still pass.
Skipped if `vector` isn't installed locally (see CI, which installs it via
https://sh.vector.dev before running this file).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

VECTOR_YAML = Path(__file__).parent.parent / "vector.yaml"

pytestmark = pytest.mark.skipif(shutil.which("vector") is None, reason="vector binary not installed")

# (name, raw syslog `.message` text, expected fields in the transform's
# output) - messages and expected fields are exactly what was captured/
# verified live during development (see vector.yaml comments), not
# invented for this test.
TEST_CASES = [
    (
        "major_alarm",
        "%CHMGR-0-PS_DOWN: Major alarm: Power supply 2 in unit 1 is down",
        {
            "facility": "CHMGR", "mnemonic": "PS_DOWN", "event_category": "hardware",
            "alarm_severity": "critical", "alarm_active": True,
            "alarm_component": "Power supply 2 in unit 1",
        },
    ),
    (
        "alarm_recovery_not_a_new_alarm",
        "%CHMGR-0-PS_UP: Power supply 2 in unit 1 is up",
        {
            "event_category": "hardware", "alarm_severity": None, "alarm_active": False,
            "alarm_component": "Power supply 2 in unit 1",
        },
    ),
    (
        # Real captured message (via the webui's live syslog view during
        # an actual PSU-2-induced port flap on Te 1/47) - the original
        # version of this test used a fabricated "changed state to down"
        # (missing "interface") that happened to match the old, buggy
        # regex, which masked that regex never matching the real message
        # format ("changed *interface* state to down") for months.
        "interface_link_down",
        "%IFMGR-5-OSTATE_DN: Changed interface state to down: Te 1/47",
        {
            "facility": "IFMGR", "event_category": "interface", "interface": "Te 1/47",
            "link_state": "down", "alarm_severity": None, "alarm_active": None,
        },
    ),
    (
        # The exact regression this ordering check exists for (see
        # vector.yaml): Dell's own recovery message contains the literal
        # substring "major alarm", so "cleared" must be checked first or
        # this misreads as a brand-new active critical alarm.
        "major_alarm_cleared_is_a_recovery_not_a_new_alarm",
        "%CHMGR-0-PS_DOWN: Major alarm cleared: Power supply 2 in unit 1",
        {
            "alarm_severity": None, "alarm_active": False,
            "alarm_component": "Power supply 2 in unit 1",
        },
    ),
    (
        "unrecognized_facility_falls_back_to_other",
        "%FOO-6-BAR: some unrelated message",
        {"event_category": "other", "alarm_severity": None, "alarm_active": None},
    ),
]


@pytest.fixture(scope="module")
def transform_results(tmp_path_factory):
    doc = yaml.safe_load(VECTOR_YAML.read_text())
    source = doc["transforms"]["interpret_switch_event"]["source"]

    tmp = tmp_path_factory.mktemp("vrl")
    program_file = tmp / "interpret.vrl"
    program_file.write_text(source)
    events_file = tmp / "events.jsonl"
    events_file.write_text("\n".join(json.dumps({"message": msg}) for _, msg, _ in TEST_CASES))

    result = subprocess.run(
        ["vector", "vrl", "-p", str(program_file), "-i", str(events_file), "-q", "-o"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == len(TEST_CASES), f"expected {len(TEST_CASES)} results, got {len(lines)}: {result.stdout!r}"
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("i,name,message,expected", [(i, *tc) for i, tc in enumerate(TEST_CASES)])
def test_interpret_switch_event(transform_results, i, name, message, expected):
    actual = transform_results[i]
    for key, value in expected.items():
        assert actual.get(key) == value, f"{name}: expected {key}={value!r}, got {actual.get(key)!r} (full: {actual})"
