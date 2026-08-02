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
import ColumnLayout from "@cloudscape-design/components/column-layout";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import Textarea from "@cloudscape-design/components/textarea";
import Badge from "@cloudscape-design/components/badge";
import Popover from "@cloudscape-design/components/popover";

import { useClientPagination } from "./useClientPagination.js";

import {
  createSilence,
  deleteSilence,
  getAlertHistory,
  getAlertsOverview,
  getAuditLog,
  getLiveAlerts,
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
  if (state === "pending") return "in-progress";
  if (state === "resolving") return "loading";
  if (state === "resolved") return "success";
  if (state === "expired") return "warning";
  return "pending";
}

function portStateType(state) {
  if (state === "up") return "success";
  if (state === "down") return "error";
  if (state === "admin_down") return "stopped";
  return "info";
}

const STATE_LABELS = {
  active: "current",
  pending: "pending",
  resolving: "pending resolve",
  suppressed: "silenced",
  resolved: "resolved",
  // Fired, then aged out of Alertmanager without ever sending a resolve -
  // deliberately not shown as "resolved", because nothing confirmed it
  // recovered. See _incident_state in app.py.
  expired: "expired (never resolved)",
};

function AckCell({ ack }) {
  if (!ack) return <StatusIndicator type="warning">unacknowledged</StatusIndicator>;
  const when = new Date(ack.acked_at).toLocaleString();
  const body = (
    <SpaceBetween size="xxs">
      <Box>Acknowledged by {ack.acked_by}</Box>
      <Box>{when}</Box>
      {ack.note && <Box>Note: {ack.note}</Box>}
    </SpaceBetween>
  );
  return (
    <Popover dismissButton={false} position="top" size="medium" triggerType="text" content={body}>
      <StatusIndicator type="success">
        {ack.acked_by} · {when}
      </StatusIndicator>
    </Popover>
  );
}

function ActiveAlertsTab({ alerts, loading, lastUpdated, onRefresh }) {
  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={alerts.length ? `(${alerts.length})` : undefined}
          description="What is alerting right now. 'Pending' means the condition is real but still inside its confirmation window - no alarm record exists until it actually fires. 'Pending resolve' means the condition already cleared but the alert hasn't been formally resolved yet. Acknowledge, comment and resolve on the alarm record itself."
          actions={
            <SpaceBetween direction="horizontal" size="s" alignItems="center">
              {lastUpdated && (
                <Box color="text-body-secondary" fontSize="body-s">
                  Updated {lastUpdated.toLocaleTimeString()}
                </Box>
              )}
              <Button iconName="refresh" loading={loading} onClick={onRefresh}>
                Refresh
              </Button>
            </SpaceBetween>
          }
        >
          Active alerts
        </Header>
      }
    >
      <Table
        variant="embedded"
        loading={loading}
        items={alerts}
        columnDefinitions={[
          { id: "name", header: "Alert", minWidth: 180, cell: (a) => a.labels?.alertname },
          {
            id: "severity",
            header: "Severity",
            minWidth: 110,
            cell: (a) => <StatusIndicator type={severityType(a.labels?.severity)}>{a.labels?.severity || "-"}</StatusIndicator>,
          },
          { id: "summary", header: "Summary", minWidth: 260, cell: (a) => a.annotations?.summary || "-" },
          {
            id: "state",
            header: "State",
            minWidth: 170,
            cell: (a) => (
              <StatusIndicator type={alertStateType(a.status?.state)}>
                {STATE_LABELS[a.status?.state] || a.status?.state}
              </StatusIndicator>
            ),
          },
          { id: "since", header: "Since", minWidth: 190, cell: (a) => (a.startsAt ? new Date(a.startsAt).toLocaleString() : "-") },
          { id: "ack", header: "Acknowledged", minWidth: 190, cell: (a) => <AckCell ack={a.ack} /> },
          {
            id: "record",
            header: "Alarm record",
            minWidth: 150,
            // Acknowledging/resolving happens on the occurrence, not here -
            // one place for those actions, and one place they get logged.
            // A pending alert has no record yet, by design: nothing is
            // opened until the condition actually fires.
            cell: (a) =>
              a.occurrence ? (
                <Button variant="inline-link" href={`#/alarms/${a.occurrence}`}>
                  ALM-{a.occurrence}
                </Button>
              ) : (
                <Box color="text-body-secondary">not opened yet</Box>
              ),
          },
        ]}
        empty={<Box textAlign="center">No active alerts.</Box>}
      />
    </Container>
  );
}

function OverviewTab({ overview, loading, onRefresh }) {
  const counts = overview?.counts || {};
  const severities = overview?.severities || {};
  const pipeline = overview?.pipeline || {};
  const notifications = pipeline.notifications;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description="Current alert load at a glance - what's firing right now, what's still confirming, and how much of it anyone has taken ownership of."
            actions={
              <Button iconName="refresh" loading={loading} onClick={onRefresh}>
                Refresh
              </Button>
            }
          >
            Alert status
          </Header>
        }
      >
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Current</Box>
            <Box fontSize="display-l" fontWeight="bold" color={counts.current ? "text-status-error" : "text-status-success"}>
              {counts.current ?? 0}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              Firing now
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Pending</Box>
            <Box fontSize="display-l" fontWeight="bold" color={counts.pending ? "text-status-warning" : "inherit"}>
              {counts.pending ?? 0}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              Real, still confirming
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Pending resolve</Box>
            <Box fontSize="display-l" fontWeight="bold">
              {counts.resolving ?? 0}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              Cleared, not yet closed
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Unacknowledged</Box>
            <Box fontSize="display-l" fontWeight="bold" color={overview?.unacknowledged ? "text-status-warning" : "inherit"}>
              {overview?.unacknowledged ?? 0}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              {overview?.acknowledged ?? 0} acknowledged
            </Box>
          </div>
        </ColumnLayout>
      </Container>

      <Container header={<Header variant="h2">Breakdown</Header>}>
        <KeyValuePairs
          columns={4}
          items={[
            {
              label: "Critical",
              value: <StatusIndicator type={severities.critical ? "error" : "success"}>{severities.critical ?? 0}</StatusIndicator>,
            },
            {
              label: "Warning",
              value: <StatusIndicator type={severities.warning ? "warning" : "success"}>{severities.warning ?? 0}</StatusIndicator>,
            },
            { label: "Silenced", value: counts.suppressed ?? 0 },
            { label: "Active maintenance windows", value: overview?.active_silences ?? 0 },
          ]}
        />
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            description="Whether the alerting pipeline itself is healthy. This matters because every count above reads zero both when nothing is wrong and when the thing that detects problems is down."
          >
            Pipeline status
          </Header>
        }
      >
        <SpaceBetween size="m">
          <KeyValuePairs
            columns={3}
            items={[
              {
                label: "Alertmanager",
                value: pipeline.alertmanager_ok ? (
                  <StatusIndicator type="success">reachable</StatusIndicator>
                ) : (
                  <StatusIndicator type="error">{pipeline.alertmanager_error || "unreachable"}</StatusIndicator>
                ),
              },
              {
                label: "Prometheus",
                value: pipeline.prometheus_ok ? (
                  <StatusIndicator type="success">reachable</StatusIndicator>
                ) : (
                  <StatusIndicator type="error">unreachable</StatusIndicator>
                ),
              },
              {
                label: "Last checked",
                value: overview?.generated_at ? new Date(overview.generated_at).toLocaleString() : "-",
              },
            ]}
          />
          {notifications && (
            <div>
              <Box variant="awsui-key-label">Notifications delivered</Box>
              <SpaceBetween direction="horizontal" size="xs">
                {Object.keys(notifications.sent || {}).length === 0 && <Box color="text-body-secondary">None yet.</Box>}
                {Object.entries(notifications.sent || {}).map(([integration, n]) => (
                  <Badge key={integration} color="green">
                    {integration}: {n}
                  </Badge>
                ))}
                {Object.entries(notifications.failed || {}).map(([integration, n]) => (
                  <Badge key={`${integration}-failed`} color="red">
                    {integration} failed: {n}
                  </Badge>
                ))}
              </SpaceBetween>
            </div>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}

function AuditLogTab({ pushFlash }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState("");

  async function refresh() {
    setLoading(true);
    try {
      setEntries(await getAuditLog(500));
    } catch (e) {
      pushFlash("error", `Could not load audit log: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = entries.filter((e) => {
    const q = filterText.toLowerCase();
    if (!q) return true;
    return (
      e.action.toLowerCase().includes(q) ||
      e.actor.toLowerCase().includes(q) ||
      (e.target || "").toLowerCase().includes(q)
    );
  });
  const { pageItems, paginationProps } = useClientPagination(filtered, 15);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="What people did in Switchboard - acknowledgements, manual resolves, maintenance windows, and alert config changes, with who and when. The alert history tab covers the other half: what the system did."
          actions={
            <Button iconName="refresh" loading={loading} onClick={refresh}>
              Refresh
            </Button>
          }
        >
          Audit &amp; event log
        </Header>
      }
    >
      <Table
        variant="embedded"
        loading={loading}
        items={pageItems}
        filter={
          <TextFilter
            filteringText={filterText}
            onChange={({ detail }) => setFilterText(detail.filteringText)}
            filteringPlaceholder="Search action, user or target..."
          />
        }
        pagination={<Pagination {...paginationProps} />}
        columnDefinitions={[
          { id: "ts", header: "Time", cell: (e) => new Date(e.ts).toLocaleString() },
          { id: "actor", header: "User", cell: (e) => e.actor },
          { id: "action", header: "Action", cell: (e) => <Badge>{e.action}</Badge> },
          { id: "target", header: "Target", cell: (e) => e.target || "-" },
          {
            id: "detail",
            header: "Detail",
            cell: (e) => {
              if (!e.detail) return "-";
              if (e.detail.note) return e.detail.note;
              if (e.detail.comment) return e.detail.comment;
              // Skip null/undefined values - an action taken without an
              // optional note used to render a literal "note=null", which
              // reads like a recorded value rather than the absence of one.
              const parts = Object.entries(e.detail)
                .filter(([k, v]) => k !== "labels" && v !== null && v !== undefined)
                .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
              return parts.length ? parts.join(", ") : "-";
            },
          },
        ]}
        empty={<Box textAlign="center">Nothing recorded yet.</Box>}
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
            {
              id: "ack",
              header: "Acknowledged",
              // The ack that was in effect at the moment this notification
              // was sent (replayed from the audit log), not whatever the
              // current ack happens to be - a later recurrence of the same
              // fault is a different incident and shouldn't retroactively
              // mark this one as owned.
              cell: (h) => (h.ack ? <AckCell ack={h.ack} /> : <Box color="text-body-secondary">-</Box>),
            },
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

function ForSecondsCell({ rule, savingName, onSave }) {
  // Same "local draft, commit on blur" shape as PageDelayCell below, but
  // simpler: every rule always has a real for_seconds value (0 is valid -
  // fire instantly, no confirmation window) so there's no "use the
  // default" state to track here.
  const [draft, setDraft] = useState(String(rule.for_seconds ?? 0));
  const busy = savingName === rule.name;

  function commit() {
    const seconds = parseInt(draft.trim(), 10);
    if (Number.isNaN(seconds) || seconds < 0) {
      setDraft(String(rule.for_seconds ?? 0));
      return;
    }
    if (seconds === rule.for_seconds) return;
    onSave(rule.name, { for_seconds: seconds });
  }

  return (
    <Input
      type="number"
      value={draft}
      disabled={busy}
      onChange={({ detail }) => setDraft(detail.value)}
      onBlur={commit}
    />
  );
}

function PageDelayCell({ rule, appDefault, savingName, onSave }) {
  // A rule's own value while it's being typed, kept separate from `rule`
  // so a slow save doesn't fight the input while the user is still typing.
  const [draft, setDraft] = useState(
    rule.page_delay_seconds === null || rule.page_delay_seconds === undefined
      ? ""
      : String(rule.page_delay_seconds)
  );
  const usingDefault = rule.page_delay_seconds === null || rule.page_delay_seconds === undefined;
  const busy = savingName === rule.name;

  function commit() {
    const trimmed = draft.trim();
    if (trimmed === "") {
      onSave(rule.name, { use_default_page_delay: true });
      return;
    }
    const seconds = parseInt(trimmed, 10);
    if (Number.isNaN(seconds) || seconds < 0) {
      setDraft(usingDefault ? "" : String(rule.page_delay_seconds));
      return;
    }
    if (seconds === rule.page_delay_seconds) return;
    onSave(rule.name, { page_delay_seconds: seconds });
  }

  return (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      <Input
        type="number"
        value={draft}
        placeholder={`default (${appDefault}s)`}
        disabled={busy}
        onChange={({ detail }) => setDraft(detail.value)}
        onBlur={commit}
      />
      {!usingDefault && (
        <Button
          variant="inline-link"
          disabled={busy}
          onClick={() => {
            setDraft("");
            onSave(rule.name, { use_default_page_delay: true });
          }}
        >
          Use default
        </Button>
      )}
    </SpaceBetween>
  );
}

function RulesTab({ pushFlash, pageDelaySeconds }) {
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
          description="Severity drives Pushover priority (critical = high priority). Disabling a rule removes it from the live Prometheus config, not just this view. 'For' is how long the condition must hold before it counts as firing (0 = instantly) - the PromQL expression itself isn't editable here, see prometheus/alerts.yml in the repo for that."
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
              header: "For (pending time)",
              minWidth: 150,
              // How long the condition must hold before this rule counts
              // as firing - Prometheus's `for:`, shown as "pending" on the
              // Alerts/Alarms pages while it's counting down. 0 = fire
              // instantly, no confirmation window.
              cell: (r) => <ForSecondsCell rule={r} savingName={savingName} onSave={handleUpdate} />,
            },
            {
              id: "page_delay",
              header: "Page delay",
              minWidth: 220,
              // How long this rule's alarms are held before paging (see
              // paging.py / the Alarms page countdown). Blank = the
              // app-wide default shown as placeholder text, not "0s" - a
              // rule nobody has touched must not silently start paging
              // instantly just because this column exists.
              cell: (r) => (
                <PageDelayCell rule={r} appDefault={pageDelaySeconds} savingName={savingName} onSave={handleUpdate} />
              ),
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
  const [activeTab, setActiveTab] = useState("overview");
  const [lastUpdated, setLastUpdated] = useState(null);
  const [overview, setOverview] = useState(null);

  async function refresh() {
    setLoading(true);
    try {
      const [a, s, o] = await Promise.all([getLiveAlerts(), listSilences(), getAlertsOverview()]);
      setAlerts(a);
      setSilences(s);
      setOverview(o);
      setError(null);
      setLastUpdated(new Date());
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
          {
            id: "overview",
            label: "Overview",
            content: <OverviewTab overview={overview} loading={loading} onRefresh={refresh} />,
          },
          {
            id: "active",
            label: "Active alerts",
            content: (
              <ActiveAlertsTab alerts={alerts} loading={loading} lastUpdated={lastUpdated} onRefresh={refresh} />
            ),
          },
          {
            id: "maintenance",
            label: "Maintenance windows",
            content: <MaintenanceWindowsTab silences={silences} loading={loading} pushFlash={pushFlash} refresh={refresh} />,
          },
          {
            id: "rules",
            label: "Rules",
            content: <RulesTab pushFlash={pushFlash} pageDelaySeconds={overview?.page_delay_seconds ?? 120} />,
          },
          {
            id: "interfaces",
            label: "Interfaces",
            content: <InterfacesTab devices={devices || []} pushFlash={pushFlash} />,
          },
          { id: "history", label: "History", content: <HistoryTab pushFlash={pushFlash} /> },
          { id: "audit", label: "Audit log", content: <AuditLogTab pushFlash={pushFlash} /> },
        ]}
      />
    </SpaceBetween>
  );
}
