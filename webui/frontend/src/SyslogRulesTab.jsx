// The "Syslog rules" tab on the Events page: the fast path's state and a
// self-test that times it, plus the rules that turn log lines into events.
//
// The self-test is the honest number: one syslog line sent to the
// receiver, timed back through Vector into Switchboard, raised as an
// event and pushed to phones - the whole path a real switch message takes,
// minus the switch.
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
import Toggle from "@cloudscape-design/components/toggle";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import Textarea from "@cloudscape-design/components/textarea";
import Badge from "@cloudscape-design/components/badge";
import Alert from "@cloudscape-design/components/alert";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import ColumnLayout from "@cloudscape-design/components/column-layout";

import {
  getFastPath,
  testFastPath,
  listSyslogRules,
  createSyslogRule,
  updateSyslogRule,
  deleteSyslogRule,
  matchSyslogRules,
} from "./api.js";

const SEVERITIES = [
  { label: "info", value: "info" },
  { label: "warning", value: "warning" },
  { label: "critical", value: "critical" },
];

function severityType(s) {
  return s === "critical" ? "error" : s === "warning" ? "warning" : "info";
}

function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

function ms(v) {
  if (v === null || v === undefined) return "-";
  return v < 1000 ? `${v} ms` : `${(v / 1000).toFixed(2)} s`;
}

// --- Fast path -------------------------------------------------------------

export function FastPathCard({ pushFlash }) {
  const [status, setStatus] = useState(null);
  const [severity, setSeverity] = useState(SEVERITIES[1]);
  const [testing, setTesting] = useState(false);

  async function refresh() {
    try {
      setStatus(await getFastPath());
    } catch (e) {
      pushFlash("error", `Could not load the fast-path status: ${e.message}`);
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runTest() {
    setTesting(true);
    try {
      const r = await testFastPath(severity.value);
      if (r.ok) {
        pushFlash(
          "success",
          `Fast path round trip: received in ${ms(r.received_ms)}, event raised in ${ms(r.event_ms)}` +
            (r.pushed_devices ? `, pushed to ${r.pushed_devices} device(s) in ${ms(r.push_ms)}` : ", no enrolled device at this severity") +
            ". The test event resolves itself in a minute."
        );
      } else {
        pushFlash("error", `Fast-path test failed: ${r.detail || "no event raised"}`);
      }
      refresh();
    } catch (e) {
      pushFlash("error", `Fast-path test failed: ${e.message}`);
    } finally {
      setTesting(false);
    }
  }

  let state;
  if (!status) state = <StatusIndicator type="loading">loading</StatusIndicator>;
  else if (!status.configured) state = <StatusIndicator type="error">not configured (SYSLOG_INGEST_TOKEN)</StatusIndicator>;
  else if (!status.last_received_at) state = <StatusIndicator type="warning">configured, nothing received yet</StatusIndicator>;
  else {
    const age = (Date.now() - new Date(status.last_received_at)) / 1000;
    state =
      age <= status.stale_after_seconds ? (
        <StatusIndicator type="success">receiving</StatusIndicator>
      ) : (
        <StatusIndicator type="warning">quiet for {ago(status.last_received_at).replace(" ago", "")}</StatusIndicator>
      );
  }
  const t = status?.last_test;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Vector posts every parsed syslog line here the moment it arrives and it becomes an event in well under a second. Loki polling remains as the fallback for syslog, and the SSH poll for what syslog never said."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Select selectedOption={severity} onChange={({ detail }) => setSeverity(detail.selectedOption)} options={SEVERITIES} />
              <Button onClick={runTest} loading={testing} disabled={!status?.configured}>
                Send a test
              </Button>
            </SpaceBetween>
          }
        >
          Fast path
        </Header>
      }
    >
      <ColumnLayout columns={2} variant="text-grid">
        <KeyValuePairs
          columns={1}
          items={[
            { label: "Status", value: state },
            {
              label: "Last event",
              value: status?.last_received_at ? `${ago(status.last_received_at)} from ${status.last_host || "?"}` : "none since start",
            },
            { label: "Rate", value: status ? `${status.events_last_minute} events in the last minute (${status.total} since start)` : "-" },
            {
              label: "Vector to Switchboard",
              value:
                status?.transport_ms_median != null ? `median ${ms(status.transport_ms_median)}, p95 ${ms(status.transport_ms_p95)}` : "-",
            },
            { label: "Events from syslog", value: status ? `${status.syslog_transitions} transitions; ${status.ignored} lines ignored by setting` : "-" },
            { label: "Events from the SSH poll", value: status ? `${status.ssh_transitions} transitions` : "-" },
          ]}
        />
        <KeyValuePairs
          columns={1}
          items={[
            { label: "Receiver for the test", value: status?.receiver || "not set (Settings)" },
            {
              label: "Last test",
              value: t ? (
                <SpaceBetween size="xxs">
                  <StatusIndicator type={t.ok ? "success" : "error"}>
                    {t.ok ? "passed" : "failed"} {ago(t.sent_at)} ({t.severity}, by {t.by})
                  </StatusIndicator>
                  {t.ok ? (
                    <Box color="text-body-secondary">
                      received {ms(t.received_ms)} · event {ms(t.event_ms)} ·{" "}
                      {t.pushed_devices ? `pushed to ${t.pushed_devices} device(s) ${ms(t.push_ms)}` : "no device notified"}
                    </Box>
                  ) : (
                    <Box color="text-body-secondary">{t.detail}</Box>
                  )}
                </SpaceBetween>
              ) : (
                "none yet"
              ),
            },
            {
              label: "Rule events open",
              value: status?.rule_events_open?.length ? (
                <SpaceBetween size="xxs">
                  {status.rule_events_open.map((a) => (
                    <Box key={a.id}>
                      <StatusIndicator type={severityType(a.severity)}>{a.subject}</StatusIndicator>{" "}
                      <Box variant="span" color="text-body-secondary">{a.device}</Box>
                    </Box>
                  ))}
                </SpaceBetween>
              ) : (
                "none"
              ),
            },
          ]}
        />
      </ColumnLayout>
    </Container>
  );
}

// --- Rules -------------------------------------------------------------------

const EMPTY = {
  name: "",
  enabled: true,
  severity: "warning",
  facility: "",
  mnemonic: "",
  pattern: "",
  clear_pattern: "",
  per_interface: false,
  auto_resolve_seconds: 0,
};

function RuleForm({ rule, onClose, onSaved, pushFlash }) {
  const [form, setForm] = useState(rule ? { ...EMPTY, ...rule } : EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [sample, setSample] = useState("");
  const [sampleResult, setSampleResult] = useState(null);

  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }));

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const body = { ...form, auto_resolve_seconds: Number(form.auto_resolve_seconds) || 0 };
      const saved = rule?.id ? await updateSyslogRule(rule.id, body) : await createSyslogRule(body);
      onSaved(saved);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function tryLine() {
    try {
      const r = await matchSyslogRules({ message: sample, facility: form.facility, mnemonic: form.mnemonic });
      setSampleResult(r);
    } catch (e) {
      pushFlash("error", `Could not test the line: ${e.message}`);
    }
  }

  return (
    <Modal
      visible
      onDismiss={onClose}
      size="large"
      header={rule?.id ? `Edit rule: ${rule.name}` : "New syslog rule"}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" onClick={save} loading={saving}>
              {rule?.id ? "Save" : "Create"}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error && (
          <Alert type="error" header="Not saved">
            {error}
          </Alert>
        )}
        <ColumnLayout columns={2}>
          <FormField label="Name" description="Becomes the event's subject." constraintText="Up to 120 characters.">
            <Input value={form.name} onChange={({ detail }) => set("name")(detail.value)} disabled={rule?.builtin} />
          </FormField>
          <FormField label="Severity" description="Decides which enrolled phones are notified.">
            <Select
              selectedOption={SEVERITIES.find((s) => s.value === form.severity)}
              onChange={({ detail }) => set("severity")(detail.selectedOption.value)}
              options={SEVERITIES}
            />
          </FormField>
          <FormField label="Facility" description="Exact match on the parsed facility (Dell: the %FACILITY; Junos: the daemon name). Blank = any." constraintText="e.g. STP, BGP, CHASSISD">
            <Input value={form.facility} onChange={({ detail }) => set("facility")(detail.value.toUpperCase())} />
          </FormField>
          <FormField label="Mnemonic" description="Exact match on the message mnemonic/tag. Blank = any." constraintText="e.g. TOPOLOGY_CHANGE, SNMP_TRAP_LINK_DOWN">
            <Input value={form.mnemonic} onChange={({ detail }) => set("mnemonic")(detail.value.toUpperCase())} />
          </FormField>
          <FormField label="Fires on (regular expression)" description="Run over the whole message. Blank = every message that passes the facility/mnemonic filters." constraintText="Python syntax; (?i) for case-insensitive.">
            <Input value={form.pattern} onChange={({ detail }) => set("pattern")(detail.value)} />
          </FormField>
          <FormField label="Clears on (regular expression)" description="A matching line resolves the event. Blank = only the timer below ends it.">
            <Input value={form.clear_pattern} onChange={({ detail }) => set("clear_pattern")(detail.value)} />
          </FormField>
          <FormField label="Auto-resolve after" description="Seconds after the last matching line before the event resolves by itself. 0 = never (needs a clearing pattern)." constraintText="0 to 86400">
            <Input type="number" value={String(form.auto_resolve_seconds)} onChange={({ detail }) => set("auto_resolve_seconds")(detail.value)} />
          </FormField>
          <SpaceBetween size="s">
            <Toggle checked={form.per_interface} onChange={({ detail }) => set("per_interface")(detail.checked)}>
              One event per device and interface (when the line names one)
            </Toggle>
            <Toggle checked={form.enabled} onChange={({ detail }) => set("enabled")(detail.checked)}>
              Enabled
            </Toggle>
          </SpaceBetween>
        </ColumnLayout>
        <ExpandableSection headerText="Try a real log line against every rule">
          <SpaceBetween size="s">
            <Textarea
              value={sample}
              onChange={({ detail }) => setSample(detail.value)}
              placeholder="%STP-5-TOPOLOGY_CHANGE: Topology change on Vlan 10"
              rows={2}
            />
            <Button onClick={tryLine} disabled={!sample.trim()}>
              Test line
            </Button>
            {sampleResult && (
              <Box>
                <Box color="text-body-secondary">
                  parsed facility {sampleResult.parsed.facility || "-"}, mnemonic {sampleResult.parsed.mnemonic || "-"}
                </Box>
                {sampleResult.matches.length === 0 ? (
                  <StatusIndicator type="stopped">no rule fires or clears on this line</StatusIndicator>
                ) : (
                  <SpaceBetween direction="horizontal" size="xs">
                    {sampleResult.matches.map((m) => (
                      <StatusIndicator key={m.id} type={m.verdict === "fires" ? "warning" : "success"}>
                        {m.name}: {m.verdict}
                        {m.enabled ? "" : " (disabled)"}
                      </StatusIndicator>
                    ))}
                  </SpaceBetween>
                )}
              </Box>
            )}
          </SpaceBetween>
        </ExpandableSection>
      </SpaceBetween>
    </Modal>
  );
}

export default function SyslogRulesTab({ pushFlash }) {
  const [rules, setRules] = useState([]);
  const [active, setActive] = useState([]);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(null); // null | {} | rule
  const [busyId, setBusyId] = useState(null);

  async function refresh() {
    setLoading(true);
    try {
      const r = await listSyslogRules();
      setRules(r.rules);
      setActive(r.active || []);
    } catch (e) {
      pushFlash("error", `Could not load syslog rules: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function toggle(rule, enabled) {
    setBusyId(rule.id);
    try {
      const saved = await updateSyslogRule(rule.id, { enabled });
      setRules((prev) => prev.map((r) => (r.id === rule.id ? saved : r)));
    } catch (e) {
      pushFlash("error", `Could not update ${rule.name}: ${e.message}`);
    } finally {
      setBusyId(null);
    }
  }

  async function remove(rule) {
    if (!window.confirm(`Delete the rule "${rule.name}"? Any event it raised resolves.`)) return;
    setBusyId(rule.id);
    try {
      await deleteSyslogRule(rule.id);
      setRules((prev) => prev.filter((r) => r.id !== rule.id));
      pushFlash("success", `Deleted ${rule.name}`);
    } catch (e) {
      pushFlash("error", `Could not delete ${rule.name}: ${e.message}`);
    } finally {
      setBusyId(null);
    }
  }

  const firingByRule = active.reduce((acc, a) => {
    const id = a.labels?.rule_id;
    if (id) acc[id] = (acc[id] || 0) + 1;
    return acc;
  }, {});

  return (
    <SpaceBetween size="l">
      <FastPathCard pushFlash={pushFlash} />
      <Container
        header={
          <Header
            variant="h2"
            description="Turn a log line the catalogue does not know into an event the moment it arrives. Match on the parsed facility or mnemonic, a pattern over the message, or both; end it with a clearing line, a timer, or both."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={refresh} loading={loading} ariaLabel="Refresh" />
                <Button variant="primary" onClick={() => setEditing({})}>
                  Add rule
                </Button>
              </SpaceBetween>
            }
          >
            Syslog rules
          </Header>
        }
      >
        <Table
          variant="embedded"
          wrapLines
          items={rules}
          loading={loading}
          loadingText="Loading rules"
          trackBy="id"
          empty={<Box textAlign="center">No rules yet. Add one, or paste a log line into a new rule to see how it parses.</Box>}
          columnDefinitions={[
            {
              id: "enabled",
              header: "On",
              width: 70,
              cell: (r) => <Toggle checked={r.enabled} disabled={busyId === r.id} onChange={({ detail }) => toggle(r, detail.checked)} />,
            },
            { id: "name", header: "Rule", minWidth: 180, cell: (r) => r.name },
            {
              id: "severity",
              header: "Severity",
              minWidth: 110,
              cell: (r) => <StatusIndicator type={severityType(r.severity)}>{r.severity}</StatusIndicator>,
            },
            {
              id: "match",
              header: "Fires on",
              minWidth: 200,
              maxWidth: 360,
              cell: (r) => (
                <SpaceBetween size="xxs">
                  {(r.facility || r.mnemonic) && (
                    <SpaceBetween direction="horizontal" size="xxs">
                      {r.facility && <Badge>{r.facility}</Badge>}
                      {r.mnemonic && <Badge color="blue">{r.mnemonic}</Badge>}
                    </SpaceBetween>
                  )}
                  {r.pattern && <Box variant="code">{r.pattern}</Box>}
                  {!r.facility && !r.mnemonic && !r.pattern && "-"}
                </SpaceBetween>
              ),
            },
            {
              id: "ends",
              header: "Ends",
              minWidth: 160,
              maxWidth: 300,
              cell: (r) => (
                <SpaceBetween size="xxs">
                  {r.clear_pattern && (
                    <Box>
                      clears on <Box variant="code">{r.clear_pattern}</Box>
                    </Box>
                  )}
                  {r.auto_resolve_seconds ? <Box>after {r.auto_resolve_seconds}s</Box> : null}
                  {!r.clear_pattern && !r.auto_resolve_seconds && "never by itself"}
                </SpaceBetween>
              ),
            },
            { id: "scope", header: "Scope", width: 150, cell: (r) => (r.per_interface ? "device + interface" : "device") },
            {
              id: "firing",
              header: "Firing",
              width: 90,
              cell: (r) => (firingByRule[String(r.id)] ? <StatusIndicator type="warning">{firingByRule[String(r.id)]}</StatusIndicator> : "-"),
            },
            {
              id: "actions",
              header: "Actions",
              width: 140,
              cell: (r) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="inline-link" onClick={() => setEditing(r)}>
                    Edit
                  </Button>
                  {!r.builtin && (
                    <Button variant="inline-link" onClick={() => remove(r)} disabled={busyId === r.id}>
                      Delete
                    </Button>
                  )}
                </SpaceBetween>
              ),
            },
          ]}
        />
      </Container>
      {editing && (
        <RuleForm
          rule={editing.id ? editing : null}
          pushFlash={pushFlash}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            setRules((prev) => (prev.some((r) => r.id === saved.id) ? prev.map((r) => (r.id === saved.id ? saved : r)) : [...prev, saved]));
            setEditing(null);
            pushFlash("success", `Saved ${saved.name}`);
          }}
        />
      )}
    </SpaceBetween>
  );
}
