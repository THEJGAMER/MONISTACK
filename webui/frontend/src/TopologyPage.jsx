import React, { useCallback, useEffect, useMemo, useState } from "react";
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

import Tabs from "@cloudscape-design/components/tabs";
import {
  colorChartsStatusPositive, colorChartsStatusHigh, colorChartsStatusNeutral,
  fontFamilyMonospace,
} from "@cloudscape-design/design-tokens";
import { getTopology, saveTopologyBaseline, acceptTopologyDrift, clearTopologyBaseline } from "./api.js";
import TopologyMap from "./TopologyMap.jsx";

const PORT_PAGE_SIZE = 15;

const AUTO_REFRESH_MS = 30_000;

// Cloudscape design tokens, not hex: these resolve to CSS variables, so
// the diagram follows the app's light/dark mode instead of staying a
// light-mode island, and the status colours are the same ones every
// StatusIndicator on the page uses.
const COLOR_UP = colorChartsStatusPositive;
const COLOR_DOWN = colorChartsStatusHigh;
const COLOR_UNKNOWN = colorChartsStatusNeutral;

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
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [showMacTableHosts, setShowMacTableHosts] = useState(true);
  const [confirmAction, setConfirmAction] = useState(null); // "relearn" | "forget" | null
  const [busyAction, setBusyAction] = useState(false);
  const [expandedPortIds, setExpandedPortIds] = useState([]);
  const [portFilterText, setPortFilterText] = useState("");
  const [portPage, setPortPage] = useState(1);
  const [loadError, setLoadError] = useState(null);

  // Link state changes are the event system's job now (port.link_down,
  // raised and resolved, with de-duplication and push behind it). This
  // page used to detect them itself and raise a flash per changed edge,
  // which was both duplicate and wrong: it keyed by port, but a port has
  // one edge per host on it and only some of those carry a status - 39
  // edges on Po 3, of which 2 say "Up" and 37 say nothing. So the
  // remembered value flipped between null and "Up" on every crawl, and
  // the page announced "Po 3 is back up" twice every thirty seconds,
  // forever.
  const load = useCallback(async (force) => {
    setLoading(true);
    try {
      setData(await getTopology({ refresh: force === true }));
      setLoadError(null);
    } catch (e) {
      // The first crawl after a restart takes a while. That is a state to
      // show, not an error to pop up again on every auto-refresh.
      setLoadError(e.message);
      if (force) pushFlash("error", `Could not load topology: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }, [pushFlash]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(() => load(false), AUTO_REFRESH_MS);
    return () => clearInterval(id);
  }, [autoRefresh, load]);

  // The map owns its own geometry now (TopologyMap.jsx); all this tab
  // still needs is a way to turn a device id into its name.
  const layout = useMemo(
    () => ({ nodeById: Object.fromEntries((data?.nodes || []).map((n) => [n.id, n])) }),
    [data]
  );

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

  if (!data && loadError) {
    const warming = /first crawl|503/i.test(loadError);
    return (
      <Alert type={warming ? "info" : "error"} header="Topology is not ready yet">
        {loadError}
        {warming ? " The page fills in by itself as soon as the crawl finishes." : ""}
      </Alert>
    );
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

      {data?.last_error ? (
        <Alert type="warning" header="The last background crawl failed">
          Showing the previous successful result. {data.last_error}
        </Alert>
      ) : null}
      {data?.fetched_at ? (
        <Box color="text-body-secondary" fontSize="body-s">
          Crawled {data.age_seconds < 5 ? "just now" : `${data.age_seconds}s ago`}
          {data.refreshing ? " - refreshing…" : ""}; re-crawled every {data.refresh_seconds || 60}s in the background.
          {" "}Refresh on the Map tab forces a live crawl now.
        </Box>
      ) : null}
      {/* Tables first: a Cloudscape table with filter and pagination is
          the fastest way to answer "what is on port X" or "which links
          are down", and it works on a phone. The diagram is the picture
          for when the shape matters, one tab over. */}
      <Tabs
        tabs={[
          { id: "tables", label: "Links & hosts", content: (
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
                { id: "mac", header: "MAC address", cell: (i) => <Box variant="code">{i.mac}</Box> },
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
          ) },
          { id: "diagram", label: "Map", content: (
          <SpaceBetween size="s">
            <Header
              variant="h3"
              description={
                data?.fetched_at
                  ? `Crawled ${data.age_seconds < 5 ? "just now" : `${data.age_seconds}s ago`}${data.refreshing ? " - refreshing…" : ""}; re-crawled every ${data.refresh_seconds || 60}s in the background.`
                  : "Waiting for the first crawl…"
              }
              actions={
                <SpaceBetween direction="horizontal" size="s" alignItems="center">
                  <Toggle checked={showMacTableHosts} onChange={({ detail }) => setShowMacTableHosts(detail.checked)}>
                    MAC-table hosts ({macTableHostCount})
                  </Toggle>
                  <Toggle checked={autoRefresh} onChange={({ detail }) => setAutoRefresh(detail.checked)}>
                    Auto-refresh
                  </Toggle>
                  <Button iconName="refresh" onClick={() => load(true)} loading={loading}>
                    Refresh
                  </Button>
                </SpaceBetween>
              }
            />
            <TopologyMap
              data={data}
              showMacTableHosts={showMacTableHosts}
              onOpenConsole={onOpenConsole}
              onAddDevice={onAddDevice}
            />
          </SpaceBetween>
          ) },
        ]}
      />

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
