"""sFlow traffic views, read from the `sflow_flows` table.

Unlike every other store here, this one only ever *reads*: the rows are
written by sfacctd (pmacct) on the collector LXC, straight into the same
Postgres (see sflow/README.md). That split is deliberate - the collector
is a well-tested C daemon doing sFlow's fiddly XDR decoding, and this app
only has to ask questions of the result.

Flows are pre-aggregated into 1-minute buckets by sfacctd's `sql_history`,
so "top talkers over the last hour" is a sum over ~60 rows per flow rather
than a scan across raw samples.

A note on what these numbers mean: sFlow *samples* - the switch reports
one packet in N (32768 by default on this S4048). Byte counts are
therefore an estimate of real traffic, not a measurement of it, and are
only meaningful in aggregate and in relative terms. The UI says so rather
than presenting them as exact.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("webui.sflow")

# Dell OS9 encodes a physical port's ifIndex arithmetically. Verified
# against the real S4048 by reading `show interface ... | Interface index`
# for Te 1/1, 1/37, 1/38 and 1/48 - all four matched exactly, so this is a
# derived-and-checked mapping rather than a guess from documentation.
#
# Only stacking unit 1 / TenGigabitEthernet is covered, which is what this
# switch has. Anything outside the range falls through to the raw ifIndex,
# because showing "2103172" is honest where inventing "Te 1/999" is not.
_OS9_IFINDEX_BASE = 2097156
_OS9_IFINDEX_STEP = 128
_OS9_MAX_PORT = 64

# Well-known ports worth naming in the protocol view. Deliberately short:
# a full IANA list would name thousands of ports that never appear, and
# the useful signal is "is this the camera, SMB, or DNS", not exhaustive
# coverage.
_PORT_NAMES = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns",
    67: "dhcp", 68: "dhcp", 69: "tftp", 80: "http", 123: "ntp", 161: "snmp",
    162: "snmp-trap", 179: "bgp", 389: "ldap", 443: "https", 445: "smb",
    514: "syslog", 554: "rtsp", 587: "smtp", 636: "ldaps", 993: "imaps",
    995: "pop3s", 1194: "openvpn", 3128: "squid", 3306: "mysql",
    3389: "rdp", 5432: "postgres", 5601: "kibana", 6343: "sflow",
    8080: "http-alt", 8443: "https-alt", 9090: "prometheus", 9093: "alertmanager",
    9100: "node-exporter", 9101: "s4048-exporter", 3100: "loki", 51820: "wireguard",
}

_PROTO_NAMES = {"1": "icmp", "6": "tcp", "17": "udp", "47": "gre", "50": "esp", "58": "icmpv6"}


def ifindex_to_port(ifindex, platform="os9", cached=None):
    """Best-effort ifIndex -> human port name. Returns None when unknown,
    which callers surface as the raw index rather than a fabricated name.

    `cached` is a {ifindex: port} map discovered from the device and always
    wins: it is what the switch itself reports, where the arithmetic below
    is inference that only holds for Dell OS9 physical ports."""
    if ifindex is None:
        return None
    try:
        ifindex = int(ifindex)
    except (TypeError, ValueError):
        return None
    if cached and ifindex in cached:
        return cached[ifindex]
    if platform == "os9":
        offset = ifindex - _OS9_IFINDEX_BASE
        if offset >= 0 and offset % _OS9_IFINDEX_STEP == 0:
            port = offset // _OS9_IFINDEX_STEP + 1
            if 1 <= port <= _OS9_MAX_PORT:
                return f"Te 1/{port}"
    return None


def proto_name(proto):
    return _PROTO_NAMES.get(str(proto), str(proto))


def service_name(port):
    """The service a port number represents, if it's one worth naming."""
    try:
        return _PORT_NAMES.get(int(port))
    except (TypeError, ValueError):
        return None


class IfIndexMap:
    """Cached SNMP ifIndex -> port-name lookup, per device.

    Dell OS9's encoding is arithmetic and verified, so it needs no device
    round trip. Junos's is irregular and must be read off the switch, so
    it is discovered and cached here. A miss falls back to the arithmetic,
    then to None - never to a guess."""

    def __init__(self, db):
        self.db = db

    def save(self, device_id, mapping):
        """Replaces one device's map. Deletes first so a port that has
        genuinely gone away (a removed module) doesn't linger as a stale
        name attached to an ifIndex the switch has since reused."""
        if not mapping:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("DELETE FROM sflow_ifindex WHERE device_id = %s", (device_id,))
        for ifindex, port in mapping.items():
            self.db.execute(
                """INSERT INTO sflow_ifindex (device_id, ifindex, port, updated_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (device_id, ifindex) DO UPDATE
                     SET port = EXCLUDED.port, updated_at = EXCLUDED.updated_at""",
                (device_id, int(ifindex), str(port), now),
            )
        return len(mapping)

    def load(self, device_id):
        rows = self.db.query(
            "SELECT ifindex, port FROM sflow_ifindex WHERE device_id = %s", (device_id,)
        )
        return {int(r["ifindex"]): r["port"] for r in rows}

    def load_all(self):
        """{device_id: {ifindex: port}} - one query, since the sFlow page
        renders every agent at once."""
        out = {}
        try:
            for r in self.db.query("SELECT device_id, ifindex, port FROM sflow_ifindex"):
                out.setdefault(r["device_id"], {})[int(r["ifindex"])] = r["port"]
        except Exception:
            log.warning("could not load sflow ifindex cache", exc_info=True)
        return out


class SFlowStore:
    def __init__(self, db):
        self.db = db

    # --- shared -------------------------------------------------------

    def _window(self, since_minutes, agent_ip=None, iface=None):
        """Builds the WHERE shared by every view. Time always leads, since
        both indexes on this table are (something, stamp_inserted DESC)."""
        clauses = ["stamp_inserted > now() - (%s || ' minutes')::interval"]
        params = [str(int(since_minutes))]
        if agent_ip:
            clauses.append("peer_ip_src = %s")
            params.append(agent_ip)
        if iface is not None:
            clauses.append("(iface_in = %s OR iface_out = %s)")
            params += [int(iface), int(iface)]
        return " AND ".join(clauses), params

    def available(self):
        """Whether any sFlow has ever arrived. Drives the UI's empty state,
        which needs to distinguish "no traffic matched your filter" from
        "no switch has ever sent us anything" - those need very different
        advice."""
        try:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM sflow_flows")
            return bool(row and row["n"])
        except Exception:
            log.warning("could not check sflow availability", exc_info=True)
            return False

    def agents(self, since_minutes=60):
        """Switches that have sent flows recently, newest activity first."""
        where, params = self._window(since_minutes)
        rows = self.db.query(
            f"""SELECT peer_ip_src, COUNT(*) AS flows, SUM(bytes) AS bytes,
                       MAX(stamp_inserted) AS last_seen
                  FROM sflow_flows WHERE {where}
                 GROUP BY peer_ip_src ORDER BY bytes DESC NULLS LAST""",
            tuple(params),
        )
        return [dict(r) for r in rows]

    # --- the four views ----------------------------------------------

    def top_talkers(self, since_minutes=60, agent_ip=None, limit=20):
        where, params = self._window(since_minutes, agent_ip)
        rows = self.db.query(
            f"""SELECT ip_src, ip_dst, SUM(bytes) AS bytes, SUM(packets) AS packets,
                       COUNT(*) AS samples
                  FROM sflow_flows WHERE {where}
                 GROUP BY ip_src, ip_dst
                 ORDER BY bytes DESC NULLS LAST LIMIT %s""",
            tuple(params) + (int(limit),),
        )
        return [dict(r) for r in rows]

    def top_hosts(self, since_minutes=60, agent_ip=None, limit=20):
        """Per-host totals, counting a host's traffic in both directions -
        "who is using the bandwidth" rather than "which pair is busiest".
        A conversation view alone hides a host spread across many peers."""
        where, params = self._window(since_minutes, agent_ip)
        rows = self.db.query(
            f"""SELECT host, SUM(bytes) AS bytes, SUM(packets) AS packets FROM (
                    SELECT ip_src AS host, bytes, packets FROM sflow_flows WHERE {where}
                    UNION ALL
                    SELECT ip_dst AS host, bytes, packets FROM sflow_flows WHERE {where}
                ) t WHERE host IS NOT NULL
                GROUP BY host ORDER BY bytes DESC NULLS LAST LIMIT %s""",
            tuple(params) + tuple(params) + (int(limit),),
        )
        return [dict(r) for r in rows]

    def protocol_mix(self, since_minutes=60, agent_ip=None, limit=20):
        """Traffic by service. Keyed on the *lower* of the two ports:
        an ephemeral source port is noise, and the well-known side is what
        identifies the service - without this, one HTTPS server appears as
        hundreds of separate rows, one per client port."""
        where, params = self._window(since_minutes, agent_ip)
        rows = self.db.query(
            f"""SELECT ip_proto,
                       LEAST(COALESCE(port_src, 65535), COALESCE(port_dst, 65535)) AS port,
                       SUM(bytes) AS bytes, SUM(packets) AS packets
                  FROM sflow_flows WHERE {where}
                 GROUP BY ip_proto, port
                 ORDER BY bytes DESC NULLS LAST LIMIT %s""",
            tuple(params) + (int(limit),),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["proto_name"] = proto_name(d.get("ip_proto"))
            d["service"] = service_name(d.get("port"))
            out.append(d)
        return out

    def per_port(self, since_minutes=60, agent_ip=None, platform_for=None,
                 cached_for=None, limit=50):
        """Traffic per switch interface, in and out kept separate so a
        one-directional problem (a port only ever receiving) is visible
        rather than averaged away.

        Grouped by (agent, iface), not iface alone: an ifIndex is only
        meaningful relative to the switch that issued it, so with two
        switches sending, ifIndex 1 on each is two different ports.
        Grouping on the index by itself silently summed them into one row.

        `platform_for` is a callable agent_ip -> platform (or None), so
        each row is decoded with its own switch's encoding rather than one
        platform applied to every agent."""
        where, params = self._window(since_minutes, agent_ip)
        rows = self.db.query(
            f"""SELECT peer_ip_src, iface, SUM(in_bytes) AS in_bytes, SUM(out_bytes) AS out_bytes,
                       SUM(in_pkts) AS in_packets, SUM(out_pkts) AS out_packets FROM (
                    SELECT peer_ip_src, iface_in AS iface, bytes AS in_bytes, 0 AS out_bytes,
                           packets AS in_pkts, 0 AS out_pkts
                      FROM sflow_flows WHERE {where} AND iface_in IS NOT NULL
                    UNION ALL
                    SELECT peer_ip_src, iface_out AS iface, 0, bytes, 0, packets
                      FROM sflow_flows WHERE {where} AND iface_out IS NOT NULL
                ) t GROUP BY peer_ip_src, iface
                ORDER BY (SUM(in_bytes) + SUM(out_bytes)) DESC NULLS LAST LIMIT %s""",
            tuple(params) + tuple(params) + (int(limit),),
        )
        out = []
        for r in rows:
            d = dict(r)
            agent = d["peer_ip_src"]
            plat = platform_for(agent) if platform_for else None
            d["platform"] = plat
            d["port"] = ifindex_to_port(d.get("iface"), plat,
                                        cached=(cached_for(agent) if cached_for else None))
            out.append(d)
        return out

    def port_detail(self, iface, since_minutes=60, agent_ip=None, limit=20):
        """What is actually crossing one interface - the drill-down from
        the per-port view."""
        where, params = self._window(since_minutes, agent_ip, iface=iface)
        rows = self.db.query(
            f"""SELECT ip_src, ip_dst, ip_proto,
                       LEAST(COALESCE(port_src, 65535), COALESCE(port_dst, 65535)) AS port,
                       SUM(bytes) AS bytes, SUM(packets) AS packets
                  FROM sflow_flows WHERE {where}
                 GROUP BY ip_src, ip_dst, ip_proto, port
                 ORDER BY bytes DESC NULLS LAST LIMIT %s""",
            tuple(params) + (int(limit),),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["proto_name"] = proto_name(d.get("ip_proto"))
            d["service"] = service_name(d.get("port"))
            out.append(d)
        return out
