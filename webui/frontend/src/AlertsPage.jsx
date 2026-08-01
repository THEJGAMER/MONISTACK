import React, { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import FormField from "@cloudscape-design/components/form-field";

import { createSilence, deleteSilence, getAlerts, listSilences } from "./api.js";

const DURATION_OPTIONS = [
  { label: "1 hour", value: "1" },
  { label: "4 hours", value: "4" },
  { label: "8 hours", value: "8" },
  { label: "24 hours", value: "24" },
  { label: "1 week", value: "168" },
];

function severityType(sev) {
  if (sev === "critical") return "error";
  if (sev === "warning") return "warning";
  return "info";
}

function alertStateType(state) {
  if (state === "suppressed") return "stopped";
  if (state === "active") return "error";
  return "pending";
}

export default function AlertsPage({ pushFlash }) {
  const [alerts, setAlerts] = useState([]);
  const [silences, setSilences] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [matcherName, setMatcherName] = useState("alertname");
  const [matcherValue, setMatcherValue] = useState("");
  const [duration, setDuration] = useState("4");
  const [comment, setComment] = useState("");
  const [creating, setCreating] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      const [a, s] = await Promise.all([getAlerts(), listSilences()]);
      setAlerts(a);
      setSilences(s);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreateSilence() {
    if (!matcherValue.trim() || !comment.trim()) {
      pushFlash("error", "Matcher value and comment are required.");
      return;
    }
    setCreating(true);
    try {
      await createSilence({
        matchers: [{ name: matcherName, value: matcherValue.trim(), isRegex: false, isEqual: true }],
        duration_hours: parseFloat(duration),
        comment: comment.trim(),
      });
      pushFlash("success", "Silence created.");
      setMatcherValue("");
      setComment("");
      refresh();
    } catch (e) {
      pushFlash("error", `Could not create silence: ${e.message}`);
    } finally {
      setCreating(false);
    }
  }

  async function handleDeleteSilence(id) {
    try {
      await deleteSilence(id);
      refresh();
    } catch (e) {
      pushFlash("error", `Could not delete silence: ${e.message}`);
    }
  }

  const activeSilences = silences.filter((s) => s.status?.state !== "expired");

  return (
    <SpaceBetween size="l">
      {error && (
        <Container>
          <StatusIndicator type="error">Alertmanager unreachable: {error}</StatusIndicator>
        </Container>
      )}

      <Container
        header={
          <Header variant="h2" description="Fired by Prometheus rules (prometheus/alerts.yml) against the exporter's hardware metrics.">
            Active alerts
          </Header>
        }
      >
        <Table
          variant="embedded"
          loading={loading}
          items={alerts}
          columnDefinitions={[
            { id: "name", header: "Alert", cell: (a) => a.labels?.alertname },
            {
              id: "severity",
              header: "Severity",
              cell: (a) => <StatusIndicator type={severityType(a.labels?.severity)}>{a.labels?.severity || "-"}</StatusIndicator>,
            },
            { id: "summary", header: "Summary", cell: (a) => a.annotations?.summary || "-" },
            {
              id: "state",
              header: "State",
              cell: (a) => <StatusIndicator type={alertStateType(a.status?.state)}>{a.status?.state}</StatusIndicator>,
            },
            { id: "since", header: "Since", cell: (a) => new Date(a.startsAt).toLocaleString() },
          ]}
          empty={<Box textAlign="center">No active alerts.</Box>}
        />
      </Container>

      <Container
        header={
          <Header variant="h2" description="Real Alertmanager silences - time-boxed alert suppression for maintenance windows.">
            Maintenance windows (silences)
          </Header>
        }
      >
        <SpaceBetween size="m">
          <SpaceBetween size="s" direction="horizontal">
            <FormField label="Match label">
              <Select
                selectedOption={{ label: matcherName, value: matcherName }}
                onChange={({ detail }) => setMatcherName(detail.selectedOption.value)}
                options={[
                  { label: "alertname", value: "alertname" },
                  { label: "port", value: "port" },
                  { label: "unit", value: "unit" },
                ]}
              />
            </FormField>
            <FormField label="Value">
              <Input placeholder="e.g. S4048FanDown" value={matcherValue} onChange={({ detail }) => setMatcherValue(detail.value)} />
            </FormField>
            <FormField label="Duration">
              <Select
                selectedOption={DURATION_OPTIONS.find((o) => o.value === duration)}
                onChange={({ detail }) => setDuration(detail.selectedOption.value)}
                options={DURATION_OPTIONS}
              />
            </FormField>
            <FormField label="Comment">
              <Input placeholder="Reason for the maintenance window" value={comment} onChange={({ detail }) => setComment(detail.value)} />
            </FormField>
            <Button variant="primary" loading={creating} onClick={handleCreateSilence}>
              Create
            </Button>
          </SpaceBetween>

          <Table
            variant="embedded"
            items={activeSilences}
            columnDefinitions={[
              {
                id: "matchers",
                header: "Matches",
                cell: (s) => s.matchers.map((m) => `${m.name}=${m.value}`).join(", "),
              },
              { id: "state", header: "State", cell: (s) => <StatusIndicator type={s.status?.state === "active" ? "success" : "pending"}>{s.status?.state}</StatusIndicator> },
              { id: "ends", header: "Ends", cell: (s) => new Date(s.endsAt).toLocaleString() },
              { id: "comment", header: "Comment", cell: (s) => s.comment },
              { id: "created_by", header: "Created by", cell: (s) => s.createdBy },
              {
                id: "actions",
                header: "",
                cell: (s) => (
                  <Button variant="inline-link" onClick={() => handleDeleteSilence(s.id)}>
                    Expire now
                  </Button>
                ),
              },
            ]}
            empty={<Box textAlign="center">No active maintenance windows.</Box>}
          />
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
