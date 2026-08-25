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

import { getSflowOverview, getSflowPort } from "./api.js";

const WINDOWS = [
  { label: "Last 15 minutes", value: "15" },
  { label: "Last hour", value: "60" },
  { label: "Last 6 hours", value: "360" },
  { label: "Last 24 hours", value: "1440" },
  { label: "Last 7 days", value: "10080" },
];

function bytes(n) {
  const v = Number(n || 0);
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)} GB`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)} MB`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)} KB`;
  return `${v} B`;
}

export default function SflowPage({ devices, pushFlash }) {
  const [minutes, setMinutes] = useState(WINDOWS[1]);
  const [agent, setAgent] = useState({ label: "All switches", value: "" });
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [drill, setDrill] = useState(null);

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
      setData(await getSflowOverview({ minutes: Number(minutes.value), agent: agent.value || undefined }));
    } catch (e) {
      pushFlash("error", `Could not load sFlow data: ${e.message}`);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [minutes, agent, pushFlash]);

  useEffect(() => {
    load();
  }, [load]);

  async function openPort(iface, agentIp) {
    try {
      // Scoped to the switch that owns this ifIndex, not the page filter -
      // otherwise the drill-down mixes two switches' identically-numbered
      // interfaces together.
      setDrill(await getSflowPort(iface, { minutes: Number(minutes.value), agent: agentIp }));
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

  const empty = <Box color="text-status-inactive">Nothing in this window.</Box>;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description="Traffic sampled by the switches themselves and collected by sfacctd. Byte counts are estimates scaled from samples, not exact measurements - useful in relative terms."
            actions={
              <SpaceBetween size="xs" direction="horizontal">
                <Select selectedOption={minutes} onChange={({ detail }) => setMinutes(detail.selectedOption)} options={WINDOWS} />
                <Select selectedOption={agent} onChange={({ detail }) => setAgent(detail.selectedOption)} options={agentOptions} />
                <Button iconName="refresh" loading={loading} onClick={load}>Refresh</Button>
              </SpaceBetween>
            }
          >
            Traffic (sFlow)
          </Header>
        }
      >
        {data?.agents?.length ? (
          <ColumnLayout columns={Math.min(data.agents.length, 3)} variant="text-grid">
            {data.agents.map((a) => (
              <div key={a.peer_ip_src}>
                <Box variant="awsui-key-label">{a.device_name || a.peer_ip_src}</Box>
                <Box fontSize="display-l" fontWeight="bold">{bytes(a.bytes)}</Box>
                <Box color="text-status-inactive">
                  {a.flows} flow records · {a.peer_ip_src}
                  {!a.platform && " · unrecognised agent"}
                </Box>
              </div>
            ))}
          </ColumnLayout>
        ) : (
          <StatusIndicator type="warning">No switch reported traffic in this window</StatusIndicator>
        )}
      </Container>

      <ColumnLayout columns={2}>
        <Container header={<Header variant="h3" description="Busiest conversations">Top talkers</Header>}>
          <Table
            variant="embedded" items={data?.top_talkers || []} empty={empty}
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
            variant="embedded" items={data?.top_hosts || []} empty={empty} trackBy="host"
            columnDefinitions={[
              { id: "host", header: "Host", cell: (h) => <Box variant="code">{h.host}</Box> },
              { id: "bytes", header: "Traffic", cell: (h) => bytes(h.bytes) },
              { id: "packets", header: "Packets", cell: (h) => h.packets },
            ]}
          />
        </Container>
      </ColumnLayout>

      <ColumnLayout columns={2}>
        <Container header={<Header variant="h3" description="Keyed on the well-known side of each conversation">Protocol / service mix</Header>}>
          <Table
            variant="embedded" items={data?.protocol_mix || []} empty={empty}
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
            variant="embedded" items={data?.per_port || []} empty={empty}
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
