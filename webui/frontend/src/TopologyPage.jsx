import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";
import Box from "@cloudscape-design/components/box";
import Modal from "@cloudscape-design/components/modal";
import Toggle from "@cloudscape-design/components/toggle";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";

import { getTopology, saveTopologyBaseline, acceptTopologyDrift, clearTopologyBaseline } from "./api.js";

const PORT_PAGE_SIZE = 15;

// Classic hierarchical "network diagram" layout (the kind you'd draw in
// Visio/draw.io for a small LAN): devices in a row at the top connected
// by straight trunk lines, with each device's own neighbors in a grid
// underneath it, joined by right-angle ("elbow") connectors instead of
// spokes radiating from a circle. Grid + orthogonal routing reads as
// "network topology diagram" at a glance in a way a radial layout doesn't,
// and it's far more space-efficient for a device with a lot of neighbors -
// a grid grows in rows, not circumference.
const MIN_LANE_WIDTH = 260;
const LANE_GAP = 60;
const DEVICE_Y = 66;
const DEVICE_NODE_R = 40;
const ROW_GAP_AFTER_DEVICE = 56;
// External neighbors render as one card per *local port*, not one shape
// per discovered host - a port with several hosts on it (a hub behind an
// unmanaged switch, a dual-homed NIC with two LLDP chassis IDs on one
// wire) is one thing to look at, not several loose chips/dots that can
// overlap or read as unrelated when they're all actually on the same
// port. Card height is variable (driven by how many hosts are on that
// port) and packed into a small masonry grid under each device.
const PORT_CARD_W = 230;
const PORT_CARD_GAP_X = 18;
const PORT_CARD_GAP_Y = 14;
const PORT_CARD_COLS = 2;
const PORT_HEADER_H = 20;
// Every host row shows both its IP address and its MAC address (two lines)
// whenever both are known, not just whichever one is more "recognizable" -
// the user needs the MAC too (to cross-reference the switch's own
// mac-address-table output), not just an IP.
const PORT_ROW_H = 28;
const PORT_PADDING = 6;
const AUTO_REFRESH_MS = 30_000;

const COLOR_UP = "#037f0c";
const COLOR_DOWN = "#d13212";
const COLOR_UNKNOWN = "#9aa5b1";
const COLOR_DEVICE_STROKE = "#0972d3";
const COLOR_DEVICE_FILL = "#f0f6ff";
const COLOR_DEVICE_ERROR_STROKE = "#d13212";
const COLOR_DEVICE_ERROR_FILL = "#fdf2f1";
const COLOR_TEXT = "#16191f";
const COLOR_TEXT_SECONDARY = "#68737d";

// Right-angle "elbow" connector (drop - step - drop), same convention as
// an org-chart/tree diagram: down from the parent, across at the
// midpoint, down into the top of the child.
function elbowPath(fromX, fromY, toX, toY) {
  const midY = fromY + (toY - fromY) / 2;
  return `M ${fromX} ${fromY} L ${fromX} ${midY} L ${toX} ${midY} L ${toX} ${toY}`;
}

// A port card's connector routes through a shared vertical "gutter"
// between the card columns (rather than a straight elbow to each card),
// so the line for a card further down a column never has to pass over -
// and visually strike through - the text of a card stacked above it in
// the same column.
function portConnectorPath(fromX, fromY, toX, toY, corridorX) {
  const stubY = fromY + 14;
  return `M ${fromX} ${fromY} L ${fromX} ${stubY} L ${corridorX} ${stubY} L ${corridorX} ${toY} L ${toX} ${toY}`;
}

// Packs each device's port cards into a small masonry grid (greedy:
// each new card goes into whichever column is currently shortest) since
// card height varies with how many hosts are on that port - a fixed grid
// would either waste space or clip a busy port's card.
function layoutPortCards(groups, cols, cardW, gapX, gapY, laneCenterX, startY) {
  const totalW = cols * cardW + (cols - 1) * gapX;
  const leftX = laneCenterX - totalW / 2;
  const colHeights = new Array(cols).fill(startY);
  const positions = {};
  groups.forEach((g) => {
    let col = 0;
    for (let i = 1; i < cols; i++) if (colHeights[i] < colHeights[col]) col = i;
    const rowCount = Math.max(1, g.lldp.length + g.mac.length);
    const height = PORT_HEADER_H + rowCount * PORT_ROW_H + PORT_PADDING * 2;
    positions[g.port] = { x: leftX + col * (cardW + gapX), y: colHeights[col], width: cardW, height };
    colHeights[col] += height + gapY;
  });
  return { positions, bottom: Math.max(...colHeights) };
}

// Every internal edge names a Mbps rate only for OS9 (Junos has no
// per-interface rate data - see status_poller.py's _poll_once_junos) -
// treated as "no data", never shown as zero traffic.
function formatMbps(v) {
  return v === null || v === undefined ? null : `${v.toFixed(1)} Mbps`;
}

function edgeStatusInfo(states) {
  const known = states.filter((s) => s && s.status);
  if (known.length === 0) return { type: "info", text: "unknown", color: COLOR_UNKNOWN };
  if (known.some((s) => s.status !== "Up")) return { type: "error", text: "down", color: COLOR_DOWN };
  return { type: "success", text: "up", color: COLOR_UP };
}

// An IP address (resolved from the fleet's own ARP tables - see
// topology.py's merge_mac_to_ip) is a lot more recognizable at a glance
// than a bare MAC or a generic NIC description, so it's the headline text
// for an external neighbor whenever it's known; the LLDP-advertised label
// becomes the secondary line instead of disappearing.
function externalHeadline(e) {
  return e.remote_ip || e.remote_label;
}
function externalSubline(e) {
  return e.remote_ip && e.remote_label !== e.remote_ip ? e.remote_label : null;
}

// The MAC to display alongside the IP for a host row - remote_chassis_id
// is always a MAC address for both LLDP- and MAC-table-discovered edges
// (see topology.py), never the human label.
function externalMac(e) {
  return e.remote_chassis_id;
}

// A single local port can have more than one distinct external neighbor
// (confirmed live - two different NICs' chassis IDs seen on one port,
// presumably through an unmanaged switch/hub), so port alone isn't a
// unique key the way it is for internal edges.
function externalKey(e) {
  return `${e.device_id}:${e.port}:${e.remote_chassis_id}`;
}

function isLldpBacked(e) {
  return e.discovered_via?.includes("lldp");
}

function discoveredViaLabel(e) {
  const via = e.discovered_via || ["lldp"];
  if (via.includes("lldp") && via.includes("mac-table")) return "LLDP + MAC table";
  if (via.includes("lldp")) return "LLDP";
  return "MAC table";
}

function describeSignature(sig, nodeById) {
  const name = (id) => nodeById[id]?.name || id;
  if (sig.kind === "internal") {
    const [[d1, p1], [d2, p2]] = sig.endpoints;
    return `${name(d1)} — ${p1} ⟷ ${name(d2)} — ${p2}`;
  }
  return `${name(sig.device_id)} — ${sig.port} ⟷ ${sig.remote_chassis_id}`;
}

function sigKey(sig) {
  return JSON.stringify(sig, Object.keys(sig).sort());
}

export default function TopologyPage({ pushFlash, onOpenConsole, onAddDevice }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [hovered, setHovered] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [showMacTableHosts, setShowMacTableHosts] = useState(true);
  const [confirmAction, setConfirmAction] = useState(null); // "relearn" | "forget" | null
  const [busyAction, setBusyAction] = useState(false);
  const [expandedPortIds, setExpandedPortIds] = useState([]);
  const [portFilterText, setPortFilterText] = useState("");
  const [portPage, setPortPage] = useState(1);
  const prevStates = useRef(null); // edge key -> status, from the previous fetch (for flap detection)
  const firstLoad = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await getTopology();
      if (!firstLoad.current && prevStates.current) {
        const nextStates = {};
        const flap = (key, label, status) => {
          nextStates[key] = status;
          const prev = prevStates.current[key];
          if (prev === undefined || !status || prev === status) return;
          if (status === "Up" && prev !== "Up") pushFlash("success", `${label} is back up.`);
          else if (status !== "Up" && prev === "Up") pushFlash("error", `${label} went down.`);
        };
        for (const e of next.edges) {
          if (e.kind === "internal") {
            const key = `${e.a.device_id}:${e.a.port}`;
            flap(key, `Link ${e.a.port} ⟷ ${e.b.port}`, e.a.state?.status);
          } else {
            flap(`${e.device_id}:${e.port}`, `Link ${e.port} ⟷ ${e.remote_label}`, e.state?.status);
          }
        }
        prevStates.current = nextStates;
      } else {
        const initial = {};
        for (const e of next.edges) {
          if (e.kind === "internal") initial[`${e.a.device_id}:${e.a.port}`] = e.a.state?.status;
          else initial[`${e.device_id}:${e.port}`] = e.state?.status;
        }
        prevStates.current = initial;
      }
      firstLoad.current = false;
      setData(next);
    } catch (e) {
      pushFlash("error", `Could not load topology: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }, [pushFlash]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(load, AUTO_REFRESH_MS);
    return () => clearInterval(id);
  }, [autoRefresh, load]);

  const layout = useMemo(() => {
    if (!data) return null;
    const { nodes, edges } = data;
    const nodeById = Object.fromEntries(nodes.map((n) => [n.id, n]));

    // Group every external edge (LLDP-backed or MAC-table-only) by its
    // local port - the diagram's basic unit is "what's on this port", not
    // one shape per discovered host (see layoutPortCards above).
    const portGroupsByDevice = {};
    for (const e of edges) {
      if (e.kind !== "external") continue;
      const perDevice = (portGroupsByDevice[e.device_id] ||= {});
      const g = (perDevice[e.port] ||= { port: e.port, memberPorts: e.member_ports, lldp: [], mac: [] });
      if (isLldpBacked(e)) g.lldp.push(e);
      else g.mac.push(e);
    }

    // Each device's port-card grid is laid out in its own 0-based space
    // first (its lane width depends on how many card columns it actually
    // needs), then lanes are placed left-to-right by their own width - a
    // device with many neighbors gets a wider lane instead of everyone
    // being squeezed into one fixed-width slot.
    const perDeviceLayout = {};
    nodes.forEach((n) => {
      const allGroups = Object.values(portGroupsByDevice[n.id] || {}).sort((a, b) => a.port.localeCompare(b.port));
      const groups = allGroups.filter((g) => g.lldp.length > 0 || (showMacTableHosts && g.mac.length > 0));
      const cols = Math.max(1, Math.min(PORT_CARD_COLS, groups.length));
      const laneContentWidth = cols * PORT_CARD_W + (cols - 1) * PORT_CARD_GAP_X;
      const laneWidth = Math.max(MIN_LANE_WIDTH, laneContentWidth + 40);
      const { positions, bottom } = layoutPortCards(
        groups, cols, PORT_CARD_W, PORT_CARD_GAP_X, PORT_CARD_GAP_Y, laneWidth / 2, DEVICE_Y + ROW_GAP_AFTER_DEVICE
      );
      perDeviceLayout[n.id] = { groups, positions, bottom, laneWidth };
    });

    const width = nodes.reduce((sum, n) => sum + perDeviceLayout[n.id].laneWidth, 0) + LANE_GAP * Math.max(0, nodes.length - 1);

    // Devices sit in a single row, each centered in its own (variable-
    // width) lane, joined by straight trunk lines - the classic network-
    // diagram convention, not a circle.
    const positions = {};
    let cursorX = 0;
    nodes.forEach((n) => {
      const laneWidth = perDeviceLayout[n.id].laneWidth;
      positions[n.id] = { x: cursorX + laneWidth / 2, y: DEVICE_Y };
      cursorX += laneWidth + LANE_GAP;
    });

    const portPositions = {};
    let maxContentBottom = DEVICE_Y + DEVICE_NODE_R;
    nodes.forEach((n) => {
      const laneLeftX = positions[n.id].x - perDeviceLayout[n.id].laneWidth / 2;
      Object.entries(perDeviceLayout[n.id].positions).forEach(([port, pos]) => {
        portPositions[`${n.id}:${port}`] = { ...pos, x: pos.x + laneLeftX };
      });
      maxContentBottom = Math.max(maxContentBottom, perDeviceLayout[n.id].bottom);
    });

    const height = maxContentBottom + 30;

    return { nodeById, positions, portPositions, portGroupsByDevice: perDeviceLayout, height, width };
  }, [data, showMacTableHosts]);

  async function handleRelearn() {
    setBusyAction(true);
    try {
      await saveTopologyBaseline();
      pushFlash("success", "Baseline saved from the current live topology.");
      setConfirmAction(null);
      await load();
    } catch (e) {
      pushFlash("error", `Could not save baseline: ${e.message}`);
    } finally {
      setBusyAction(false);
    }
  }

  async function handleForget() {
    setBusyAction(true);
    try {
      await clearTopologyBaseline();
      pushFlash("success", "Baseline cleared.");
      setConfirmAction(null);
      await load();
    } catch (e) {
      pushFlash("error", `Could not clear baseline: ${e.message}`);
    } finally {
      setBusyAction(false);
    }
  }

  async function handleAccept(added, removed) {
    try {
      await acceptTopologyDrift(added, removed);
      pushFlash("success", "Baseline updated.");
      await load();
    } catch (e) {
      pushFlash("error", `Could not update baseline: ${e.message}`);
    }
  }

  if (loading && !data) return <Spinner size="large" />;
  if (!data || !layout) return null;

  const { nodes, edges, drift, baseline, lag_health: lagHealth } = data;
  const internalEdges = edges.filter((e) => e.kind === "internal");
  const lldpExternalEdges = edges.filter((e) => e.kind === "external" && isLldpBacked(e));
  const macTableExternalEdges = edges.filter((e) => e.kind === "external" && !isLldpBacked(e));
  const externalEdges = showMacTableHosts ? [...lldpExternalEdges, ...macTableExternalEdges] : lldpExternalEdges;
  const macTableHostCount = macTableExternalEdges.length;

  // Multiple physical links between the same two devices (e.g. a LAG's
  // members plus a separate out-of-band management link) all share the
  // same two node positions - drawn as straight lines they'd sit exactly
  // on top of each other, hiding that there's more than one. Curving each
  // one by a different amount, offset perpendicular to the a-b line, keeps
  // them visually distinct.
  const pairGroups = {};
  internalEdges.forEach((e) => {
    const pairKey = [e.a.device_id, e.b.device_id].sort().join("|");
    (pairGroups[pairKey] ||= []).push(e);
  });
  const nodesWithErrors = nodes.filter((n) => n.lldp_error);
  const degradedBundles = (lagHealth || []).filter((l) => l.degraded);

  // The "Ports" table's unit of organization is the *local port* (the AWS
  // console convention: one resource row that expands to reveal the
  // things attached to it), not one row per discovered host - a busy port
  // with two dozen MAC-table hosts is one port with a "24 hosts" summary
  // row, expandable to the individual IP/MAC rows, rather than 24 flat
  // table rows with the same port name repeated down the left column.
  const utilizationOf = (state) =>
    [formatMbps(state?.input_mbps), formatMbps(state?.output_mbps)].filter(Boolean).join(" in / ") || "—";

  const externalGroups = {};
  externalEdges.forEach((e) => {
    const key = `${e.device_id}:${e.port}`;
    (externalGroups[key] ||= { device_id: e.device_id, port: e.port, memberPorts: e.member_ports, hosts: [] }).hosts.push(e);
  });

  // A LAG's individual member links (e.g. a 2x10G bundle) are the same
  // logical connection between the same two devices, not two unrelated
  // links - grouped into one summary row (mirroring the external-host
  // "N hosts" grouping above) rather than repeating the same two device
  // names down the table once per member port.
  const internalRow = (e) => ({
    id: `internal:${e.a.device_id}:${e.a.port}-${e.b.device_id}:${e.b.port}`,
    port: `${layout.nodeById[e.a.device_id]?.name} — ${e.a.port}${e.a.lag ? ` (${e.a.lag})` : ""}`,
    host: `${layout.nodeById[e.b.device_id]?.name} — ${e.b.port}${e.b.lag ? ` (${e.b.lag})` : ""}`,
    mac: "—",
    status: edgeStatusInfo([e.a.state, e.b.state]),
    discoveredVia: discoveredViaLabel(e),
    utilization: utilizationOf(e.a.state),
    children: [],
  });
  const internalGroups = {};
  const internalSingles = [];
  internalEdges.forEach((e) => {
    if (!e.a.lag) {
      internalSingles.push(e);
      return;
    }
    const key = `${e.a.device_id}:${e.a.lag}:${e.b.device_id}`;
    (internalGroups[key] ||= { a: e.a, b: e.b, members: [] }).members.push(e);
  });

  const internalItems = [
    ...internalSingles.map(internalRow),
    ...Object.values(internalGroups).map((g) => {
      if (g.members.length === 1) return internalRow(g.members[0]);
      const ins = g.members.map((e) => e.a.state?.input_mbps).filter((v) => v != null);
      const outs = g.members.map((e) => e.a.state?.output_mbps).filter((v) => v != null);
      const utilization = [
        ins.length ? `${ins.reduce((a, b) => a + b, 0).toFixed(1)} Mbps in` : null,
        outs.length ? `${outs.reduce((a, b) => a + b, 0).toFixed(1)} Mbps out` : null,
      ].filter(Boolean).join(" / ") || "—";
      return {
        id: `internal-group:${g.a.device_id}:${g.a.lag}:${g.b.device_id}`,
        port: `${layout.nodeById[g.a.device_id]?.name} — ${g.a.lag} (${g.members.length} members)`,
        host: `${layout.nodeById[g.b.device_id]?.name} — ${g.b.lag || "?"} (${g.members.length} members)`,
        mac: "—",
        status: edgeStatusInfo(g.members.flatMap((e) => [e.a.state, e.b.state])),
        discoveredVia: [...new Set(g.members.map(discoveredViaLabel))].join(" / "),
        utilization,
        children: g.members.map(internalRow),
      };
    }),
  ];

  const portItems = [
    ...internalItems,
    ...Object.values(externalGroups).map((g) => {
      const portLabel = `${layout.nodeById[g.device_id]?.name} — ${g.port}${g.memberPorts?.length > 1 ? ` (${g.memberPorts.join(", ")})` : ""}`;
      const status = edgeStatusInfo([g.hosts[0].state]);
      const toChildRow = (e) => ({
        id: `ext:${g.device_id}:${g.port}:${externalKey(e)}`,
        port: "",
        host: `${e.remote_ip || "no IP"} (not managed here)`,
        mac: externalMac(e),
        status: edgeStatusInfo([e.state]),
        discoveredVia: discoveredViaLabel(e),
        utilization: utilizationOf(e.state),
        children: [],
      });
      if (g.hosts.length === 1) {
        return { ...toChildRow(g.hosts[0]), id: `ext:${g.device_id}:${g.port}`, port: portLabel };
      }
      return {
        id: `ext:${g.device_id}:${g.port}`,
        port: portLabel,
        host: `${g.hosts.length} hosts`,
        mac: "—",
        status,
        discoveredVia: [...new Set(g.hosts.map(discoveredViaLabel))].join(" / "),
        utilization: utilizationOf(g.hosts[0].state),
        children: g.hosts.map(toChildRow),
      };
    }),
  ];

  const portMatchesFilter = (item, needle) =>
    [item.port, item.host, item.mac, item.discoveredVia].some((v) => v?.toLowerCase().includes(needle));
  const filteredPortItems = !portFilterText.trim()
    ? portItems
    : portItems.filter((item) => {
        const needle = portFilterText.trim().toLowerCase();
        return portMatchesFilter(item, needle) || item.children.some((c) => portMatchesFilter(c, needle));
      });
  const portPagesCount = Math.max(1, Math.ceil(filteredPortItems.length / PORT_PAGE_SIZE));
  const clampedPortPage = Math.min(portPage, portPagesCount);
  const portPageItems = filteredPortItems.slice(
    (clampedPortPage - 1) * PORT_PAGE_SIZE,
    clampedPortPage * PORT_PAGE_SIZE
  );
  const portPaginationProps = {
    currentPageIndex: clampedPortPage,
    pagesCount: portPagesCount,
    onChange: ({ detail }) => setPortPage(detail.currentPageIndex),
  };
  const expandedItems = portPageItems.filter((i) => expandedPortIds.includes(i.id));

  return (
    <SpaceBetween size="l">
      {nodesWithErrors.map((n) => (
        <Alert key={n.id} type="warning" header={`Couldn't fetch LLDP from ${n.name}`}>
          {n.lldp_error} — its links below may be stale or incomplete.
        </Alert>
      ))}

      {degradedBundles.map((b) => (
        <Alert key={`${b.device_id}:${b.lag}`} type="warning" header={`${layout.nodeById[b.device_id]?.name}: bundle ${b.lag} is degraded`}>
          Its {b.member_count} members disagree on link state ({b.statuses.map((s) => s || "unknown").join(", ")}) -
          the bundle is still passing traffic on its remaining member(s), but has lost redundancy.
        </Alert>
      ))}

      <Container header={<Header variant="h2">Baseline</Header>}>
        <SpaceBetween size="s">
          {!baseline ? (
            <Alert type="info">
              No baseline saved yet - topology changes won't be flagged until one exists.
            </Alert>
          ) : (
            <Box>
              Saved {new Date(baseline.saved_at).toLocaleString()} by {baseline.saved_by}.
            </Box>
          )}
          {drift && (drift.added.length > 0 || drift.removed.length > 0) && (
            <Alert
              type="warning"
              header="Topology has drifted from the baseline"
              action={
                <Button onClick={() => handleAccept(drift.added, drift.removed)}>Accept all as new baseline</Button>
              }
            >
              <SpaceBetween size="xs">
                {drift.added.map((sig) => (
                  <Box key={sigKey(sig)}>
                    <StatusIndicator type="success">new</StatusIndicator> {describeSignature(sig, layout.nodeById)}{" "}
                    <Button variant="inline-link" onClick={() => handleAccept([sig], [])}>
                      Accept
                    </Button>
                  </Box>
                ))}
                {drift.removed.map((sig) => (
                  <Box key={sigKey(sig)}>
                    <StatusIndicator type="error">missing</StatusIndicator> {describeSignature(sig, layout.nodeById)}{" "}
                    <Button variant="inline-link" onClick={() => handleAccept([], [sig])}>
                      Accept
                    </Button>
                  </Box>
                ))}
              </SpaceBetween>
            </Alert>
          )}
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setConfirmAction("relearn")} loading={busyAction}>
              Relearn (save current as baseline)
            </Button>
            {baseline && (
              <Button onClick={() => setConfirmAction("forget")} loading={busyAction}>
                Forget baseline
              </Button>
            )}
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="s">
                <Toggle checked={showMacTableHosts} onChange={({ detail }) => setShowMacTableHosts(detail.checked)}>
                  MAC-table hosts ({macTableHostCount})
                </Toggle>
                <Toggle checked={autoRefresh} onChange={({ detail }) => setAutoRefresh(detail.checked)}>
                  Auto-refresh (30s)
                </Toggle>
                <Button iconName="refresh" onClick={load} loading={loading}>
                  Refresh
                </Button>
              </SpaceBetween>
            }
          >
            Fleet topology
          </Header>
        }
      >
        <SpaceBetween size="s">
          <SpaceBetween direction="horizontal" size="l">
            <Box fontSize="body-s" color="text-body-secondary">
              <svg width="20" height="12" style={{ verticalAlign: "middle", marginRight: 4 }}>
                <line x1="0" y1="6" x2="20" y2="6" stroke={COLOR_UP} strokeWidth="3" />
              </svg>
              Link up
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              <svg width="20" height="12" style={{ verticalAlign: "middle", marginRight: 4 }}>
                <line x1="0" y1="6" x2="20" y2="6" stroke={COLOR_DOWN} strokeWidth="3" />
              </svg>
              Link down
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              <svg width="20" height="12" style={{ verticalAlign: "middle", marginRight: 4 }}>
                <line x1="0" y1="6" x2="20" y2="6" stroke={COLOR_UNKNOWN} strokeWidth="3" />
              </svg>
              State unknown
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              <svg width="20" height="12" style={{ verticalAlign: "middle", marginRight: 4 }}>
                <line x1="0" y1="6" x2="20" y2="6" stroke={COLOR_UNKNOWN} strokeWidth="2" strokeDasharray="4 3" />
              </svg>
              LLDP neighbor (click to add)
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              <svg width="20" height="12" style={{ verticalAlign: "middle", marginRight: 4 }}>
                <line x1="0" y1="6" x2="20" y2="6" stroke={COLOR_UNKNOWN} strokeWidth="1" strokeDasharray="1 3" />
              </svg>
              MAC-table only (lower confidence, doesn't speak LLDP)
            </Box>
          </SpaceBetween>

          <svg viewBox={`0 0 ${layout.width} ${layout.height}`} style={{ width: "100%", height: "auto" }}>
            {internalEdges.map((e) => {
              const a = layout.positions[e.a.device_id];
              const b = layout.positions[e.b.device_id];
              if (!a || !b) return null;
              const info = edgeStatusInfo([e.a.state, e.b.state]);
              const key = `${e.a.device_id}:${e.a.port}-${e.b.device_id}:${e.b.port}`;

              const pairKey = [e.a.device_id, e.b.device_id].sort().join("|");
              const group = pairGroups[pairKey];
              const idxInGroup = group.indexOf(e);
              const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
              const dx = b.x - a.x;
              const dy = b.y - a.y;
              const len = Math.hypot(dx, dy) || 1;
              // Perpendicular unit vector, scaled by how far this edge sits
              // from the middle of its group (e.g. 3 edges -> offsets of
              // -28, 0, +28), so a single edge stays a straight line.
              const spacing = 28;
              const offset = (idxInGroup - (group.length - 1) / 2) * spacing;
              const ctrl = { x: mid.x + (-dy / len) * offset, y: mid.y + (dx / len) * offset };
              const path = group.length > 1
                ? `M ${a.x} ${a.y} Q ${ctrl.x} ${ctrl.y} ${b.x} ${b.y}`
                : `M ${a.x} ${a.y} L ${b.x} ${b.y}`;
              // Label placed at the curve's midpoint (same formula as the
              // control point, halved) so each parallel link's ports are
              // readable directly on the diagram, not just on hover.
              const labelPos = { x: mid.x + (-dy / len) * offset * 0.5, y: mid.y + (dx / len) * offset * 0.5 };

              return (
                <g key={key}>
                  <path
                    d={path} fill="none"
                    stroke={info.color} strokeWidth={hovered === key ? 5 : 3}
                    style={{ cursor: "pointer", transition: "stroke-width 0.1s" }}
                    onMouseEnter={() => setHovered(key)}
                    onMouseLeave={() => setHovered(null)}
                  >
                    <title>
                      {layout.nodeById[e.a.device_id]?.name} ({e.a.port}) ⟷ {layout.nodeById[e.b.device_id]?.name} ({e.b.port})
                      {"\n"}state: {info.text}
                      {formatMbps(e.a.state?.input_mbps) ? `\n${e.a.port} in: ${formatMbps(e.a.state.input_mbps)}` : ""}
                      {formatMbps(e.a.state?.output_mbps) ? `\n${e.a.port} out: ${formatMbps(e.a.state.output_mbps)}` : ""}
                    </title>
                  </path>
                  {hovered === key && (
                    <g style={{ pointerEvents: "none" }}>
                      <rect
                        x={labelPos.x - 62} y={labelPos.y - 11} width="124" height="22" rx="4"
                        fill="white" stroke={info.color} strokeWidth="1"
                      />
                      <text x={labelPos.x} y={labelPos.y + 4} textAnchor="middle" fontSize="11" fill={COLOR_TEXT}>
                        {e.a.port} ⟷ {e.b.port}
                      </text>
                    </g>
                  )}
                </g>
              );
            })}

            {nodes.flatMap((n) => {
              const a = layout.positions[n.id];
              const { groups, laneWidth } = layout.portGroupsByDevice[n.id] || { groups: [] };
              const cols = Math.max(1, Math.min(PORT_CARD_COLS, groups.length));
              const laneLeftX = a ? a.x - laneWidth / 2 : 0;
              const corridorX = cols > 1
                ? laneLeftX + PORT_CARD_W + PORT_CARD_GAP_X / 2
                : laneLeftX + PORT_CARD_W + 10;
              return groups.map((g) => {
                const card = layout.portPositions[`${n.id}:${g.port}`];
                if (!a || !card) return null;
                const rows = [
                  ...g.lldp.map((e) => ({ e, kind: "lldp" })),
                  ...g.mac.map((e) => ({ e, kind: "mac" })),
                ];
                return (
                  <g key={`${n.id}:${g.port}`}>
                    <path
                      d={portConnectorPath(a.x, a.y + DEVICE_NODE_R, card.x + card.width / 2, card.y, corridorX)}
                      fill="none" stroke={COLOR_UNKNOWN} strokeWidth="1.5"
                      strokeDasharray={g.lldp.length > 0 ? "4 3" : "1 3"}
                    />
                    <foreignObject x={card.x} y={card.y} width={card.width} height={card.height}>
                      <div
                        xmlns="http://www.w3.org/1999/xhtml"
                        style={{
                          fontFamily: "inherit",
                          background: "#f7f8fa",
                          border: `1.5px solid ${COLOR_UNKNOWN}`,
                          borderRadius: 8,
                          height: card.height - 2,
                          boxSizing: "border-box",
                          overflow: "hidden",
                        }}
                      >
                        <div
                          style={{
                            fontSize: 11, fontWeight: "bold", color: COLOR_TEXT, padding: "3px 8px",
                            borderBottom: `1px solid #d8dde3`, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                          }}
                          title={`${n.name} — ${g.port}${g.memberPorts?.length > 1 ? ` (${g.memberPorts.join(", ")})` : ""}`}
                        >
                          {g.port}
                          {g.memberPorts?.length > 1 ? ` (${g.memberPorts.length} members)` : ""}
                        </div>
                        {rows.map(({ e, kind }) => {
                          const ip = e.remote_ip;
                          const mac = externalMac(e);
                          const sub = externalSubline(e);
                          const alsoKnownAs = e.also_known_as?.length ? ` — also seen as: ${e.also_known_as.join(", ")}` : "";
                          const corroborated = kind === "lldp" && e.discovered_via.includes("mac-table") ? " (confirmed via MAC table)" : "";
                          const notice = kind === "mac" ? " — MAC table only, no LLDP" : "";
                          return (
                            <div
                              key={externalKey(e)}
                              onClick={() => onAddDevice?.({ name: ip || e.remote_label, host: ip || "" })}
                              title={`${ip || "no IP"} / ${mac}${sub ? ` (${sub})` : ""}${alsoKnownAs}${corroborated}${notice}\nClick to add as a device`}
                              style={{
                                display: "flex", alignItems: "center", gap: 5, padding: "1px 8px",
                                height: PORT_ROW_H, cursor: onAddDevice ? "pointer" : "default",
                                fontFamily: "monospace",
                              }}
                            >
                              <span
                                style={{
                                  flex: "none", width: 6, height: 6, borderRadius: "50%",
                                  background: kind === "lldp" ? COLOR_DEVICE_STROKE : "#9aa5b1",
                                }}
                              />
                              <span style={{ display: "flex", flexDirection: "column", overflow: "hidden", lineHeight: 1.25 }}>
                                <span
                                  style={{
                                    fontSize: 11, fontWeight: kind === "lldp" ? 600 : 400, color: COLOR_TEXT,
                                    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                                  }}
                                >
                                  {ip || "no IP"}
                                </span>
                                <span
                                  style={{
                                    fontSize: 9, color: COLOR_TEXT_SECONDARY,
                                    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                                  }}
                                >
                                  {mac}
                                </span>
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    </foreignObject>
                  </g>
                );
              });
            })}

            {nodes.map((n) => {
              const p = layout.positions[n.id];
              if (!p) return null;
              return (
                <g key={n.id} style={{ cursor: onOpenConsole ? "pointer" : "default" }} onClick={() => onOpenConsole?.(n.id)}>
                  <circle
                    cx={p.x} cy={p.y} r={DEVICE_NODE_R}
                    fill={n.lldp_error ? COLOR_DEVICE_ERROR_FILL : COLOR_DEVICE_FILL}
                    stroke={n.lldp_error ? COLOR_DEVICE_ERROR_STROKE : COLOR_DEVICE_STROKE}
                    strokeWidth="2.5"
                  >
                    <title>Open {n.name} in the Console</title>
                  </circle>
                  <text x={p.x} y={p.y - 10} textAnchor="middle" fontSize="13" fontWeight="bold" fill={COLOR_TEXT}>
                    {n.name.length > 20 ? `${n.name.slice(0, 18)}…` : n.name}
                  </text>
                  <text x={p.x} y={p.y + 6} textAnchor="middle" fontSize="11" fill={COLOR_TEXT_SECONDARY} fontFamily="monospace">
                    {n.host}
                  </text>
                  <text x={p.x} y={p.y + 20} textAnchor="middle" fontSize="9" fill={COLOR_TEXT_SECONDARY}>
                    {n.platform}
                  </text>
                </g>
              );
            })}
          </svg>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            counter={`(${filteredPortItems.length})`}
            description="Every local port across the fleet, and what's attached to it. Expand a port to see its individual hosts."
          >
            Ports
          </Header>
        }
      >
        <Table
          columnDefinitions={[
            { id: "port", header: "Port", cell: (i) => i.port || "", minWidth: 220 },
            { id: "host", header: "Host / remote", cell: (i) => i.host, minWidth: 220 },
            { id: "mac", header: "MAC address", cell: (i) => <span style={{ fontFamily: "monospace" }}>{i.mac}</span> },
            {
              id: "status",
              header: "State",
              cell: (i) => <StatusIndicator type={i.status.type}>{i.status.text}</StatusIndicator>,
            },
            { id: "discoveredVia", header: "Discovered via", cell: (i) => i.discoveredVia },
            { id: "utilization", header: "Utilization", cell: (i) => i.utilization },
          ]}
          items={portPageItems}
          trackBy="id"
          expandableRows={{
            getItemChildren: (item) => item.children,
            isItemExpandable: (item) => item.children.length > 0,
            expandedItems,
            onExpandableItemToggle: ({ detail }) =>
              setExpandedPortIds((prev) =>
                detail.expanded ? [...prev, detail.item.id] : prev.filter((id) => id !== detail.item.id)
              ),
          }}
          filter={
            <TextFilter
              filteringText={portFilterText}
              onChange={({ detail }) => {
                setPortFilterText(detail.filteringText);
                setPortPage(1);
              }}
              filteringPlaceholder="Find a port, host, IP, or MAC address..."
              countText={`${filteredPortItems.length} match${filteredPortItems.length === 1 ? "" : "es"}`}
            />
          }
          pagination={<Pagination {...portPaginationProps} />}
          empty={<Box textAlign="center">No ports found on any device.</Box>}
          variant="embedded"
          stripedRows
          resizableColumns
          wrapLines
        />
      </Container>

      <Modal
        visible={confirmAction === "relearn"}
        onDismiss={() => setConfirmAction(null)}
        header="Relearn topology baseline?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setConfirmAction(null)}>Cancel</Button>
              <Button variant="primary" onClick={handleRelearn} loading={busyAction}>Relearn</Button>
            </SpaceBetween>
          </Box>
        }
      >
        This overwrites the entire saved baseline with exactly what's live right now, discarding any
        previously-accepted changes. Use "Accept" on individual drift items instead if you only want to
        fold in one specific change.
      </Modal>

      <Modal
        visible={confirmAction === "forget"}
        onDismiss={() => setConfirmAction(null)}
        header="Forget topology baseline?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setConfirmAction(null)}>Cancel</Button>
              <Button variant="primary" onClick={handleForget} loading={busyAction}>Forget</Button>
            </SpaceBetween>
          </Box>
        }
      >
        No topology changes will be flagged until a new baseline is saved.
      </Modal>
    </SpaceBetween>
  );
}
