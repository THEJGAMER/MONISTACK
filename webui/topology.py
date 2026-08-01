"""Builds a fleet-wide topology graph from each device's own LLDP neighbor
table (parsers.parse_lldp_neighbors_detail / junos_parsers.parse_junos_lldp).

Matching two devices' half-views of the same physical link into one edge
is done by *mutual port-name corroboration*, not chassis ID: device A's
neighbor entry for its own port P names its peer's port as Q, and device
B's neighbor entry for port Q names its peer's port as P - only linked
into one edge when both sides agree. This was chosen over chassis-ID
matching because Dell OS9 has no CLI command that reports its own chassis
ID (confirmed live: `show lldp local-info`/`local-information` both 404 -
only `neighbors`/`statistics` exist under `show lldp`), so there's no
direct way to ask a Dell device "what's your chassis ID" the way Junos's
`show lldp local-information` answers for itself. Mutual naming
corroboration needs nothing from either side but what `show lldp
neighbors(-detail)` already gives, so it works uniformly across platforms
today and any platform added later - as a side benefit, it's also strictly
harder to false-positive on than a bare chassis-ID match would be, since
both sides have to name each other correctly.

Neighbors that don't corroborate with any other known device (an AP, a
NIC, an unmanaged upstream box) still show up - as an "external" leaf node
attached to whichever known device saw them, labeled with whatever the
neighbor advertised (port description or, failing that, its chassis ID).
"""

def _short_dell_port(name):
    from parsers import _short_port_name
    return _short_port_name(name)


def _fetch_lldp_edges(device, output):
    """Runs the appropriate parser for `device.platform` against already-
    fetched raw command output, returning a uniform list of
    {'local_port', 'remote_name', 'remote_chassis_id', 'remote_label'}.
    `remote_name` is the peer's own port name as best identified from this
    device's side (for corroboration); `remote_label` is a human string
    safe to show for an external/unmatched neighbor."""
    if device.platform == "junos":
        import junos_parsers
        edges = []
        for row in junos_parsers.parse_junos_lldp(output):
            remote_name = row["remote_port_description"]
            if remote_name and remote_name[0].isupper() and " " in remote_name:
                # e.g. "TenGigabitEthernet 1/47" - a Dell neighbor spelling
                # its own port out in full, same as its own CLI does.
                remote_name = _short_dell_port(remote_name)
            edges.append({
                "local_port": row["local_port"],
                "parent_interface": row["parent_interface"],
                "remote_name": remote_name,
                "remote_chassis_id": row["remote_chassis_id"],
                "remote_port_id": None,  # not exposed by `show lldp neighbors`'s plain table
                "remote_label": row["remote_system_name"] or remote_name or row["remote_chassis_id"],
            })
        return edges

    import parsers
    edges = []
    for row in parsers.parse_lldp_neighbors_detail(output):
        remote_name = row["remote_port_description"]
        if remote_name and remote_name.endswith((".0", ".1", ".2", ".3")) and "/" in remote_name:
            # e.g. "xe-0/1/3.0" - a Junos neighbor's own interface name,
            # unit suffix stripped to match how the rest of the app spells it.
            remote_name = remote_name.rsplit(".", 1)[0]
        edges.append({
            "local_port": row["local_port"],
            "parent_interface": None,
            "remote_name": remote_name,
            "remote_chassis_id": row["remote_chassis_id"],
            # A multi-port NIC's chassis ID and the specific port ID that's
            # actually on the wire (and therefore in the ARP table) are
            # often two different MACs of the same physical host (confirmed
            # live) - kept separately so ARP lookup can try both.
            "remote_port_id": row["remote_port_id"],
            "remote_label": remote_name or row["remote_chassis_id"],
        })
    return edges


def _is_aggregate_interface(name):
    """True for a port-channel/LAG name (Dell "Po 2", Junos "ae1") as
    opposed to a single physical port."""
    return name.startswith("Po ") or name.startswith("Port-channel") or name.startswith("ae")


def _mac_table_hosts(
    mac_table_by_device_id,
    claimed_ports_by_device,
    lldp_macs_by_device_port,
    lldp_ips_by_device_port,
    mac_to_ip,
    confirmed_uplink_aggregates,
):
    """Second, independent discovery pass: a MAC the switch has learned on
    a port, with no LLDP neighbor to explain it - the "fallback" case
    (confirmed live: this fleet's real unmanaged switch/hub ports show
    several MACs with zero LLDP data at all, since a plain unmanaged
    switch/hub or most consumer/IoT devices never speak LLDP in the first
    place). Skips ports already accounted for by an internal (device-to-
    device) edge, and any MAC an LLDP-discovered node on that same port
    already covers - either directly (same MAC) or indirectly (resolves to
    the same IP an LLDP host on that port already claimed, e.g. a locally-
    administered/virtual sibling MAC of the same NIC - confirmed live:
    `f8:f2:1e:44:17:b0` vs `fa:f2:1e:44:17:b0`, same host, one bit apart).
    MACs on the same port that resolve to the same IP as *each other* are
    also merged, for the same reason. Every distinct host after merging is
    returned - no per-port cap - since hiding real hosts behind a "+N more"
    summary defeats the point of a discovery tool the user is relying on
    to see everything actually attached.

    An aggregate/LAG port (see _is_aggregate_interface) is only skipped
    when `confirmed_uplink_aggregates` says LLDP has actually corroborated
    it as a link to another known device (e.g. the Dell<->Juniper bundle) -
    in that case its mac-address-table entries can be transit traffic
    through the uplink, not directly-attached hosts, and there's no way to
    tell which physical LAG member a given MAC arrived on. An aggregate
    with no such corroboration is trusted the same as a physical port
    (confirmed live: two of this fleet's LAGs - "Po 2"/"Po 3" - are LACP
    bonds terminating on individual Proxmox hosts, not switch uplinks at
    all, and were wrongly hiding dozens of real, directly-attached VM/
    bridge MACs before this distinction existed)."""
    edges = []
    for d_id, rows in (mac_table_by_device_id or {}).items():
        claimed_ports = claimed_ports_by_device.get(d_id, set())
        uplink_aggregates = confirmed_uplink_aggregates.get(d_id, set())
        by_port = {}
        for row in rows:
            port = row["interface"]
            if port in claimed_ports or (_is_aggregate_interface(port) and port in uplink_aggregates):
                continue
            if row["mac"] in lldp_macs_by_device_port.get((d_id, port), set()):
                continue  # already represented by an LLDP-discovered node
            resolved_ip = mac_to_ip.get(row["mac"])
            if resolved_ip and resolved_ip in lldp_ips_by_device_port.get((d_id, port), set()):
                continue  # same host an LLDP node on this port already resolved to
            by_port.setdefault(port, []).append(row["mac"])

        for port, macs in by_port.items():
            # Merge MACs on this port that resolve to the same IP (or, for
            # unresolved MACs, treat each as its own identity - no evidence
            # they're the same host).
            hosts_by_identity = {}
            order = []
            for mac in macs:
                identity = mac_to_ip.get(mac) or mac
                if identity not in hosts_by_identity:
                    order.append(identity)
                    hosts_by_identity[identity] = {"ip": mac_to_ip.get(mac), "macs": []}
                hosts_by_identity[identity]["macs"].append(mac)

            for identity in order:
                host = hosts_by_identity[identity]
                edges.append({
                    "kind": "external",
                    "device_id": d_id,
                    "port": port,
                    "member_ports": [port],
                    "remote_chassis_id": host["macs"][0],
                    "remote_ip": host["ip"],
                    "remote_label": host["ip"] or host["macs"][0],
                    "also_known_as": host["macs"][1:],
                    "discovered_via": ["mac-table"],
                })
    return edges


def merge_mac_to_ip(arp_rows_by_device_id):
    """Merges every device's own ARP table into one {mac: ip} map. Any
    device's ARP table can resolve a MAC seen by any other device - ARP
    reflects L3 neighbors on the shared LAN, not something specific to
    whichever switch happened to ask - so this doesn't need to track which
    device an entry came from, just build one shared lookup. Later entries
    don't overwrite earlier ones for the same MAC (should always agree
    anyway; first-seen is as good a tiebreak as any)."""
    mac_to_ip = {}
    for rows in arp_rows_by_device_id.values():
        for row in rows:
            mac_to_ip.setdefault(row["mac"], row["ip"])
    return mac_to_ip


def build_topology(
    devices,
    raw_lldp_by_device_id,
    errors_by_device_id=None,
    mac_to_ip=None,
    mac_table_by_device_id=None,
    port_channel_members_by_device_id=None,
):
    """`raw_lldp_by_device_id`: {device_id: raw command output}, already
    fetched over SSH by the caller (app.py) - kept out of this module so
    it stays testable without a live SSH session. `errors_by_device_id`:
    {device_id: error message} for devices whose LLDP fetch failed (e.g.
    unreachable) - still included as a node, flagged `lldp_error`.
    `mac_to_ip`: optional {mac: ip} from merge_mac_to_ip() - resolves an
    external neighbor's bare MAC into a real IP address when the fleet's
    own ARP tables happen to know it (confirmed live: they usually do, for
    anything actually on the LAN), which is a lot more useful at a glance
    than a MAC address nobody recognizes. `mac_table_by_device_id`:
    optional {device_id: [{'mac','interface'}, ...]} from
    parsers.parse_mac_address_table / junos_parsers.parse_ethernet_switching_table
    - a second, independent discovery source (see _mac_table_hosts) for
    hosts that never speak LLDP at all, so they'd otherwise be invisible.
    `port_channel_members_by_device_id`: optional {device_id: {member_port:
    'Po N'}} from parsers.parse_port_channel_brief - fills in an edge's
    `lag` on platforms whose LLDP output doesn't name the parent
    port-channel itself (Dell OS9; Junos already does via `show lldp
    neighbors`), which is what lets a Dell port-channel be recognized as a
    confirmed inter-switch uplink (see _mac_table_hosts) instead of always
    looking like a bare, un-aggregated physical port.

    Every edge carries `discovered_via` (`["lldp"]`, `["mac-table"]`, or
    `["lldp", "mac-table"]` when both agree) - this is a multi-source
    discovery graph, not just an LLDP one, and callers/UI should be able
    to tell which kind of evidence (or how much of it) backs a given edge.

    Returns {'nodes': [...], 'edges': [...]}. Internal edges (both ends are
    known devices) have 'a'/'b' endpoints; external edges (peer is
    unmatched) have a single 'device'/'port' plus a 'remote_label'.
    """
    errors_by_device_id = errors_by_device_id or {}
    mac_to_ip = mac_to_ip or {}
    mac_table_by_device_id = mac_table_by_device_id or {}
    port_channel_members_by_device_id = port_channel_members_by_device_id or {}

    def _lag_for(d_id, port, parent_interface):
        # LLDP already names the parent aggregate directly on some
        # platforms (Junos's `ae1`) - only fall back to the port-channel
        # membership map (Dell OS9, which LLDP never names it on) when LLDP
        # didn't already tell us.
        if parent_interface:
            return parent_interface
        return port_channel_members_by_device_id.get(d_id, {}).get(port)

    edges_by_device = {
        d.id: _fetch_lldp_edges(d, raw_lldp_by_device_id[d.id])
        for d in devices
        if d.id in raw_lldp_by_device_id
    }

    nodes = [
        {
            "id": d.id,
            "name": d.name,
            "host": d.host,
            "platform": d.platform,
            "make": d.make,
            "model": d.model,
            "lldp_error": errors_by_device_id.get(d.id),
        }
        for d in devices
        # Only devices LLDP was actually attempted against (the caller
        # skips platforms with no LLDP concept, e.g. OPNsense) - otherwise
        # every device ever configured would clutter the diagram with a
        # permanently-isolated node it was never meant to include.
        if d.id in raw_lldp_by_device_id or d.id in errors_by_device_id
    ]

    edges = []
    claimed = set()  # (device_id, local_port) already used in a confirmed internal edge
    device_ids = list(edges_by_device)
    for i, a_id in enumerate(device_ids):
        for a_edge in edges_by_device[a_id]:
            key_a = (a_id, a_edge["local_port"])
            if key_a in claimed:
                continue
            match = None
            for b_id in device_ids:
                if b_id == a_id:
                    continue
                for b_edge in edges_by_device[b_id]:
                    key_b = (b_id, b_edge["local_port"])
                    if key_b in claimed:
                        continue
                    if a_edge["remote_name"] == b_edge["local_port"] and b_edge["remote_name"] == a_edge["local_port"]:
                        match = (b_id, b_edge)
                        break
                if match:
                    break
            if match:
                b_id, b_edge = match
                claimed.add(key_a)
                claimed.add((b_id, b_edge["local_port"]))
                edges.append({
                    "kind": "internal",
                    "a": {
                        "device_id": a_id,
                        "port": a_edge["local_port"],
                        "lag": _lag_for(a_id, a_edge["local_port"], a_edge["parent_interface"]),
                    },
                    "b": {
                        "device_id": b_id,
                        "port": b_edge["local_port"],
                        "lag": _lag_for(b_id, b_edge["local_port"], b_edge["parent_interface"]),
                    },
                    # Device-to-device adjacency is only ever established
                    # via mutual LLDP corroboration - a MAC-table entry
                    # alone can't independently confirm two switches are
                    # physically adjacent the way naming each other does.
                    "discovered_via": ["lldp"],
                })

    # A single physical port can legitimately report more than one LLDP
    # neighbor entry for the very same host (confirmed live: a hypervisor
    # advertising several of its own internal bridge interfaces - "nic0",
    # "fwln115i0", "fwln108i0" - all under one chassis ID down one cable).
    # Grouped by (port, chassis ID) and merged into a single node instead
    # of one overlapping, indistinguishable node per alias - that's a
    # readability problem, not new information.
    #
    # When the local port is itself a LAG member (confirmed live: a
    # Proxmox host bonds two NICs into the same LACP port-channel, and its
    # LLDP frames show up individually on each Dell member port - "Te
    # 1/39" and "Te 1/40" - carrying the same chassis ID), the port used
    # for grouping is the *logical* port-channel name ("Po 2"), not each
    # physical member - otherwise the same host shows up as two separate,
    # duplicate-looking rows. This also aligns it with the switch's own
    # mac-address-table, which already reports that bundle's other MACs
    # under the aggregate name directly, not per physical member.
    external_groups = {}
    member_ports_by_group = {}
    for d_id, d_edges in edges_by_device.items():
        members = port_channel_members_by_device_id.get(d_id, {})
        for edge in d_edges:
            key = (d_id, edge["local_port"])
            if key in claimed:
                continue
            logical_port = members.get(edge["local_port"], edge["local_port"])
            group_key = (d_id, logical_port, edge["remote_chassis_id"])
            external_groups.setdefault(group_key, []).append(edge)
            member_ports_by_group.setdefault(group_key, set()).add(edge["local_port"])

    def _resolve_group_ip(chassis_id, group):
        remote_ip = mac_to_ip.get(chassis_id.lower())
        if remote_ip is None:
            for edge in group:
                if edge.get("remote_port_id") and mac_to_ip.get(edge["remote_port_id"].lower()):
                    # A multi-port NIC's chassis ID is sometimes a
                    # different MAC than the one actually on the wire for
                    # this specific link (confirmed live) - the port ID is
                    # the one ARP actually knows about in that case.
                    remote_ip = mac_to_ip[edge["remote_port_id"].lower()]
                    break
        return remote_ip

    # A single port can also legitimately report *two different chassis
    # IDs* that both resolve to the same IP address (confirmed live: a
    # dual-function NIC card emitting LLDP under two chassis IDs down what
    # the fleet's own ARP tables agree is one address) - same host, not two.
    # Re-key each (port, chassis ID) group by its resolved IP (falling back
    # to chassis ID when unresolved, since two unresolved chassis IDs on
    # one port can't be confirmed to be the same host) and merge.
    merged_groups = {}
    for (d_id, port, chassis_id), group in external_groups.items():
        remote_ip = _resolve_group_ip(chassis_id, group)
        merge_key = (d_id, port, remote_ip or chassis_id)
        entry = merged_groups.setdefault(
            merge_key, {"chassis_ids": [], "groups": [], "remote_ip": remote_ip, "member_ports": set()}
        )
        entry["chassis_ids"].append(chassis_id)
        entry["groups"].append(group)
        entry["member_ports"] |= member_ports_by_group[(d_id, port, chassis_id)]

    # Ports already spoken for by an internal edge, and every MAC an LLDP
    # group already accounts for per (device, port) - both needed by the
    # MAC-table discovery pass below, to skip transit/uplink ports and
    # avoid drawing a second node for a host LLDP already found.
    claimed_ports_by_device = {}
    for e in edges:
        claimed_ports_by_device.setdefault(e["a"]["device_id"], set()).add(e["a"]["port"])
        claimed_ports_by_device.setdefault(e["b"]["device_id"], set()).add(e["b"]["port"])

    # Aggregates LLDP has actually corroborated as a link to another known
    # device - the only aggregates the MAC-table pass should treat as
    # possible transit/uplink bundles rather than ordinary host-facing
    # ports (see _mac_table_hosts).
    confirmed_uplink_aggregates = {}
    for e in edges:
        if e["a"]["lag"]:
            confirmed_uplink_aggregates.setdefault(e["a"]["device_id"], set()).add(e["a"]["lag"])
        if e["b"]["lag"]:
            confirmed_uplink_aggregates.setdefault(e["b"]["device_id"], set()).add(e["b"]["lag"])

    lldp_macs_by_device_port = {}
    for (d_id, port, chassis_id), group in external_groups.items():
        macs = lldp_macs_by_device_port.setdefault((d_id, port), set())
        macs.add(chassis_id.lower())
        for edge in group:
            if edge.get("remote_port_id"):
                macs.add(edge["remote_port_id"].lower())
    mac_table_macs_by_device_port = {}
    for d_id, rows in mac_table_by_device_id.items():
        for row in rows:
            mac_table_macs_by_device_port.setdefault((d_id, row["interface"]), set()).add(row["mac"])

    # Every IP an LLDP-discovered host already occupies per (device, port) -
    # used below so the MAC-table fallback pass doesn't draw a second,
    # unlabeled node for a host LLDP already placed there (e.g. a locally-
    # administered/virtual sibling MAC of the same NIC resolving to the
    # same address the LLDP entry already resolved to).
    lldp_ips_by_device_port = {}

    for (d_id, port, identity), entry in merged_groups.items():
        groups = entry["groups"]
        remote_ip = entry["remote_ip"]
        chassis_ids = entry["chassis_ids"]
        primary_chassis_id = chassis_ids[0]
        primary_group = groups[0]
        labels = [e["remote_label"] for g in groups for e in g if e["remote_label"]]
        label = labels[0] if labels else primary_chassis_id
        also_known_as = list(dict.fromkeys(labels[1:] + chassis_ids[1:]))
        discovered_via = ["lldp"]
        lldp_macs = lldp_macs_by_device_port[(d_id, port)]
        if mac_table_macs_by_device_port.get((d_id, port), set()) & lldp_macs:
            discovered_via.append("mac-table")
        if remote_ip:
            lldp_ips_by_device_port.setdefault((d_id, port), set()).add(remote_ip)
        edges.append({
            "kind": "external",
            "device_id": d_id,
            "port": port,
            # `port` is already the logical port-channel name when this
            # host was reached over a LAG (see external_groups above) - the
            # physical members that carried it are kept here for detail/
            # tooltips, not as a separate "lag" field to avoid saying the
            # same thing twice (e.g. "Po 2 (Po 2)").
            "member_ports": sorted(entry["member_ports"]),
            "remote_chassis_id": primary_chassis_id,
            "remote_ip": remote_ip,
            "remote_label": label,
            "also_known_as": also_known_as,
            "discovered_via": discovered_via,
        })

    edges += _mac_table_hosts(
        mac_table_by_device_id,
        claimed_ports_by_device,
        lldp_macs_by_device_port,
        lldp_ips_by_device_port,
        mac_to_ip,
        confirmed_uplink_aggregates,
    )

    return {"nodes": nodes, "edges": edges}


def edge_signature(edge):
    """The *structural* identity of an edge - which ports on which devices
    are connected - with no state/utilization in it, since those change
    constantly and aren't what "did the wiring change" means. Internal
    edges are endpoint-order-independent (sorted) so the same physical
    link always produces the same signature regardless of which side
    happened to be iterated as `a` in build_topology().

    Returns None for an edge with no LLDP evidence behind it at all (a
    pure MAC-table host, or its overflow summary) - baseline drift
    tracking deliberately only follows LLDP-backed edges. MAC-table
    discovery exists for *visibility* into hosts LLDP can't see (see
    _mac_table_hosts), not as a source of "normal state" truth: a hub
    port's guest laptop coming and going would otherwise flag as constant
    false "infra changed" drift, defeating the point of a baseline."""
    if "lldp" not in edge.get("discovered_via", ["lldp"]):
        return None
    if edge["kind"] == "internal":
        endpoints = sorted([[edge["a"]["device_id"], edge["a"]["port"]], [edge["b"]["device_id"], edge["b"]["port"]]])
        return {"kind": "internal", "endpoints": endpoints}
    return {
        "kind": "external",
        "device_id": edge["device_id"],
        "port": edge["port"],
        "remote_chassis_id": edge["remote_chassis_id"],
    }


def diff_against_baseline(edges, baseline_signatures):
    """Compares the live edge list against a saved baseline (a list of
    `edge_signature()` dicts, from topology_store.TopologyStore.get()).
    Returns {'added': [...], 'removed': [...]} - edges that exist now but
    weren't in the baseline, and vice versa. None (not diffed at all) if
    there's no baseline yet."""
    if baseline_signatures is None:
        return None
    import json

    live = {}
    for e in edges:
        sig = edge_signature(e)
        if sig is not None:
            live[json.dumps(sig, sort_keys=True)] = sig
    baseline = {json.dumps(s, sort_keys=True): s for s in baseline_signatures}
    added = [s for k, s in live.items() if k not in baseline]
    removed = [s for k, s in baseline.items() if k not in live]
    return {"added": added, "removed": removed}
