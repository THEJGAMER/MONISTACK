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


# --- discovered ifIndex maps -----------------------------------------
# Dell OS9's encoding is arithmetic and verified; Junos's is irregular
# (501, 503, 525, 547...) and has to be read off the switch, so it is
# discovered and cached. These pin that the cache always beats inference.

def test_a_cached_mapping_wins_over_the_arithmetic():
    """The switch's own answer beats our inference about it. If a device
    reports 2101764 as something other than Te 1/37, the device is right."""
    assert ifindex_to_port(2101764, "os9") == "Te 1/37"
    assert ifindex_to_port(2101764, "os9", cached={2101764: "Po 1"}) == "Po 1"


def test_a_cached_mapping_names_a_junos_port_the_arithmetic_cannot():
    """The whole point for Junos - 525 is derivable from nothing."""
    assert ifindex_to_port(525, "junos") is None
    assert ifindex_to_port(525, "junos", cached={525: "ge-0/0/2"}) == "ge-0/0/2"


def test_a_cache_miss_still_falls_back_rather_than_guessing():
    assert ifindex_to_port(999, "junos", cached={525: "ge-0/0/2"}) is None
    assert ifindex_to_port(2101764, "os9", cached={525: "ge-0/0/2"}) == "Te 1/37"


def test_ifindex_map_round_trips(store):
    from sflow_store import IfIndexMap

    m = IfIndexMap(store.db)
    store.db.execute("""CREATE TABLE sflow_ifindex (
        device_id TEXT NOT NULL, ifindex BIGINT NOT NULL, port TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY (device_id, ifindex))""")

    m.save("ex3300", {501: "ge-0/0/0", 525: "ge-0/0/2"})

    assert m.load("ex3300") == {501: "ge-0/0/0", 525: "ge-0/0/2"}
    assert m.load_all()["ex3300"][525] == "ge-0/0/2"


def test_saving_replaces_rather_than_merges(store):
    """A port that has genuinely gone away must not linger as a stale name
    on an ifIndex the switch may since have reused."""
    from sflow_store import IfIndexMap

    m = IfIndexMap(store.db)
    store.db.execute("""CREATE TABLE sflow_ifindex (
        device_id TEXT NOT NULL, ifindex BIGINT NOT NULL, port TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY (device_id, ifindex))""")

    m.save("ex3300", {501: "ge-0/0/0", 525: "ge-0/0/2"})
    m.save("ex3300", {501: "ge-0/0/0"})

    assert m.load("ex3300") == {501: "ge-0/0/0"}


def test_an_empty_discovery_does_not_wipe_a_good_map(store):
    """A failed or truncated read must leave the previous map intact -
    losing every port name is worse than a slightly stale one."""
    from sflow_store import IfIndexMap

    m = IfIndexMap(store.db)
    store.db.execute("""CREATE TABLE sflow_ifindex (
        device_id TEXT NOT NULL, ifindex BIGINT NOT NULL, port TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY (device_id, ifindex))""")

    m.save("ex3300", {501: "ge-0/0/0"})
    m.save("ex3300", {})

    assert m.load("ex3300") == {501: "ge-0/0/0"}


# --- time series / host detail / totals -------------------------------

def test_timeseries_buckets_by_agent(store):
    """The one view that keeps time. Every other view on the page
    collapses it away entirely."""
    _flow(store, "10.0.0.1", "10.0.0.2", 100, agent="a", age_minutes=1)
    _flow(store, "10.0.0.1", "10.0.0.2", 200, agent="a", age_minutes=1)
    _flow(store, "10.0.0.3", "10.0.0.4", 50, agent="b", age_minutes=1)

    ts = store.timeseries(since_minutes=60)

    assert set(ts["series"]) == {"a", "b"}
    assert sum(p["bytes"] for p in ts["series"]["a"]) == 300


def test_bucket_size_scales_with_the_window():
    """A 7-day window at 1-minute resolution is 10,080 points per series -
    slower to render and less legible than the ~150 a chart needs."""
    from sflow_store import SFlowStore as S

    assert S.bucket_for(30) == 60
    assert S.bucket_for(360) == 300
    assert S.bucket_for(1440) == 900
    assert S.bucket_for(10080) == 21600
    assert S.bucket_for(999999) == 21600, "an out-of-range window still returns a usable bucket"


def test_host_detail_covers_both_directions_and_labels_them(store):
    """A host's traffic is split across ip_src and ip_dst; showing only
    one direction answers half the question and looks complete."""
    _flow(store, "10.0.0.9", "10.0.0.1", 100)
    _flow(store, "10.0.0.2", "10.0.0.9", 800)

    flows = store.host_detail("10.0.0.9", since_minutes=60)

    assert {f["direction"] for f in flows} == {"in", "out"}
    assert sum(int(f["bytes"]) for f in flows) == 900


def test_host_detail_excludes_unrelated_conversations(store):
    _flow(store, "10.0.0.9", "10.0.0.1", 100)
    _flow(store, "10.0.0.7", "10.0.0.8", 999)

    assert len(store.host_detail("10.0.0.9", since_minutes=60)) == 1


def test_totals_summarise_the_window(store):
    _flow(store, "10.0.0.1", "10.0.0.2", 100, agent="a")
    _flow(store, "10.0.0.3", "10.0.0.4", 200, agent="b")

    t = store.totals(since_minutes=60)

    assert int(t["bytes"]) == 300
    assert int(t["records"]) == 2
    assert int(t["agents"]) == 2
    assert int(t["talkers"]) == 2


def test_totals_are_zero_not_null_on_an_empty_window(store):
    """A stat tile rendering "null" is worse than one rendering 0."""
    t = store.totals(since_minutes=60)

    assert int(t["bytes"]) == 0 and int(t["records"]) == 0


# --- the time window -------------------------------------------------
# One control drives every panel on the sFlow page, so the window has to
# mean exactly the same thing to all of them.

from datetime import timedelta  # noqa: E402


def _now(store):
    return store.db.query_one("SELECT now() AS now")["now"]


def test_an_absolute_window_excludes_flows_outside_it(store):
    _flow(store, "10.0.0.1", "8.8.8.8", 100, age_minutes=200)   # before
    _flow(store, "10.0.0.2", "8.8.8.8", 200, age_minutes=120)   # inside
    _flow(store, "10.0.0.3", "8.8.8.8", 400, age_minutes=10)    # after
    now = _now(store)

    rows = store.top_talkers(start=now - timedelta(minutes=180), end=now - timedelta(minutes=60))

    assert [r["ip_src"] for r in rows] == ["10.0.0.2"]


def test_the_window_is_half_open_so_adjacent_ranges_do_not_double_count(store):
    """Two ranges that meet at a boundary must together count each row
    once - a chart built from tiled windows would otherwise show a spike
    at every join."""
    # Inserted at a timestamp equal to the boundary itself. Deriving the
    # row's age and the boundary separately does not test this: `now()`
    # advances between the two, the row lands just inside one window, and
    # the boundary rule is never exercised at all.
    edge = _now(store) - timedelta(minutes=30)
    store.db.execute(
        """INSERT INTO sflow_flows (peer_ip_src, ip_src, ip_dst, packets, bytes, stamp_inserted)
           VALUES ('192.168.4.106', '10.0.0.1', '8.8.8.8', 1, 100, %s)""", (edge,))

    older = store.totals(start=edge - timedelta(minutes=30), end=edge)
    newer = store.totals(start=edge, end=_now(store))

    assert int(older["records"] or 0) == 1, "a row on the boundary belongs to the window ending there"
    assert int(newer["records"] or 0) == 0, "and not also to the one starting there"


def test_resolve_window_turns_minutes_into_concrete_bounds(store):
    start, end = store.resolve_window(since_minutes=90)

    assert (end - start) == timedelta(minutes=90)
    # Anchored to the database's clock, not this process's - the two run
    # on different hosts in production.
    assert abs((end - _now(store)).total_seconds()) < 5


def test_resolve_window_leaves_an_explicit_range_alone(store):
    a, b = _now(store) - timedelta(days=3), _now(store) - timedelta(days=1)

    assert store.resolve_window(since_minutes=60, start=a, end=b) == (a, b)


def test_every_view_agrees_when_handed_one_resolved_window(store):
    """The property the whole page rests on. Before this, each view
    evaluated now() in its own query, so a slow page could show panels
    covering measurably different spans while claiming one range."""
    _flow(store, "10.0.0.1", "8.8.8.8", 100, age_minutes=5)
    _flow(store, "10.0.0.2", "8.8.8.8", 200, age_minutes=400)
    start, end = store.resolve_window(since_minutes=60)
    win = {"start": start, "end": end}

    assert int(store.totals(**win)["records"]) == 1
    assert len(store.top_talkers(**win)) == 1
    assert len(store.top_hosts(**win)) == 2          # both ends of the one flow
    assert len(store.agents(**win)) == 1
    assert len(store.protocol_mix(**win)) == 1
    assert sum(len(v) for v in store.timeseries(**win)["series"].values()) == 1


def test_bucket_size_follows_the_absolute_span_not_the_minutes_default(store):
    """An absolute range leaves `since_minutes` at its default, so a
    chart over a week would be bucketed as if it were an hour - 10,080
    points per series instead of the ~150 a chart can show."""
    now = _now(store)
    week = store.timeseries(start=now - timedelta(days=7), end=now)
    hour = store.timeseries(start=now - timedelta(hours=1), end=now)

    assert week["bucket_seconds"] > hour["bucket_seconds"]
    assert week["bucket_seconds"] == store.bucket_for(7 * 24 * 60)


def test_relative_windows_still_work_untouched(store):
    """The absolute path is additive: nothing that only passes minutes
    should change behaviour."""
    _flow(store, "10.0.0.1", "8.8.8.8", 100, age_minutes=5)
    _flow(store, "10.0.0.2", "8.8.8.8", 200, age_minutes=400)

    assert len(store.top_talkers(since_minutes=60)) == 1
    assert len(store.top_talkers(since_minutes=600)) == 2


# --- the search ------------------------------------------------------
# The bug this covers: the filter used to run in the browser over the top
# 20 rows the API had already returned, so anything ranked below that was
# unfindable no matter how exactly it was typed. Found with a real host
# sitting 86th of 152.

def _quiet_and_loud(store):
    """One host far below the top of the ranking, and enough noise above
    it that a top-N cut would drop it."""
    for i in range(25):
        _flow(store, f"10.9.{i}.1", "8.8.8.8", 10_000_000 + i)
    _flow(store, "192.168.0.125", "192.168.0.1", 1671, sp=50001, dp=445)


def test_a_host_below_the_top_n_is_still_found(store):
    _quiet_and_loud(store)

    assert store.top_hosts(since_minutes=60, limit=20, q="192.168.0.125") != []
    assert store.top_talkers(since_minutes=60, limit=20, q="192.168.0.125") != []


def test_that_host_is_genuinely_outside_the_unfiltered_page(store):
    """Proves the test above is testing something - if the quiet host
    happened to rank inside the limit, the search would look like it
    worked while doing nothing."""
    _quiet_and_loud(store)

    hosts = [h["host"] for h in store.top_hosts(since_minutes=60, limit=20)]

    assert "192.168.0.125" not in hosts


def test_top_hosts_returns_the_match_not_its_peers(store):
    """Row matching keeps a flow when either end matches, so folding both
    endpoints together would list the peer beside it."""
    _quiet_and_loud(store)

    hosts = [h["host"] for h in store.top_hosts(since_minutes=60, q="192.168.0.125")]

    assert hosts == ["192.168.0.125"]
    assert "192.168.0.1" not in hosts, "the peer belongs in Top talkers, not here"


def test_top_talkers_keeps_the_peer_so_the_conversation_is_visible(store):
    _quiet_and_loud(store)

    row = store.top_talkers(since_minutes=60, q="192.168.0.125")[0]

    assert {row["ip_src"], row["ip_dst"]} == {"192.168.0.125", "192.168.0.1"}


def test_a_partial_address_matches(store):
    _quiet_and_loud(store)

    assert [h["host"] for h in store.top_hosts(since_minutes=60, q="0.125")] == ["192.168.0.125"]


def test_searching_by_port_number(store):
    _quiet_and_loud(store)

    assert store.top_talkers(since_minutes=60, q="445") != []
    assert store.top_talkers(since_minutes=60, q="9999") == []


def test_searching_by_service_name(store):
    """445 is smb; typing the name should not require knowing the number."""
    _quiet_and_loud(store)

    rows = store.top_talkers(since_minutes=60, q="smb")

    assert [r["ip_src"] for r in rows] == ["192.168.0.125"]


def test_searching_by_interface_ifindex_set(store):
    """Interface *names* are decoded in Python from an SSH-discovered map,
    so the caller resolves them to ifIndexes and passes those down."""
    _quiet_and_loud(store)

    assert store.top_talkers(since_minutes=60, q="Te 1/37", q_ifaces=[2101764]) != []
    assert store.top_talkers(since_minutes=60, q="Te 1/37", q_ifaces=[999999]) == []


def test_the_totals_reflect_the_search(store):
    """The tiles sit above the tables, so leaving them unfiltered would
    caption a filtered page with the unfiltered numbers."""
    _quiet_and_loud(store)

    assert int(store.totals(since_minutes=60, q="192.168.0.125")["records"]) == 1


def test_an_empty_search_changes_nothing(store):
    _quiet_and_loud(store)

    assert len(store.top_hosts(since_minutes=60, limit=20)) == \
           len(store.top_hosts(since_minutes=60, limit=20, q="   "))


def test_a_search_matching_nothing_returns_nothing_rather_than_everything(store):
    """The failure mode worth guarding: a clause that silently drops out
    turns a no-match search into an unfiltered page."""
    _quiet_and_loud(store)

    assert store.top_hosts(since_minutes=60, q="203.0.113.99") == []
    assert store.totals(since_minutes=60, q="203.0.113.99")["records"] in (0, None)


def test_a_service_search_lists_the_hosts_doing_it(store):
    """"192.168.0.125" means "this machine"; "https" means "whoever is
    doing this". Narrowing Top hosts by name in the second case empties
    the panel, since no host is called https."""
    _quiet_and_loud(store)

    hosts = [h["host"] for h in store.top_hosts(since_minutes=60, q="smb")]

    assert sorted(hosts) == ["192.168.0.1", "192.168.0.125"]


def test_an_interface_search_lists_the_hosts_crossing_it(store):
    _quiet_and_loud(store)

    hosts = store.top_hosts(since_minutes=60, q="Te 1/37", q_ifaces=[2101764])

    assert hosts != []


def test_address_likeness_splits_the_two_behaviours():
    assert sflow_store._looks_like_address("192.168.0.125")
    assert sflow_store._looks_like_address("0.125")
    assert sflow_store._looks_like_address("fe80::1")
    # A bare number is far more often a port than an address fragment.
    assert not sflow_store._looks_like_address("445")
    assert not sflow_store._looks_like_address("https")
    assert not sflow_store._looks_like_address("Te 1/37")
    assert not sflow_store._looks_like_address("")


# --- the two vantage points ------------------------------------------

def test_the_source_selects_the_table(store):
    assert sflow_store.SFlowStore(store.db, source="switches").table == "sflow_flows"
    assert sflow_store.SFlowStore(store.db, source="firewall").table == "netflow_flows"


def test_an_unknown_source_is_refused_at_construction(store):
    """The table name is interpolated into SQL - it cannot be a bound
    parameter - so it must never be able to carry a caller's string."""
    with pytest.raises(ValueError):
        sflow_store.SFlowStore(store.db, source="'; DROP TABLE sflow_flows; --")
    with pytest.raises(ValueError):
        sflow_store.SFlowStore(store.db, source="netflow_flows")   # the value, not the key


def test_capped_rows_counts_only_those_at_the_ceiling(store):
    _flow(store, "10.0.0.1", "8.8.8.8", 500)
    _flow(store, "10.0.0.2", "8.8.8.8", 4_294_901_889)   # at the 32-bit ceiling
    _flow(store, "10.0.0.3", "8.8.8.8", 3_000_000_000)   # large but under it

    assert store.capped_rows(since_minutes=60) == 1


def test_capped_rows_respects_the_window_and_the_search(store):
    _flow(store, "10.0.0.2", "8.8.8.8", 4_294_901_889, age_minutes=5)
    _flow(store, "10.0.0.9", "8.8.8.8", 4_294_901_889, age_minutes=400)

    assert store.capped_rows(since_minutes=60) == 1
    assert store.capped_rows(since_minutes=600) == 2
    assert store.capped_rows(since_minutes=600, q="10.0.0.9") == 1


def test_capped_rows_are_still_included_in_the_totals(store):
    """Reported, not filtered: they are a small share of rows and a large
    share of bytes, so dropping them removes more traffic than keeping
    them understates."""
    _flow(store, "10.0.0.2", "8.8.8.8", 4_294_901_889)

    assert int(store.totals(since_minutes=60)["bytes"]) == 4_294_901_889
    assert len(store.top_talkers(since_minutes=60)) == 1
