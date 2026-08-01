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
import Tabs from "@cloudscape-design/components/tabs";
import Toggle from "@cloudscape-design/components/toggle";
import ExpandableSection from "@cloudscape-design/components/expandable-section";

import {
  createSilence,
  deleteSilence,
  getAlerts,
  listAlertRules,
  listSilences,
  updateAlertRule,
} from "./api.js";

const DURATION_OPTIONS = [
  { label: "1 hour", value: "1" },
  { label: "4 hours", value: "4" },
  { label: "8 hours", value: "8" },
  { label: "24 hours", value: "24" },
  { label: "1 week", value: "168" },
];

const SEVERITY_OPTIONS = [
  { label: "warning", value: "warning" },
  { label: "critical", value: "critical" },
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

function ActiveAlertsTab({ alerts, loading }) {
  return (
    <Container
      header={
        <Header variant="h2" description="Fired by Prometheus rules against the exporter's hardware metrics - see the Rules tab.">
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
  );
}

function MaintenanceWindowsTab({ silences, loading, pushFlash, refresh }) {
  const [matcherName, setMatcherName] = useState("alertname");
  const [matcherValue, setMatcherValue] = useState("");
  const [duration, setDuration] = useState("4");
  const [comment, setComment] = useState("");
  const [creating, setCreating] = useState(false);

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
  );
}

function RulesTab({ pushFlash }) {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [savingName, setSavingName] = useState(null);

  async function refresh() {
    setLoading(true);
    try {
      setRules(await listAlertRules());
    } catch (e) {
      pushFlash("error", `Could not load alert rules: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleUpdate(name, body) {
    setSavingName(name);
    try {
      const updated = await updateAlertRule(name, body);
      setRules((prev) => prev.map((r) => (r.name === name ? updated : r)));
      pushFlash("success", `${name} saved - Prometheus reloaded live.`);
    } catch (e) {
      pushFlash("error", `Could not update ${name}: ${e.message}`);
      refresh(); // resync in case the DB write succeeded but reload failed
    } finally {
      setSavingName(null);
    }
  }

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Severity drives Pushover priority (critical = high priority). Disabling a rule removes it from the live Prometheus config, not just this view. Thresholds/PromQL aren't editable here - see prometheus/alerts.yml in the repo for that."
        >
          Alert rules
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Table
          variant="embedded"
          loading={loading}
          items={rules}
          columnDefinitions={[
            { id: "name", header: "Rule", cell: (r) => r.name },
            {
              id: "severity",
              header: "Severity / priority",
              cell: (r) => (
                <Select
                  selectedOption={SEVERITY_OPTIONS.find((o) => o.value === r.severity)}
                  onChange={({ detail }) => handleUpdate(r.name, { severity: detail.selectedOption.value })}
                  options={SEVERITY_OPTIONS}
                  disabled={savingName === r.name}
                />
              ),
            },
            {
              id: "for",
              header: "For",
              cell: (r) => (r.for_seconds ? `${r.for_seconds}s` : "-"),
            },
            {
              id: "enabled",
              header: "Enabled",
              cell: (r) => (
                <Toggle
                  checked={r.enabled}
                  disabled={savingName === r.name}
                  onChange={({ detail }) => handleUpdate(r.name, { enabled: detail.checked })}
                />
              ),
            },
          ]}
          empty={<Box textAlign="center">No alert rules configured.</Box>}
        />
        {rules.map((r) => (
          <ExpandableSection key={r.name} headerText={`${r.name} details`}>
            <SpaceBetween size="xs">
              <Box>
                <b>Expression:</b> <code>{r.expr}</code>
              </Box>
              <Box>
                <b>Summary template:</b> {r.summary_template}
              </Box>
              <Box>
                <b>Description template:</b> {r.description_template}
              </Box>
            </SpaceBetween>
          </ExpandableSection>
        ))}
      </SpaceBetween>
    </Container>
  );
}

export default function AlertsPage({ pushFlash }) {
  const [alerts, setAlerts] = useState([]);
  const [silences, setSilences] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [activeTab, setActiveTab] = useState("active");

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

  return (
    <SpaceBetween size="l">
      {error && (
        <Container>
          <StatusIndicator type="error">Alertmanager unreachable: {error}</StatusIndicator>
        </Container>
      )}

      <Tabs
        activeTabId={activeTab}
        onChange={({ detail }) => setActiveTab(detail.activeTabId)}
        tabs={[
          { id: "active", label: "Active alerts", content: <ActiveAlertsTab alerts={alerts} loading={loading} /> },
          {
            id: "maintenance",
            label: "Maintenance windows",
            content: <MaintenanceWindowsTab silences={silences} loading={loading} pushFlash={pushFlash} refresh={refresh} />,
          },
          { id: "rules", label: "Rules", content: <RulesTab pushFlash={pushFlash} /> },
        ]}
      />
    </SpaceBetween>
  );
}
