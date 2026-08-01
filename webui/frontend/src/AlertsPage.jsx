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
import Pagination from "@cloudscape-design/components/pagination";
import TextFilter from "@cloudscape-design/components/text-filter";

import { useClientPagination } from "./useClientPagination.js";

import {
  createSilence,
  deleteSilence,
  getAlertHistory,
  getAlerts,
  listAlertRules,
  listInterfaceAlerts,
  listSilences,
  updateAlertRule,
  updateInterfaceAlert,
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

const MODE_OPTIONS = [
  { label: "Immediately", value: "immediate" },
  { label: "After a delay (recheck, then alarm)", value: "delayed" },
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

function portStateType(state) {
  if (state === "up") return "success";
  if (state === "down") return "error";
  if (state === "admin_down") return "stopped";
  return "info";
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

function InterfacesTab({ devices, pushFlash }) {
  const [deviceId, setDeviceId] = useState(devices[0]?.id || null);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [savingKey, setSavingKey] = useState(null);

  const deviceOptions = devices.map((d) => ({ label: d.name, value: d.id }));

  async function refresh(id) {
    if (!id) return;
    setLoading(true);
    try {
      setRows(await listInterfaceAlerts(id));
    } catch (e) {
      pushFlash("error", `Could not load interface alerts: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh(deviceId);
    const t = setInterval(() => refresh(deviceId), 30000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  async function handleUpdate(row, patch) {
    const key = row.port;
    setSavingKey(key);
    try {
      const body = { enabled: row.enabled, mode: row.mode, delay_seconds: row.delay_seconds, severity: row.severity, ...patch };
      const updated = await updateInterfaceAlert(deviceId, row.port, body);
      setRows((prev) => prev.map((r) => (r.port === row.port ? { ...r, ...updated } : r)));
    } catch (e) {
      pushFlash("error", `Could not update ${row.port}: ${e.message}`);
      refresh(deviceId);
    } finally {
      setSavingKey(null);
    }
  }

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Opt specific interfaces into down-alerting - most ports are unused and shouldn't alert. Alarms post directly to Alertmanager, so they go through the same Pushover/silence pipeline as everything else."
        >
          Interface alerts
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Select
          placeholder="Device"
          selectedOption={deviceOptions.find((o) => o.value === deviceId) || null}
          onChange={({ detail }) => setDeviceId(detail.selectedOption.value)}
          options={deviceOptions}
        />
        <Table
          variant="embedded"
          loading={loading}
          items={rows}
          columnDefinitions={[
            { id: "port", header: "Interface", cell: (r) => r.port },
            {
              id: "state",
              header: "Current state",
              cell: (r) => <StatusIndicator type={portStateType(r.current_state)}>{r.current_state || "unknown"}</StatusIndicator>,
            },
            {
              id: "enabled",
              header: "Alert on down",
              cell: (r) => (
                <Toggle
                  checked={r.enabled}
                  disabled={savingKey === r.port}
                  onChange={({ detail }) => handleUpdate(r, { enabled: detail.checked })}
                />
              ),
            },
            {
              id: "severity",
              header: "Severity / priority",
              cell: (r) => (
                <Select
                  selectedOption={SEVERITY_OPTIONS.find((o) => o.value === r.severity)}
                  onChange={({ detail }) => handleUpdate(r, { severity: detail.selectedOption.value })}
                  options={SEVERITY_OPTIONS}
                  disabled={!r.enabled || savingKey === r.port}
                  expandToViewport
                />
              ),
            },
            {
              id: "mode",
              header: "When",
              cell: (r) => (
                <Select
                  selectedOption={MODE_OPTIONS.find((o) => o.value === r.mode)}
                  onChange={({ detail }) => handleUpdate(r, { mode: detail.selectedOption.value })}
                  options={MODE_OPTIONS}
                  disabled={!r.enabled || savingKey === r.port}
                  expandToViewport
                />
              ),
            },
            {
              id: "delay",
              header: "Delay (s)",
              cell: (r) =>
                r.mode === "delayed" ? (
                  <Input
                    type="number"
                    value={String(r.delay_seconds)}
                    disabled={!r.enabled || savingKey === r.port}
                    onChange={({ detail }) => setRows((prev) => prev.map((x) => (x.port === r.port ? { ...x, delay_seconds: detail.value } : x)))}
                    onBlur={() => handleUpdate(r, { delay_seconds: parseInt(r.delay_seconds, 10) || 60 })}
                  />
                ) : (
                  "-"
                ),
            },
          ]}
          empty={<Box textAlign="center">{deviceId ? "No interfaces configured for this device." : "Select a device."}</Box>}
        />
      </SpaceBetween>
    </Container>
  );
}

function HistoryTab({ pushFlash }) {
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState("");
  const [limit, setLimit] = useState(200);

  async function refresh(l) {
    setLoading(true);
    try {
      setHistory(await getAlertHistory(l));
    } catch (e) {
      pushFlash("error", `Could not load alert history: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh(limit);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [limit]);

  const filtered = history.filter((h) => {
    const q = filterText.toLowerCase();
    if (!q) return true;
    return h.alertname.toLowerCase().includes(q) || (h.summary || "").toLowerCase().includes(q);
  });
  const { pageItems, paginationProps } = useClientPagination(filtered, 10);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Every notification Alertmanager has sent this app's webhook receiver, firing and resolved - covers both Prometheus-rule alerts and per-interface alerts. Alertmanager's own API only shows currently-active alerts; this survives after they resolve."
        >
          Alert history
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Table
          variant="embedded"
          loading={loading}
          items={pageItems}
          filter={
            <TextFilter
              filteringText={filterText}
              onChange={({ detail }) => setFilterText(detail.filteringText)}
              filteringPlaceholder="Search alert name or summary..."
            />
          }
          pagination={<Pagination {...paginationProps} />}
          columnDefinitions={[
            { id: "time", header: "Time", cell: (h) => new Date(h.received_at).toLocaleString() },
            { id: "name", header: "Alert", cell: (h) => h.alertname },
            {
              id: "severity",
              header: "Severity",
              cell: (h) => <StatusIndicator type={severityType(h.severity)}>{h.severity || "-"}</StatusIndicator>,
            },
            {
              id: "status",
              header: "Status",
              cell: (h) => <StatusIndicator type={h.status === "firing" ? "error" : "success"}>{h.status}</StatusIndicator>,
            },
            { id: "summary", header: "Summary", cell: (h) => h.summary || "-" },
          ]}
          empty={<Box textAlign="center">No alert history yet.</Box>}
        />
        {history.length >= limit && (
          <Box textAlign="center">
            <Button loading={loading} onClick={() => setLimit(limit + 200)}>
              Load 200 more
            </Button>
          </Box>
        )}
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
                  // Without this, the dropdown's options list collapses to
                  // 0x0 when the Select sits inside a Table cell - confirmed
                  // live by inspecting the open listbox's computed
                  // getBoundingClientRect(). expandToViewport portals it to
                  // document.body instead, where it measures/renders
                  // correctly - the officially recommended fix for Select/
                  // Multiselect/Autosuggest inside Table/Modal in Cloudscape.
                  expandToViewport
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

export default function AlertsPage({ devices, pushFlash }) {
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
          {
            id: "interfaces",
            label: "Interfaces",
            content: <InterfacesTab devices={devices || []} pushFlash={pushFlash} />,
          },
          { id: "history", label: "History", content: <HistoryTab pushFlash={pushFlash} /> },
        ]}
      />
    </SpaceBetween>
  );
}
