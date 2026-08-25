"""Tests for sflow_store.py - the read side of the sFlow integration.

Rows here are written by sfacctd (pmacct) on the collector LXC, never by
this app, so these tests exercise the queries against a real Postgres with
rows shaped exactly as pmacct writes them.

The ifIndex decode is the part most worth pinning: it is arithmetic
derived from the hardware, not documentation, and a wrong mapping would
label traffic with confidently wrong port names - worse than showing a raw
number.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

import sflow_store  # noqa: E402
from sflow_store import SFlowStore, ifindex_to_port, proto_name, service_name  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")

DDL = """
CREATE TABLE sflow_flows (
    id BIGSERIAL PRIMARY KEY,
    peer_ip_src TEXT NOT NULL,
    iface_in BIGINT, iface_out BIGINT,
    ip_src TEXT, ip_dst TEXT,
    port_src INTEGER, port_dst INTEGER,
    ip_proto TEXT,
    packets BIGINT NOT NULL DEFAULT 0,
    bytes BIGINT NOT NULL DEFAULT 0,
    stamp_inserted TIMESTAMPTZ NOT NULL DEFAULT now(),
    stamp_updated TIMESTAMPTZ
);
"""


def _reachable():
    try:
        psycopg2.connect(DSN, connect_timeout=4).close()
        return True
    except Exception:
        return False


# --- the ifIndex decode (no DB needed) -------------------------------

def test_dell_ifindex_decodes_to_the_real_port_names():
    """Verified against the live S4048 by reading `Interface index is ...`
    for each of these - all four matched, which is why this is arithmetic
    rather than a lookup table."""
    assert ifindex_to_port(2097156) == "Te 1/1"
    assert ifindex_to_port(2101764) == "Te 1/37"
    assert ifindex_to_port(2101892) == "Te 1/38"
    assert ifindex_to_port(2103172) == "Te 1/48"


def test_an_ifindex_outside_the_known_pattern_returns_none():
    """A raw number the UI shows as-is beats a confidently wrong port
    name. Anything not on the 128-byte stride, below the base, or past a
    plausible port count is unknown rather than extrapolated."""
    assert ifindex_to_port(2097157) is None      # not on the stride
    assert ifindex_to_port(12) is None           # below the base
    assert ifindex_to_port(99999999) is None     # implausibly far past
    assert ifindex_to_port(None) is None
    assert ifindex_to_port("not-a-number") is None


def test_a_non_dell_platform_does_not_borrow_the_dell_mapping():
    """Junos encodes ifIndex differently. Applying the OS9 arithmetic to a
    Junos flow would produce a real-looking but wrong port name."""
    assert ifindex_to_port(2101764, platform="junos") is None


def test_protocol_and_service_naming():
    assert proto_name("6") == "tcp"
    assert proto_name("17") == "udp"
    assert proto_name("99") == "99", "an unknown protocol keeps its number"
    assert service_name(554) == "rtsp"
    assert service_name(445) == "smb"
    assert service_name(51999) is None


# --- the queries ------------------------------------------------------

pytestmark = pytest.mark.skipif(not _reachable(), reason="no Postgres reachable for integration test")


class _DB:
    def __init__(self, conn):
        self.conn = conn

    def _cur(self):
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur

    def query(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchall()

    def query_one(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchone()


@pytest.fixture
def store():
    schema = f"test_sflow_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(DDL)
    try:
        yield SFlowStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _flow(store, src, dst, b, sp=50000, dp=443, proto="6", i_in=2101764, i_out=2101892,
          agent="192.168.4.106", age_minutes=1):
    store.db.execute(
        """INSERT INTO sflow_flows
           (peer_ip_src, iface_in, iface_out, ip_src, ip_dst, port_src, port_dst,
            ip_proto, packets, bytes, stamp_inserted)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() - (%s || ' minutes')::interval)""",
        (agent, i_in, i_out, src, dst, sp, dp, proto, 1, b, str(age_minutes)))


def test_available_distinguishes_empty_from_populated(store):
    assert store.available() is False
    _flow(store, "10.0.0.1", "10.0.0.2", 100)
    assert store.available() is True


def test_top_talkers_ranks_by_bytes(store):
    _flow(store, "10.0.0.1", "8.8.8.8", 500)
    _flow(store, "10.0.0.2", "8.8.8.8", 9000)

    top = store.top_talkers(since_minutes=60)

    assert top[0]["ip_src"] == "10.0.0.2"
    assert int(top[0]["bytes"]) == 9000


def test_top_hosts_counts_both_directions(store):
    """A host that mostly *receives* is still a top host. Ranking only by
    ip_src would hide exactly the download-heavy client you're looking for."""
    _flow(store, "10.0.0.9", "10.0.0.1", 100)
    _flow(store, "10.0.0.2", "10.0.0.9", 8000)

    hosts = {h["host"]: int(h["bytes"]) for h in store.top_hosts(since_minutes=60)}

    assert hosts["10.0.0.9"] == 8100


def test_the_time_window_excludes_older_flows(store):
    _flow(store, "10.0.0.1", "10.0.0.2", 100, age_minutes=1)
    _flow(store, "10.0.0.3", "10.0.0.4", 999, age_minutes=600)

    recent = store.top_talkers(since_minutes=60)

    assert [t["ip_src"] for t in recent] == ["10.0.0.1"]


def test_filtering_by_agent(store):
    _flow(store, "10.0.0.1", "10.0.0.2", 100, agent="192.168.4.106")
    _flow(store, "10.0.0.3", "10.0.0.4", 100, agent="192.168.4.1")

    only = store.top_talkers(since_minutes=60, agent_ip="192.168.4.1")

    assert [t["ip_src"] for t in only] == ["10.0.0.3"]


def test_protocol_mix_keys_on_the_well_known_port(store):
    """Two clients hitting the same HTTPS server must collapse into one
    row, not appear as two ephemeral-port rows."""
    _flow(store, "10.0.0.1", "10.0.0.9", 100, sp=51001, dp=443)
    _flow(store, "10.0.0.2", "10.0.0.9", 100, sp=52002, dp=443)

    mix = store.protocol_mix(since_minutes=60)

    assert len(mix) == 1
    assert mix[0]["port"] == 443
    assert mix[0]["service"] == "https"
    assert int(mix[0]["bytes"]) == 200


def test_per_port_separates_in_from_out(store):
    """A port that only ever receives is a real signal; summing the two
    directions would hide it."""
    _flow(store, "10.0.0.1", "10.0.0.2", 700, i_in=2101764, i_out=2101892)

    rows = {r["iface"]: r for r in store.per_port(since_minutes=60, platform_for=lambda a: "os9")}

    assert int(rows[2101764]["in_bytes"]) == 700
    assert int(rows[2101764]["out_bytes"]) == 0
    assert int(rows[2101892]["out_bytes"]) == 700
    assert rows[2101764]["port"] == "Te 1/37"


def test_per_port_does_not_merge_the_same_ifindex_across_switches(store):
    """An ifIndex is only meaningful relative to the switch that issued
    it. With two switches sending, ifIndex 1 on each is a different port -
    grouping on the index alone silently summed them into one row. Found
    once both a Dell and a Juniper were really exporting."""
    _flow(store, "10.0.0.1", "10.0.0.2", 100, i_in=1, i_out=2, agent="192.168.4.106")
    _flow(store, "10.0.0.3", "10.0.0.4", 900, i_in=1, i_out=2, agent="192.168.5.10")

    rows = store.per_port(since_minutes=60)
    ifindex_1 = [r for r in rows if r["iface"] == 1]

    assert len(ifindex_1) == 2, "one row per (switch, ifIndex), not one per ifIndex"
    assert {int(r["in_bytes"]) for r in ifindex_1} == {100, 900}


def test_each_row_is_decoded_with_its_own_switch_platform(store):
    """A Juniper ifIndex must never be run through Dell's arithmetic. The
    per-row platform lookup is what prevents one vendor's encoding being
    applied to another's flows."""
    _flow(store, "10.0.0.1", "10.0.0.2", 100, i_in=2101764, i_out=2101892, agent="192.168.4.106")
    _flow(store, "10.0.0.3", "10.0.0.4", 100, i_in=503, i_out=504, agent="192.168.5.10")

    platform_for = {"192.168.4.106": "os9", "192.168.5.10": "junos"}.get
    rows = {(r["peer_ip_src"], r["iface"]): r for r in
            store.per_port(since_minutes=60, platform_for=platform_for)}

    assert rows[("192.168.4.106", 2101764)]["port"] == "Te 1/37"
    assert rows[("192.168.5.10", 503)]["port"] is None, "no Dell decode on a Junos ifIndex"


def test_an_unknown_agent_gets_no_vendor_decode(store):
    """The default must be "no opinion", not "assume Dell" - applying one
    vendor's arithmetic to an unidentified agent is how a real-looking but
    wrong port name gets shown."""
    _flow(store, "10.0.0.1", "10.0.0.2", 100, i_in=2101764, agent="10.9.9.9")

    rows = store.per_port(since_minutes=60, platform_for=lambda a: None)

    assert all(r["port"] is None for r in rows)


def test_port_detail_matches_traffic_in_either_direction(store):
    _flow(store, "10.0.0.1", "10.0.0.2", 100, i_in=2101764, i_out=2101892)

    assert len(store.port_detail(2101764, since_minutes=60)) == 1
    assert len(store.port_detail(2101892, since_minutes=60)) == 1
    assert len(store.port_detail(2102020, since_minutes=60)) == 0


def test_agents_summarises_each_switch(store):
    _flow(store, "10.0.0.1", "10.0.0.2", 100, agent="192.168.4.106")
    _flow(store, "10.0.0.3", "10.0.0.4", 250, agent="192.168.4.1")

    agents = {a["peer_ip_src"]: int(a["bytes"]) for a in store.agents(since_minutes=60)}

    assert agents == {"192.168.4.106": 100, "192.168.4.1": 250}
