"""The catalogue: every kind has a default; a site overrides severity and
thresholds per kind, and link-down severity per port; `ignore` is a
choice. Real Postgres via the test_eventstore fixtures.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import event_catalog as cat  # noqa: E402
from test_eventstore import db  # noqa: E402,F401


def test_every_kind_has_a_group_a_default_and_a_resolution_story():
    kinds = [c["kind"] for c in cat.CATALOG]
    assert len(kinds) == len(set(kinds))
    groups = dict(cat.GROUPS)
    for c in cat.CATALOG:
        assert c["group"] in groups and c["default"] in cat.SEVERITIES and c["resolves"] and c["sources"]


def test_overrides_and_reset(db):  # noqa: F811
    s = cat.EventSettings(db)
    assert s.severity_for("port.link_down") == "warning"
    assert s.set("port.link_down", severity="critical")["severity"] == "critical"
    assert s.severity_for("port.link_down") == "critical" and s.entry("port.link_down")["overridden"]
    assert s.reset("port.link_down")["severity"] == "warning"
    assert s.set("env.fan", severity="ignore")["severity"] == "ignore"


def test_params_merge_and_validate(db):  # noqa: F811
    s = cat.EventSettings(db)
    e = s.set("compute.cpu_high", params={"raise_percent": "95"})
    assert e["params"] == {"raise_percent": 95, "clear_percent": 80, "polls": 3}
    for bad in [dict(kind="nope"), dict(kind="compute.cpu_high", severity="loud"),
                dict(kind="compute.cpu_high", params={"unknown": 1}), dict(kind="compute.cpu_high", params={"polls": "x"}),
                dict(kind="syslog.rule", severity="critical")]:
        with pytest.raises(ValueError):
            s.set(**bad)


def test_port_overrides(db):  # noqa: F811
    p = cat.PortSettings(db)
    assert p.severity_for("s4048", "Te 1/47", "warning") == "warning" and not p.has_override("s4048", "Te 1/47")
    p.set("s4048", "Te 1/47", "critical")
    p.set("s4048", "Te 1/1", "ignore")
    assert p.severity_for("s4048", "Te 1/47", "warning") == "critical" and p.list("s4048") == {"Te 1/47": "critical", "Te 1/1": "ignore"}
    assert p.set("s4048", "Te 1/47", "default") is None and not p.has_override("s4048", "Te 1/47")
    with pytest.raises(ValueError):
        p.set("s4048", "Te 1/2", "loud")
