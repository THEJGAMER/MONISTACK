// The fleet map.
//
// What this replaced drew every discovered host as a row of text inside an
// SVG card, which on this fleet meant one column of forty MAC addresses
// running off the bottom of the page, device labels clipped by the circles
// they sat in, and nothing clickable. Eighty-four of its eighty-seven
// edges were hosts learned from a MAC table - an access list, drawn as a
// diagram.
//
// So the two halves are separated. The *fabric* - the devices and the
// links between them - is the only part that is genuinely a graph, and it
// is small (two devices, two links here), so it gets the SVG and is drawn
// large enough to read. Everything hanging off a port is a list, so it is
// Cloudscape components: port chips that wrap, and a details panel that
// fills in when you select something. Nothing is drawn that you cannot
// click, and nothing is drawn that a table would say better.
import React, { useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import Badge from "@cloudscape-design/components/badge";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import TextFilter from "@cloudscape-design/components/text-filter";
import Link from "@cloudscape-design/components/link";
import {
  colorChartsStatusPositive,
  colorChartsStatusHigh,
  colorChartsStatusNeutral,
  fontFamilyMonospace,
} from "@cloudscape-design/design-tokens";

// Only the status colours are taken as values, because they read on a
// light or a dark background alike. Everything else - surfaces, borders,
// text - is drawn in `currentColor` and inherits from the Cloudscape
// container around it. The tokens for those resolve to a `var()` whose
// custom property this build never emits, so they silently fall back to
// their light value: the map this replaced drew white device cards on a
// dark page, and so did this one until it was looked at in dark mode.
const INK = "currentColor";

// The SVG works in viewBox units and scales to whatever width it is
// given, so there is no resize observer and no layout that only works at
// one window size.
const VB_W = 1000;
const DEV_W = 230;
const DEV_H = 76;
const ROW_Y = 70;
const LANE_H = 34;   // vertical space per link between a pair of devices

const UP = colorChartsStatusPositive;
const DOWN = colorChartsStatusHigh;
const UNKNOWN = colorChartsStatusNeutral;

function statusColor(status) {
  if (!status) return UNKNOWN;
  return String(status).toLowerCase() === "up" ? UP : DOWN;
}

function statusType(status) {
  if (!status) return "pending";
  return String(status).toLowerCase() === "up" ? "success" : "error";
}

function mbps(v) {
  return v === null || v === undefined ? null : `${Number(v).toFixed(0)} Mbps`;
}

function isLldp(e) {
  return (e.discovered_via || ["lldp"]).includes("lldp");
}

function viaLabel(e) {
  const via = e.discovered_via || ["lldp"];
  if (via.includes("lldp") && via.includes("mac-table")) return "LLDP + MAC table";
  return via.includes("lldp") ? "LLDP" : "MAC table";
}

function hostLabel(e) {
  return e.remote_ip || e.remote_label || e.remote_chassis_id || "unknown";
}

// --- the fabric ------------------------------------------------------------

function Fabric({ nodes, trunks, selected, onSelect }) {
  const n = Math.max(1, nodes.length);
  const slot = VB_W / n;
  const at = (i) => slot * i + slot / 2;
  const lanesDeep = Math.max(1, ...trunks.map((t) => t.lanes || 1));
  const height = ROW_Y + Math.max(DEV_H, lanesDeep * LANE_H) / 2 + 20;

  const pos = {};
  nodes.forEach((node, i) => {
    pos[node.id] = { x: at(i), y: ROW_Y };
  });

  return (
    <svg
      viewBox={`0 0 ${VB_W} ${height}`}
      width="100%"
      style={{ maxHeight: 260, display: "block" }}
      role="img"
      aria-label="Fleet fabric"
    >
      {trunks.map((t) => {
        const a = pos[t.aId];
        const b = pos[t.bId];
        if (!a || !b) return null;
        // One straight lane per trunk, stacked. Arcs with labels on them
        // collided as soon as a pair had more than one link - which this
        // fleet does: a two-member LAG and a management link side by side.
        const y = a.y + (t.lane - (t.lanes - 1) / 2) * LANE_H;
        const x1 = Math.min(a.x, b.x) + DEV_W / 2;
        const x2 = Math.max(a.x, b.x) - DEV_W / 2;
        const midX = (x1 + x2) / 2;
        const isSel = selected?.type === "trunk" && selected.id === t.id;
        const w = Math.min(x2 - x1 - 16, 7.2 * t.label.length + 16);
        const gapL = midX - w / 2;
        const gapR = midX + w / 2;
        const stroke = statusColor(t.status);
        const width = isSel ? 7 : t.members.length > 1 ? 5 : 2.5;
        return (
          <g key={t.id} style={{ cursor: "pointer" }} onClick={() => onSelect({ type: "trunk", id: t.id })}>
            {/* the label sits in a gap in its own line, so it needs no
                background of its own - which is what made it unreadable
                on a dark page */}
            <line x1={x1} y1={y} x2={gapL} y2={y} stroke={stroke} strokeWidth={width} opacity={isSel ? 1 : 0.85} />
            <line x1={gapR} y1={y} x2={x2} y2={y} stroke={stroke} strokeWidth={width} opacity={isSel ? 1 : 0.85} />
            <line x1={x1} y1={y} x2={x2} y2={y} stroke="transparent" strokeWidth={LANE_H - 4} />
            {isSel && <rect x={gapL - 4} y={y - 13} width={w + 8} height={26} rx={4} fill={INK} fillOpacity={0.1} />}
            <text x={midX} y={y + 4} textAnchor="middle" fontSize="12" fill={INK}>
              {t.label}
            </text>
          </g>
        );
      })}

      {nodes.map((node) => {
        const p = pos[node.id];
        const isSel = selected?.type === "device" && selected.id === node.id;
        const bad = !!node.lldp_error;
        return (
          <g key={node.id} style={{ cursor: "pointer" }} onClick={() => onSelect({ type: "device", id: node.id })}>
            <rect
              x={p.x - DEV_W / 2}
              y={p.y - DEV_H / 2}
              width={DEV_W}
              height={DEV_H}
              rx={8}
              fill={INK}
              fillOpacity={isSel ? 0.1 : 0.04}
              stroke={bad ? DOWN : INK}
              strokeOpacity={bad ? 1 : isSel ? 0.85 : 0.35}
              strokeWidth={isSel ? 3 : 1.5}
            />
            <text x={p.x} y={p.y - 12} textAnchor="middle" fontSize="15" fontWeight="700" fill={INK}>
              {node.name.length > 26 ? `${node.name.slice(0, 25)}…` : node.name}
            </text>
            <text x={p.x} y={p.y + 8} textAnchor="middle" fontSize="13" fontFamily={fontFamilyMonospace} fill={INK} fillOpacity={0.7}>
              {node.host}
            </text>
            <text x={p.x} y={p.y + 26} textAnchor="middle" fontSize="12" fill={INK} fillOpacity={0.7}>
              {[node.make, node.model].filter(Boolean).join(" ") || node.platform}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

// --- port chips ---------------------------------------------------------------

function PortChip({ port, selected, onSelect, dim }) {
  const color = statusColor(port.status);
  return (
    <button
      type="button"
      onClick={() => onSelect({ type: "port", id: port.id })}
      title={`${port.port} - ${port.hosts.length} host(s)`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px",
        borderRadius: 16,
        cursor: "pointer",
        font: "inherit",
        fontSize: 12,
        opacity: dim ? 0.35 : 1,
        color: "inherit",
        background: selected ? "color-mix(in srgb, currentColor 12%, transparent)" : "transparent",
        border: `1px solid color-mix(in srgb, currentColor ${selected ? "70%" : "30%"}, transparent)`,
      }}
    >
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flex: "none" }} />
      <span style={{ fontFamily: fontFamilyMonospace }}>{port.port}</span>
      {port.lagMembers.length > 1 && <Badge>{port.lagMembers.length}×</Badge>}
      {port.hosts.length > 0 && <span style={{ opacity: 0.7 }}>{port.hosts.length}</span>}
    </button>
  );
}

// --- detail --------------------------------------------------------------------

function DeviceDetail({ node, ports, trunks, onOpenConsole }) {
  const hosts = ports.reduce((a, p) => a + p.hosts.length, 0);
  const down = ports.filter((p) => p.status && p.status.toLowerCase() !== "up").length;
  return (
    <SpaceBetween size="m">
      <KeyValuePairs
        columns={4}
        items={[
          { label: "Address", value: node.host },
          { label: "Platform", value: [node.make, node.model].filter(Boolean).join(" ") || node.platform },
          { label: "Ports in use", value: `${ports.length} (${down} not up)` },
          { label: "Hosts seen", value: hosts },
          { label: "Trunks", value: trunks.length ? trunks.map((t) => t.label).join("; ") : "none" },
          {
            label: "LLDP",
            value: node.lldp_error ? (
              <StatusIndicator type="error">{node.lldp_error}</StatusIndicator>
            ) : (
              <StatusIndicator type="success">answering</StatusIndicator>
            ),
          },
        ]}
      />
      <SpaceBetween direction="horizontal" size="xs">
        <Button onClick={() => onOpenConsole && onOpenConsole(node.id)}>Open in Console</Button>
        <Button href={`#/events`} iconAlign="right">
          Events
        </Button>
      </SpaceBetween>
    </SpaceBetween>
  );
}

function TrunkDetail({ trunk, nodeById }) {
  return (
    <SpaceBetween size="m">
      <KeyValuePairs
        columns={3}
        items={[
          { label: "Between", value: `${nodeById[trunk.aId]?.name || trunk.aId} and ${nodeById[trunk.bId]?.name || trunk.bId}` },
          { label: "Members", value: `${trunk.members.length}` },
          { label: "State", value: <StatusIndicator type={statusType(trunk.status)}>{trunk.status || "unknown"}</StatusIndicator> },
        ]}
      />
      <Table
        variant="embedded"
        items={trunk.members}
        columnDefinitions={[
          { id: "a", header: nodeById[trunk.aId]?.name || trunk.aId, cell: (m) => <Box fontFamily="monospace">{m.aPort}{m.aLag ? ` (${m.aLag})` : ""}</Box> },
          { id: "b", header: nodeById[trunk.bId]?.name || trunk.bId, cell: (m) => <Box fontFamily="monospace">{m.bPort}{m.bLag ? ` (${m.bLag})` : ""}</Box> },
          { id: "state", header: "State", cell: (m) => <StatusIndicator type={statusType(m.status)}>{m.status || "unknown"}</StatusIndicator> },
          { id: "in", header: "In", cell: (m) => mbps(m.inMbps) || "-" },
          { id: "out", header: "Out", cell: (m) => mbps(m.outMbps) || "-" },
          { id: "via", header: "Discovered via", cell: (m) => m.via },
        ]}
      />
    </SpaceBetween>
  );
}

function PortDetail({ port, nodeById, onAddDevice }) {
  return (
    <SpaceBetween size="m">
      <KeyValuePairs
        columns={4}
        items={[
          { label: "Device", value: nodeById[port.deviceId]?.name || port.deviceId },
          { label: "Port", value: <Box fontFamily="monospace">{port.port}</Box> },
          { label: "State", value: <StatusIndicator type={statusType(port.status)}>{port.status || "unknown"}</StatusIndicator> },
          { label: "Traffic", value: [mbps(port.inMbps), mbps(port.outMbps)].filter(Boolean).join(" in / ") || "not measured" },
          ...(port.lagMembers.length > 1 ? [{ label: "Bundle members", value: port.lagMembers.join(", ") }] : []),
        ]}
      />
      <Table
        variant="embedded"
        items={port.hosts}
        empty={<Box textAlign="center">Nothing has been seen on this port.</Box>}
        columnDefinitions={[
          { id: "host", header: "Host", cell: (h) => hostLabel(h) },
          { id: "mac", header: "MAC", cell: (h) => <Box fontFamily="monospace">{h.remote_chassis_id || "-"}</Box> },
          { id: "via", header: "Discovered via", cell: (h) => viaLabel(h) },
          {
            id: "add",
            header: "",
            cell: (h) =>
              isLldp(h) && onAddDevice ? (
                <Button variant="inline-link" onClick={() => onAddDevice({ host: h.remote_ip || "", name: h.remote_label || "" })}>
                  Add as device
                </Button>
              ) : (
                ""
              ),
          },
        ]}
      />
    </SpaceBetween>
  );
}

// --- the map ---------------------------------------------------------------------

export default function TopologyMap({ data, showMacTableHosts, onOpenConsole, onAddDevice }) {
  const [selected, setSelected] = useState(null);
  const [filterText, setFilterText] = useState("");

  const { nodes, trunks, portsByDevice, nodeById, portById, trunkById } = useMemo(() => {
    const nodes = data.nodes || [];
    const nodeById = Object.fromEntries(nodes.map((n) => [n.id, n]));
    const edges = data.edges || [];

    // One trunk per device pair *per bundle*. Grouping only by pair was
    // wrong on this fleet: the EX3300 and the S4048 are joined by a
    // two-member LAG and a separate management link, and lumping all
    // three together labelled the management link as part of ae1.
    const byPair = {};
    for (const e of edges.filter((x) => x.kind === "internal")) {
      const [aId, bId] = [e.a.device_id, e.b.device_id];
      const sorted = [aId, bId].sort();
      const flip = aId !== sorted[0];
      const a = flip ? e.b : e.a;
      const b = flip ? e.a : e.b;
      const bundle = a.lag || b.lag ? `${a.lag || "-"}/${b.lag || "-"}` : `single:${a.port}`;
      const key = `${sorted.join("::")}::${bundle}`;
      (byPair[key] = byPair[key] || { aId: a.device_id, bId: b.device_id, pair: sorted.join("::"), members: [] }).members.push({
        aPort: a.port, bPort: b.port, aLag: a.lag, bLag: b.lag,
        status: a.state?.status || b.state?.status,
        inMbps: a.state?.input_mbps ?? b.state?.input_mbps,
        outMbps: a.state?.output_mbps ?? b.state?.output_mbps,
        via: viaLabel(e),
      });
    }
    // Several trunks between the same two devices bow apart so they do
    // not draw on top of each other: 0, then above, then below.
    const perPair = {};
    for (const t of Object.values(byPair)) {
      perPair[t.pair] = (perPair[t.pair] || 0) + 1;
    }
    const laneSeen = {};
    const trunks = Object.entries(byPair).map(([key, t]) => {
      const lane = (laneSeen[t.pair] = (laneSeen[t.pair] || 0) + 1) - 1;
      const statuses = t.members.map((m) => m.status);
      const anyDown = statuses.some((s) => s && s.toLowerCase() !== "up");
      const lags = [...new Set(t.members.flatMap((m) => [m.aLag, m.bLag]).filter(Boolean))];
      const traffic = t.members.reduce((acc, m) => acc + (m.inMbps || 0) + (m.outMbps || 0), 0);
      return {
        ...t, id: key, lane, lanes: perPair[t.pair],
        status: anyDown ? "Down" : statuses.some(Boolean) ? "Up" : null,
        label: lags.length
          ? `${lags.join(" ⟷ ")} · ${t.members.length} × ${traffic ? `${traffic.toFixed(0)} Mbps` : "up"}`
          : `${t.members[0].aPort} ⟷ ${t.members[0].bPort}${traffic ? ` · ${traffic.toFixed(0)} Mbps` : ""}`,
      };
    });

    // Every external edge belongs to a port; a port is the unit people
    // actually think about.
    const ports = {};
    for (const e of edges.filter((x) => x.kind === "external")) {
      if (!showMacTableHosts && !isLldp(e)) continue;
      const id = `${e.device_id}::${e.port}`;
      const p = (ports[id] = ports[id] || {
        id, deviceId: e.device_id, port: e.port, hosts: [],
        lagMembers: e.member_ports && e.member_ports.length > 1 ? e.member_ports : [],
        status: e.state?.status, inMbps: e.state?.input_mbps, outMbps: e.state?.output_mbps,
      });
      p.hosts.push(e);
    }
    const portsByDevice = {};
    for (const p of Object.values(ports)) {
      (portsByDevice[p.deviceId] = portsByDevice[p.deviceId] || []).push(p);
    }
    for (const list of Object.values(portsByDevice)) {
      list.sort((a, b) => b.hosts.length - a.hosts.length || a.port.localeCompare(b.port));
    }
    trunks.sort((x, y2) => y2.members.length - x.members.length);
    return {
      nodes, trunks, portsByDevice, nodeById, portById: ports,
      trunkById: Object.fromEntries(trunks.map((t) => [t.id, t])),
    };
  }, [data, showMacTableHosts]);

  const needle = filterText.trim().toLowerCase();
  const matches = (port) =>
    !needle ||
    port.port.toLowerCase().includes(needle) ||
    (nodeById[port.deviceId]?.name || "").toLowerCase().includes(needle) ||
    port.hosts.some((h) =>
      [h.remote_ip, h.remote_label, h.remote_chassis_id, ...(h.also_known_as || [])]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(needle))
    );

  const matchCount = Object.values(portById).filter(matches).length;

  let detail = null;
  if (selected?.type === "device" && nodeById[selected.id]) {
    detail = {
      title: nodeById[selected.id].name,
      body: (
        <DeviceDetail
          node={nodeById[selected.id]}
          ports={portsByDevice[selected.id] || []}
          trunks={trunks.filter((t) => t.aId === selected.id || t.bId === selected.id)}
          onOpenConsole={onOpenConsole}
        />
      ),
    };
  } else if (selected?.type === "trunk" && trunkById[selected.id]) {
    const t = trunkById[selected.id];
    detail = { title: `${nodeById[t.aId]?.name || t.aId} ⟷ ${nodeById[t.bId]?.name || t.bId}`, body: <TrunkDetail trunk={t} nodeById={nodeById} /> };
  } else if (selected?.type === "port" && portById[selected.id]) {
    const p = portById[selected.id];
    detail = { title: `${nodeById[p.deviceId]?.name || p.deviceId} — ${p.port}`, body: <PortDetail port={p} nodeById={nodeById} onAddDevice={onAddDevice} /> };
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description="Devices and the links between them. Select a device, a link, or a port to see what is on it."
          >
            Fabric
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Fabric nodes={nodes} trunks={trunks} selected={selected} onSelect={setSelected} />
          <SpaceBetween direction="horizontal" size="s">
            <Box color="text-body-secondary">
              <span style={{ display: "inline-block", width: 10, height: 3, background: UP, verticalAlign: "middle", marginRight: 6 }} />
              up
            </Box>
            <Box color="text-body-secondary">
              <span style={{ display: "inline-block", width: 10, height: 3, background: DOWN, verticalAlign: "middle", marginRight: 6 }} />
              down
            </Box>
            <Box color="text-body-secondary">A thick line is a bundle. Its label names the aggregate and its throughput.</Box>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            counter={`(${Object.keys(portById).length})`}
            description="Every port with something on it, busiest first. The number on a chip is how many hosts have been seen there."
            actions={
              <Box>
                <TextFilter
                  filteringText={filterText}
                  onChange={({ detail: d }) => setFilterText(d.filteringText)}
                  filteringPlaceholder="Find a port, IP or MAC..."
                  countText={needle ? `${matchCount} match${matchCount === 1 ? "" : "es"}` : undefined}
                />
              </Box>
            }
          >
            Ports
          </Header>
        }
      >
        <ColumnLayout columns={Math.min(nodes.length || 1, 3)} borders="vertical">
          {nodes.map((node) => {
            const ports = portsByDevice[node.id] || [];
            return (
              <SpaceBetween key={node.id} size="xs">
                <Box variant="awsui-key-label">
                  <Link
                    onFollow={(e) => {
                      e.preventDefault();
                      setSelected({ type: "device", id: node.id });
                    }}
                    href="#"
                  >
                    {node.name}
                  </Link>{" "}
                  <Box variant="span" color="text-body-secondary">
                    {ports.length} port(s)
                  </Box>
                </Box>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {ports.map((p) => (
                    <PortChip
                      key={p.id}
                      port={p}
                      selected={selected?.type === "port" && selected.id === p.id}
                      onSelect={setSelected}
                      dim={needle ? !matches(p) : false}
                    />
                  ))}
                  {ports.length === 0 && <Box color="text-body-secondary">nothing discovered on this device</Box>}
                </div>
              </SpaceBetween>
            );
          })}
        </ColumnLayout>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            actions={selected ? <Button onClick={() => setSelected(null)}>Clear selection</Button> : undefined}
          >
            {detail ? detail.title : "Nothing selected"}
          </Header>
        }
      >
        {detail ? detail.body : <Box color="text-body-secondary">Select a device, a link or a port above.</Box>}
      </Container>
    </SpaceBetween>
  );
}
