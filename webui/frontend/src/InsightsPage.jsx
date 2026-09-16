// Insights: what is worth knowing about the network right now.
//
// Not a second alarm list. Events say what broke; this says what is worth
// a look - a link quietly losing light, optics sitting in dead ports, the
// port carrying most of the traffic, the live ports nobody labelled.
// Findings come ordered by how much they want doing something about, and
// the checks that found nothing are listed too, so the page shows it
// looked rather than just going quiet.
import React, { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Alert from "@cloudscape-design/components/alert";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Spinner from "@cloudscape-design/components/spinner";

import { getInsights } from "./api.js";

const LEVELS = {
  act: { type: "error", label: "Worth doing something about" },
  watch: { type: "warning", label: "Worth an eye" },
  note: { type: "info", label: "Worth knowing" },
};

function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

function cellFor(columnId) {
  return (row) => {
    const v = row[columnId];
    if (v === null || v === undefined || v === "") return "-";
    if (columnId === "severity") {
      return <StatusIndicator type={v === "critical" ? "error" : v === "warning" ? "warning" : "info"}>{v}</StatusIndicator>;
    }
    if (columnId === "change") return <Box color="text-status-error">{v}</Box>;
    if (columnId === "margin" || columnId === "headroom") {
      return <Box color={Number(v) <= 3 ? "text-status-error" : Number(v) <= 6 ? "text-status-warning" : "text-status-success"}>{v}</Box>;
    }
    if (columnId === "used" && Number(v) >= 70) return <Box color="text-status-warning">{v}</Box>;
    if (columnId === "last" && String(v).includes("T")) return new Date(v).toLocaleString();
    return String(v);
  };
}

function Finding({ finding }) {
  const level = LEVELS[finding.level] || LEVELS.note;
  const hidden = finding.total - finding.rows.length;
  return (
    <Container
      header={
        <Header
          variant="h2"
          description={finding.summary}
          info={<Badge color={finding.level === "act" ? "red" : finding.level === "watch" ? "severity-medium" : "grey"}>{level.label}</Badge>}
        >
          {finding.title}
        </Header>
      }
      footer={finding.detail ? <Box color="text-body-secondary">{finding.detail}</Box> : undefined}
    >
      <Table
        variant="embedded"
        items={finding.rows}
        wrapLines
        columnDefinitions={finding.columns.map((c) => ({ id: c.id, header: c.header, cell: cellFor(c.id) }))}
        footer={hidden > 0 ? <Box textAlign="center" color="text-body-secondary">and {hidden} more</Box> : undefined}
      />
    </Container>
  );
}

export default function InsightsPage({ pushFlash }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  async function load(recompute = false) {
    if (recompute) setRefreshing(true);
    try {
      setData(await getInsights(recompute));
    } catch (e) {
      pushFlash("error", `Could not load insights: ${e.message}`);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(() => load(), 60000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> <Box variant="p">Working through the last six weeks of measurements...</Box>
      </Box>
    );
  }

  const findings = data?.findings || [];
  const counts = findings.reduce((acc, f) => ({ ...acc, [f.level]: (acc[f.level] || 0) + 1 }), {});

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Derived from the trend history, the SSH poller's live state and the event log. Nothing here talks to a device."
        actions={
          <Button iconName="refresh" loading={refreshing} onClick={() => load(true)}>
            Recompute
          </Button>
        }
      >
        Insights
      </Header>

      <Container>
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Worth doing something about</Box>
            <StatusIndicator type={counts.act ? "error" : "success"}>{counts.act || 0}</StatusIndicator>
          </div>
          <div>
            <Box variant="awsui-key-label">Worth an eye</Box>
            <StatusIndicator type={counts.watch ? "warning" : "success"}>{counts.watch || 0}</StatusIndicator>
          </div>
          <div>
            <Box variant="awsui-key-label">Worth knowing</Box>
            <StatusIndicator type="info">{counts.note || 0}</StatusIndicator>
          </div>
          <div>
            <Box variant="awsui-key-label">Looked at</Box>
            <Box>
              {data.devices} device(s), {data.interfaces} interface(s)
              <Box color="text-body-secondary">computed {ago(data.cached_at || data.generated_at)}</Box>
            </Box>
          </div>
        </ColumnLayout>
      </Container>

      {data.failed?.length > 0 && (
        <Alert type="warning" header="Some checks could not run">
          {data.failed.join(", ")}. The rest of the page is unaffected.
        </Alert>
      )}

      {findings.length === 0 && (
        <Alert type="success" header="Nothing to report">
          Every check ran and found nothing worth saying. That is a good sign, not a broken page.
        </Alert>
      )}

      {findings.map((f) => (
        <Finding key={f.id} finding={f} />
      ))}

      {data.clear?.length > 0 && (
        <ExpandableSection headerText={`Checked and found nothing (${data.clear.length})`}>
          <SpaceBetween size="xxs">
            {data.clear.map((name) => (
              <Box key={name}>
                <StatusIndicator type="success">{name}</StatusIndicator>
              </Box>
            ))}
          </SpaceBetween>
        </ExpandableSection>
      )}
    </SpaceBetween>
  );
}
