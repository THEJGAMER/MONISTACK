"""Every external edge on one local port must report that port's state.

Confirmed live and it drove a visible bug: a port carries one edge per
host on it, and only the edges whose LAG had already been expanded to its
physical members could be looked up in the status poller. Po 3 had 39
edges - 2 saying "Up", 37 saying nothing. The topology page keyed its
link-change detection by port, so the value it remembered flipped between
null and "Up" on every crawl and it announced "Po 3 is back up" twice
every thirty seconds, forever. The port chips on the map took their
colour from whichever edge happened to be first.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import app as app_module  # noqa: E402


def _assign(interfaces, edges, device_id="s4048"):
    """Drive the real state-assignment pass over a hand-built crawl."""
    app_module._assign_edge_states({"edges": edges}, {device_id: {i["port"]: i for i in interfaces}})
    return edges


def test_all_edges_on_a_port_report_the_same_state():
    """The real shape: some edges know the LAG's members, most do not."""
    interfaces = [
        {"port": "Te 1/39", "status": "Up", "input_mbps": 5.0, "output_mbps": 2.0},
        {"port": "Te 1/40", "status": "Up", "input_mbps": 6.0, "output_mbps": 3.0},
    ]
    edges = [
        {"kind": "external", "device_id": "s4048", "port": "Po 3", "member_ports": ["Te 1/39", "Te 1/40"]},
        *[{"kind": "external", "device_id": "s4048", "port": "Po 3", "member_ports": []} for _ in range(37)],
        {"kind": "external", "device_id": "s4048", "port": "Po 3", "member_ports": ["Te 1/39", "Te 1/40"]},
    ]

    _assign(interfaces, edges)

    statuses = {e["state"]["status"] for e in edges}
    assert statuses == {"Up"}, f"a port cannot be up and unknown at once: {statuses}"
    assert {e["state"]["input_mbps"] for e in edges} == {11.0}, "and the throughput is the bundle's, on every edge"


def test_a_port_nothing_knows_about_is_unknown_everywhere():
    edges = [{"kind": "external", "device_id": "s4048", "port": "Te 9/9", "member_ports": []} for _ in range(3)]

    _assign([], edges)

    assert all(e["state"]["status"] is None for e in edges)


def test_internal_edges_still_take_their_own_ends_state():
    interfaces = [{"port": "Te 1/47", "status": "Up", "input_mbps": 26.0, "output_mbps": 7.0}]
    edges = [{"kind": "internal",
              "a": {"device_id": "s4048", "port": "Te 1/47"},
              "b": {"device_id": "ex3300", "port": "xe-0/1/3"}}]

    _assign(interfaces, edges)

    assert edges[0]["a"]["state"]["status"] == "Up" and edges[0]["a"]["state"]["input_mbps"] == 26.0
    assert edges[0]["b"]["state"]["status"] is None, "the other device has its own poller state"
