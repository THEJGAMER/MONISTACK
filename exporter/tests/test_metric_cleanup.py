"""Tests for the exporter's device-removal metric cleanup.

Regression cover for a real live bug: removing a device stopped its poll
loop but left its last-scraped series in the exposition permanently. The
value that mattered was `s4048_up`, which is typically 0 at exactly that
moment (a device usually gets removed *because* it's dead), so
`S4048DeviceDown` kept firing forever for a device that no longer existed
- and nothing short of restarting the exporter could clear it.

These are the exporter's first tests; it had none before.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import exporter


def _series_for(device_id):
    """Every device_id-labelled series currently in the exposition."""
    found = []
    for metric in exporter._DEVICE_LABELED_METRICS:
        for labelvalues in getattr(metric, "_metrics", {}):
            if labelvalues and labelvalues[0] == device_id:
                found.append((metric._name, labelvalues))
    return found


def test_clear_device_metrics_removes_every_series_for_that_device():
    exporter.up.labels(device_id="doomed").set(0)
    exporter.fan_status.labels(device_id="doomed", unit="1", bay="1", fan="1").set(1)
    exporter.iface_up.labels(device_id="doomed", port="Te 1/1", description="").set(1)
    exporter.xcvr_alarm.labels(device_id="doomed", port="Te 1/1", flag="rx_power").set(0)
    assert _series_for("doomed"), "sanity: series should exist before removal"

    exporter._clear_device_metrics("doomed")

    assert _series_for("doomed") == []


def test_clear_device_metrics_leaves_other_devices_untouched():
    """The bug this guards against is over-broad deletion - removing one
    device must not blank the fleet."""
    exporter.up.labels(device_id="keeper").set(1)
    exporter.fan_status.labels(device_id="keeper", unit="1", bay="2", fan="1").set(1)
    exporter.up.labels(device_id="goner").set(0)

    exporter._clear_device_metrics("goner")

    assert _series_for("goner") == []
    kept = _series_for("keeper")
    assert len(kept) == 2, f"expected keeper's 2 series intact, got {kept}"


def test_clear_device_metrics_is_safe_on_an_unknown_device():
    """Called for a device that never emitted anything (or was already
    swept) - must be a no-op, not a KeyError in the registry-refresh loop."""
    exporter._clear_device_metrics("never-existed")


def test_up_metric_specifically_is_cleared():
    """Called out on its own because this is the series that actually
    caused the unclearable S4048DeviceDown alert - `s4048_up == 0` is the
    rule's expression."""
    exporter.up.labels(device_id="dead-device").set(0)
    assert ("dead-device",) in exporter.up._metrics

    exporter._clear_device_metrics("dead-device")

    assert ("dead-device",) not in exporter.up._metrics
