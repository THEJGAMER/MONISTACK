"""Tests for alert_acks.fingerprint_for - the function that decides what
"the same alarm" means.

This is worth pinning down precisely because both failure directions are
bad and neither is loud. If two genuinely different faults collide onto one
fingerprint, acknowledging one silently marks the other as owned and a real
alarm looks handled when nobody has looked at it. If the same fault
fingerprints differently between two observations, an ack detaches from the
alarm it was placed on - and the case that matters most there is an alarm
crossing from "pending" to "firing", i.e. exactly when it starts paging.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from alert_acks import fingerprint_for


def test_same_labels_same_fingerprint():
    labels = {"alertname": "S4048PSUDown", "bay": "2", "unit": "1", "severity": "critical"}
    assert fingerprint_for(labels) == fingerprint_for(dict(labels))


def test_key_order_does_not_change_identity():
    """Dict ordering is an implementation detail of whoever built the dict
    (Alertmanager's JSON, a Prometheus rule, our own code) and must not
    leak into identity."""
    a = {"alertname": "InterfaceDown", "device_id": "s4048", "port": "Te 1/47"}
    b = {"port": "Te 1/47", "alertname": "InterfaceDown", "device_id": "s4048"}
    assert fingerprint_for(a) == fingerprint_for(b)


def test_different_bay_is_a_different_alarm():
    """The real case this exists for: two power supplies on one switch share
    an alert name and differ only by `bay`. Acking one must not ack the
    other."""
    bay1 = {"alertname": "S4048PSUDown", "bay": "1", "unit": "1", "severity": "critical"}
    bay2 = {"alertname": "S4048PSUDown", "bay": "2", "unit": "1", "severity": "critical"}
    assert fingerprint_for(bay1) != fingerprint_for(bay2)


def test_different_port_is_a_different_alarm():
    a = {"alertname": "InterfaceDown", "device_id": "s4048", "port": "Te 1/47"}
    b = {"alertname": "InterfaceDown", "device_id": "s4048", "port": "Te 1/48"}
    assert fingerprint_for(a) != fingerprint_for(b)


def test_same_port_name_on_different_devices_is_a_different_alarm():
    a = {"alertname": "InterfaceDown", "device_id": "s4048", "port": "Te 1/47"}
    b = {"alertname": "InterfaceDown", "device_id": "ex3300-juniper", "port": "Te 1/47"}
    assert fingerprint_for(a) != fingerprint_for(b)


def test_extra_label_changes_identity():
    a = {"alertname": "InterfaceDown", "port": "Te 1/47"}
    b = {"alertname": "InterfaceDown", "port": "Te 1/47", "severity": "critical"}
    assert fingerprint_for(a) != fingerprint_for(b)


def test_separator_prevents_label_boundary_collision():
    """Without a delimiter between keys and values, {"a": "bc"} and
    {"ab": "c"} would both flatten to "abc" and collide - two unrelated
    alarms sharing one ack."""
    assert fingerprint_for({"a": "bc"}) != fingerprint_for({"ab": "c"})


def test_empty_and_none_are_handled():
    assert fingerprint_for({}) == fingerprint_for(None)
    assert isinstance(fingerprint_for({}), str)


def test_fingerprint_is_short_stable_hex():
    fp = fingerprint_for({"alertname": "S4048PSUDown", "bay": "2"})
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)
