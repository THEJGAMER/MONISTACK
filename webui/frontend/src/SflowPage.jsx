import React, { useCallback, useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Select from "@cloudscape-design/components/select";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Modal from "@cloudscape-design/components/modal";
import StatusIndicator from "@cloudscape-design/components/status-indicator";

import AreaChart from "@cloudscape-design/components/area-chart";
import BarChart from "@cloudscape-design/components/bar-chart";
import Toggle from "@cloudscape-design/components/toggle";
import TextFilter from "@cloudscape-design/components/text-filter";
import DateRangePicker from "@cloudscape-design/components/date-range-picker";

import { getSflowOverview, getSflowPort, getSflowHost } from "./api.js";

// One control for the entire page. Every panel here - the tiles, the
// chart and all four tables - is served by a single request carrying this
// window, and the drill-down modals carry it too, so there is never a
// panel showing a different span from the one named at the top.
const RANGE_PRESETS = [
  { key: "1h",  amount: 1,  unit: "hour" },
  { key: "3h",  amount: 3,  unit: "hour" },
  { key: "6h",  amount: 6,  unit: "hour" },
  { key: "12h", amount: 12, unit: "hour" },
  { key: "1d",  amount: 1,  unit: "day" },
  { key: "7d",  amount: 7,  unit: "day" },
].map((r) => ({ ...r, type: "relative" }));

const DEFAULT_RANGE = { type: "relative", amount: 1, unit: "hour", key: "1h" };

const UNIT_MINUTES = { second: 1 / 60, minute: 1, hour: 60, day: 1440, week: 10080, month: 43200, year: 525600 };

// The picker speaks relative-or-absolute; the API speaks minutes-or-ISO.
// Absolute ranges are converted to UTC here because the server treats a
// naive timestamp as UTC, and letting the browser's local offset go
// unstated would shift the window by hours without saying so.
function rangeToQuery(range) {
  if (!range) return { minutes: 60 };
  if (range.type === "absolute") {
    return {
      minutes: 60,
      start: new Date(range.startDate).toISOString(),
      end: new Date(range.endDate).toISOString(),
    };
  }
  const mins = Math.max(1, Math.round(range.amount * (UNIT_MINUTES[range.unit] || 1)));
  return { minutes: mins };
}

function rangeLabel(range) {
  if (!range) return "";
  if (range.type === "absolute") {
    const fmt = (d) => new Date(d).toLocaleString();
    return `${fmt(range.startDate)} to ${fmt(range.endDate)}`;
  }
  const n = range.amount;
  return `last ${n} ${range.unit}${n === 1 ? "" : "s"}`;
}

function bytes(n) {
  const v = Number(n || 0);
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)} GB`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)} MB`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)} KB`;
  return `${v} B`;
}

export default function SflowPage({ devices, pushFlash }) {
  const [range, setRange] = useState(DEFAULT_RANGE);
  const [agent, setAgent] = useState({ label: "All switches", value: "" });
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [drill, setDrill] = useState(null);
  const [hostDrill, setHostDrill] = useState(null);
  const [auto, setAuto] = useState(false);
  const [filterText, setFilterText] = useState("");
  // Debounced so typing an address is one query, not one per keystroke -
  // these are aggregates over the whole window, not a lookup.
  const [query, setQuery] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setQuery(filterText.trim()), 400);
    return () => clearTimeout(id);
  }, [filterText]);

  // Options come from the *data* (which agents have actually sent flows),
  // annotated with a device name where the server could match one. Built
  // from the device registry instead, an agent whose sFlow agent-id
  // differs from its management IP is unselectable - which is exactly
  // what happened with the EX3300 reporting 192.168.5.10 while registered
  // at 192.168.4.1.
  const agentOptions = [
    { label: "All switches", value: "" },
    ...(data?.agents || []).map((a) => ({
      label: a.device_name ? `${a.device_name} (${a.peer_ip_src})` : a.peer_ip_src,
      value: a.peer_ip_src,
    })),
  ];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await getSflowOverview({ ...rangeToQuery(range), agent: agent.value || undefined, q: query || undefined }));
    } catch (e) {
      pushFlash("error", `Could not load sFlow data: ${e.message}`);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [range, agent, query, pushFlash]);

  useEffect(() => {
    load();
  }, [load]);

  // Off by default: this page is usually opened to answer a question, not
  // left up as a dashboard, and a background refresh that silently
  // reshuffles the table under a cursor is worse than a stale view.
  useEffect(() => {
    if (!auto) return undefined;
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [auto, load]);

  async function openHost(host) {
    try {
      setHostDrill(await getSflowHost(host, { ...rangeToQuery(range), agent: agent.value || undefined }));
    } catch (e) {
      pushFlash("error", `Could not load host detail: ${e.message}`);
    }
  }

  // Server-side. Filtering here instead would only ever search the rows
  // already fetched - the top 20 of the window - so anything ranked below
  // that was unfindable however precisely it was typed. A host sitting
  // 86th of 152 returned nothing for its own address, and the box gave no
  // hint it was searching a truncated list rather than the window.
  const q = (data?.q || "").toLowerCase();
  const talkers = data?.top_talkers || [];
  const hosts = data?.top_hosts || [];
  const protos = data?.protocol_mix || [];
  const ports = data?.per_port || [];

  // Stacked area over time, one series per switch. Cloudscape's own
  // categorical order is used rather than hand-picked colours - it is a
  // validated palette, and the project's design rules mandate its tokens.
  const tsSeries = Object.entries(data?.timeseries?.series || {}).map(([agentIp, points]) => ({
    title: nameFor(agentIp),
    type: "area",
    data: points.map((pt) => ({ x: new Date(pt.t), y: Number(pt.bytes) })),
    valueFormatter: bytes,
  }));

  function nameFor(agentIp) {
    const a = (data?.agents || []).find((x) => x.peer_ip_src === agentIp);
    return a?.device_name || agentIp;
  }

  async function openPort(iface, agentIp) {
    try {
      // Scoped to the switch that owns this ifIndex, not the page filter -
      // otherwise the drill-down mixes two switches' identically-numbered
      // interfaces together.
      setDrill(await getSflowPort(iface, { ...rangeToQuery(range), agent: agentIp }));
    } catch (e) {
      pushFlash("error", `Could not load port detail: ${e.message}`);
    }
  }

  if (loading && !data) return <Spinner />;

  // "Nothing matched your filter" and "no switch has ever sent us
  // anything" need completely different advice, so they're separate.
  if (data && !data.available) {
    return (
      <Alert type="info" header="No sFlow data has arrived yet">
        <SpaceBetween size="s">
          <Box>
            The collector is reachable but no switch has ever sent it a flow. On Dell OS9 a global
            <Box variant="code" display="inline"> sflow enable </Box> is not enough - each interface
            you want sampled also needs <Box variant="code" display="inline">sflow enable</Box>.
            On Junos, check the config was actually <Box variant="code" display="inline">commit</Box>ted
            and that <Box variant="code" display="inline">interfaces</Box> and
            <Box variant="code" display="inline"> sample-rate</Box> are set.
          </Box>
          <Box color="text-status-inactive">
            Verify from the switch with <Box variant="code" display="inline">show sflow</Box> - it reports
            packets exported and samples collected.
          </Box>
        </SpaceBetween>
      </Alert>
    );
  }

  const empty = (
    <Box color="text-status-inactive">
      {q ? `Nothing matching "${data?.q}" in this window.` : "Nothing in this window."}
    </Box>
  );

  return (
    <SpaceBetween size="l">
      {data?.clamped_to_days ? (
        <Alert type="info" header="Showing a shorter range than requested">
          These views aggregate every flow record in the range, so they are capped at{" "}
          {data.clamped_to_days} days. The most recent {data.clamped_to_days} days of the range
          you picked are shown. Nothing has been deleted - the older data is still there.
        </Alert>
      ) : null}
      <Container
        header={
          <Header
            variant="h2"
            description={
              // The window is named once, here, because it governs every
              // panel below - naming it per panel would invite the reader
              // to think each could differ.
              `Traffic sampled by the switches themselves (1 packet in 1024) and scaled back up by sfacctd, so these are estimates of real traffic. `
              + `Showing ${rangeLabel(range)} across every panel on this page.`
            }
            actions={
              <SpaceBetween size="xs" direction="horizontal" alignItems="center">
                <Toggle checked={auto} onChange={({ detail }) => setAuto(detail.checked)}>Auto</Toggle>
                <DateRangePicker
                  value={range}
                  onChange={({ detail }) => setRange(detail.value)}
                  relativeOptions={RANGE_PRESETS}
                  rangeSelectorMode="default"
                  placeholder="Choose a time range"
                  hideTimeOffset
                  isValidRange={(r) => {
                    if (!r) return { valid: false, errorMessage: "Pick a range." };
                    if (r.type === "absolute") {
                      if (!r.startDate || !r.endDate) {
                        return { valid: false, errorMessage: "Both a start and an end date are needed." };
                      }
                      if (new Date(r.startDate) >= new Date(r.endDate)) {
                        return { valid: false, errorMessage: "The start must be before the end." };
                      }
                    } else if (!r.amount || r.amount <= 0) {
                      return { valid: false, errorMessage: "The range must be a positive length." };
                    }
                    return { valid: true };
                  }}
                  i18nStrings={{
                    relativeModeTitle: "Relative range",
                    absoluteModeTitle: "Absolute range",
                    relativeRangeSelectionHeading: "Choose a range",
                    customRelativeRangeOptionLabel: "Custom range",
                    customRelativeRangeOptionDescription: "Any number of minutes, hours or days back from now",
                    customRelativeRangeUnitLabel: "Unit of time",
                    customRelativeRangeDurationLabel: "Duration",
                    startDateLabel: "Start date", startTimeLabel: "Start time",
                    endDateLabel: "End date", endTimeLabel: "End time",
                    clearButtonLabel: "Clear", cancelButtonLabel: "Cancel", applyButtonLabel: "Apply",
                    formatRelativeRange: (r) => `Last ${r.amount} ${r.unit}${r.amount === 1 ? "" : "s"}`,
                    formatUnit: (unit, n) => (n === 1 ? unit : `${unit}s`),
                    dateTimeConstraintText: "Local time, applied to every panel on this page.",
                    todayAriaLabel: "Today",
                    nextMonthAriaLabel: "Next month",
                    previousMonthAriaLabel: "Previous month",
                  }}
                />
                <Select selectedOption={agent} onChange={({ detail }) => setAgent(detail.selectedOption)} options={agentOptions} />
                <Button iconName="refresh" loading={loading} onClick={load}>Refresh</Button>
              </SpaceBetween>
            }
          >
            Traffic (sFlow)
          </Header>
        }
      >
        {/* Headline figures are stat tiles, not charts - a single number
            is not a bar chart. */}
        <SpaceBetween size="l">
          <ColumnLayout columns={4} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Traffic (estimated)</Box>
              <Box fontSize="display-l" fontWeight="bold">{bytes(data?.totals?.bytes)}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Flow records</Box>
              <Box fontSize="display-l" fontWeight="bold">{Number(data?.totals?.records || 0).toLocaleString()}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Distinct sources</Box>
              <Box fontSize="display-l" fontWeight="bold">{Number(data?.totals?.talkers || 0).toLocaleString()}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Switches reporting</Box>
              <Box fontSize="display-l" fontWeight="bold">{data?.agents?.length || 0}</Box>
              <Box color="text-status-inactive" fontSize="body-s">
                {(data?.agents || []).map((a) => a.device_name || a.peer_ip_src).join(", ") || "none"}
              </Box>
            </div>
          </ColumnLayout>

          {/* Trend over time, stacked by switch. This is the one thing the
              tables below cannot show - every other view collapses time
              away entirely. One y-axis, bytes only: never a second scale. */}
          <AreaChart
            series={tsSeries}
            xScaleType="time"
            height={220}
            hideFilter
            xTitle="Time"
            yTitle="Bytes (estimated)"
            ariaLabel="Sampled traffic over time, stacked by switch"
            i18nStrings={{ xTickFormatter: (t) => t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
                           yTickFormatter: bytes }}
            empty={<Box color="text-status-inactive">No traffic in this window.</Box>}
            noMatch={<Box color="text-status-inactive">No traffic matches.</Box>}
          />
        </SpaceBetween>
      </Container>

      <TextFilter
        filteringText={filterText}
        onChange={({ detail }) => setFilterText(detail.filteringText)}
        filteringPlaceholder="Search the whole window by address, port, service, interface or switch..."
        countText={
          q && !loading
            ? `${talkers.length + hosts.length + protos.length + ports.length} matches for "${data?.q}"`
            : ""
        }
        filteringAriaLabel="Search sFlow traffic"
      />

      {/* Magnitude across identities -> horizontal bars. The table beside
          it carries the exact numbers; the chart is for reading the shape
          at a glance, which a table cannot do. */}
      <Container header={<Header variant="h3" description="Busiest hosts in this window, both directions">Traffic by host</Header>}>
        <BarChart
          series={[{ title: "Traffic", type: "bar", data: hosts.slice(0, 10).map((h) => ({ x: h.host, y: Number(h.bytes) })), valueFormatter: bytes }]}
          horizontalBars
          hideFilter
          hideLegend
          height={260}
          xTitle="Host"
          yTitle="Bytes (estimated)"
          ariaLabel="Sampled traffic by host"
          i18nStrings={{ yTickFormatter: bytes }}
          empty={<Box color="text-status-inactive">No traffic in this window.</Box>}
          noMatch={<Box color="text-status-inactive">No hosts match.</Box>}
        />
      </Container>

      <ColumnLayout columns={2}>
        <Container header={<Header variant="h3" description="Busiest conversations">Top talkers</Header>}>
          <Table
            variant="embedded" items={talkers} empty={empty}
            trackBy={(t) => `${t.ip_src}-${t.ip_dst}`}
            columnDefinitions={[
              { id: "src", header: "Source", cell: (t) => <Box variant="code">{t.ip_src || "-"}</Box> },
              { id: "dst", header: "Destination", cell: (t) => <Box variant="code">{t.ip_dst || "-"}</Box> },
              { id: "bytes", header: "Traffic", cell: (t) => bytes(t.bytes) },
            ]}
          />
        </Container>

        <Container header={<Header variant="h3" description="Both directions combined">Top hosts</Header>}>
          <Table
            variant="embedded" items={hosts} empty={empty} trackBy="host"
            columnDefinitions={[
              {
                id: "host", header: "Host",
                cell: (h) => (
                  <Button variant="inline-link" onClick={() => openHost(h.host)}>{h.host}</Button>
                ),
              },
              { id: "bytes", header: "Traffic", cell: (h) => bytes(h.bytes) },
              { id: "packets", header: "Packets", cell: (h) => h.packets },
            ]}
          />
        </Container>
      </ColumnLayout>

      <ColumnLayout columns={2}>
        <Container header={<Header variant="h3" description="Keyed on the well-known side of each conversation">Protocol / service mix</Header>}>
          <Table
            variant="embedded" items={protos} empty={empty}
            trackBy={(p) => `${p.ip_proto}-${p.port}`}
            columnDefinitions={[
              { id: "proto", header: "Proto", cell: (p) => p.proto_name },
              { id: "port", header: "Port", cell: (p) => (p.port === 65535 ? "-" : p.port) },
              { id: "service", header: "Service", cell: (p) => p.service || <Box color="text-status-inactive">unknown</Box> },
              { id: "bytes", header: "Traffic", cell: (p) => bytes(p.bytes) },
            ]}
          />
        </Container>

        <Container header={<Header variant="h3" description="Per switch interface - click a row for what's crossing it">Per-port traffic</Header>}>
          <Table
            variant="embedded" items={ports} empty={empty}
            trackBy={(p) => `${p.peer_ip_src}-${p.iface}`}
            columnDefinitions={[
              {
                id: "port", header: "Port",
                // An unmapped ifIndex is shown raw rather than guessed at -
                // the decode only covers Dell OS9 physical ports.
                cell: (p) => (
                  <Button variant="inline-link" onClick={() => openPort(p.iface, p.peer_ip_src)}>
                    {p.port || `ifIndex ${p.iface}`}
                  </Button>
                ),
              },
              // Shown because an ifIndex only means anything relative to
              // the switch that issued it.
              { id: "switch", header: "Switch", cell: (p) => p.peer_ip_src },
              { id: "in", header: "In", cell: (p) => bytes(p.in_bytes) },
              { id: "out", header: "Out", cell: (p) => bytes(p.out_bytes) },
            ]}
          />
        </Container>
      </ColumnLayout>

      <Modal
        visible={!!hostDrill}
        onDismiss={() => setHostDrill(null)}
        header={hostDrill ? `Traffic involving ${hostDrill.host}` : ""}
        size="large"
      >
        <Table
          variant="embedded" items={hostDrill?.flows || []} empty={empty}
          trackBy={(f) => `${f.ip_src}-${f.ip_dst}-${f.port}`}
          columnDefinitions={[
            {
              id: "dir", header: "",
              // Direction is stated rather than left to be inferred from
              // which column the address happens to be in.
              cell: (f) => (
                <StatusIndicator type={f.direction === "out" ? "info" : "success"}>
                  {f.direction === "out" ? "out" : "in"}
                </StatusIndicator>
              ),
            },
            { id: "peer", header: "Peer",
              cell: (f) => <Box variant="code">{f.direction === "out" ? f.ip_dst : f.ip_src}</Box> },
            { id: "svc", header: "Service", cell: (f) => f.service || (f.port === 65535 ? "-" : f.port) },
            { id: "proto", header: "Proto", cell: (f) => f.proto_name },
            { id: "bytes", header: "Traffic", cell: (f) => bytes(f.bytes) },
          ]}
        />
      </Modal>

      <Modal
        visible={!!drill}
        onDismiss={() => setDrill(null)}
        header={drill ? `Traffic on ${drill.port || `ifIndex ${drill.iface}`}` : ""}
        size="large"
      >
        <Table
          variant="embedded" items={drill?.flows || []} empty={empty}
          trackBy={(f) => `${f.ip_src}-${f.ip_dst}-${f.port}`}
          columnDefinitions={[
            { id: "src", header: "Source", cell: (f) => <Box variant="code">{f.ip_src || "-"}</Box> },
            { id: "dst", header: "Destination", cell: (f) => <Box variant="code">{f.ip_dst || "-"}</Box> },
            { id: "svc", header: "Service", cell: (f) => f.service || (f.port === 65535 ? "-" : f.port) },
            { id: "proto", header: "Proto", cell: (f) => f.proto_name },
            { id: "bytes", header: "Traffic", cell: (f) => bytes(f.bytes) },
          ]}
        />
      </Modal>
    </SpaceBetween>
  );
}
