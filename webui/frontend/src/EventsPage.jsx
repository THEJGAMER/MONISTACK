// Events: what the devices did, when, how bad, and whether it is over.
//
// The page is a live stream (open events first), a catalogue of every
// event kind with the site's severity for it, per-port link severities,
// the syslog rules and fast path, and the audit log. Nothing here
// acknowledges or silences: actioning belongs to the ticketing system that
// consumes the same events over webhooks.
import React, { useEffect, useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import Select from "@cloudscape-design/components/select";
import Multiselect from "@cloudscape-design/components/multiselect";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import FormField from "@cloudscape-design/components/form-field";
import Tabs from "@cloudscape-design/components/tabs";
import Toggle from "@cloudscape-design/components/toggle";
import Pagination from "@cloudscape-design/components/pagination";
import TextFilter from "@cloudscape-design/components/text-filter";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import Badge from "@cloudscape-design/components/badge";
import Textarea from "@cloudscape-design/components/textarea";
import Link from "@cloudscape-design/components/link";

import { useClientPagination } from "./useClientPagination.js";
import SyslogRulesTab from "./SyslogRulesTab.jsx";
import AuditLogTab from "./AuditLogTab.jsx";
import {
  listEvents,
  getEvent,
  resolveEvent,
  getEventCatalog,
  updateEventKind,
  resetEventKind,
  getPortSettings,
  setPortSeverity,
} from "./api.js";

const SEVERITY_OPTIONS = [
  { label: "critical", value: "critical" },
  { label: "warning", value: "warning" },
  { label: "info", value: "info" },
];
const CHOICE_OPTIONS = [...SEVERITY_OPTIONS, { label: "ignore", value: "ignore" }];
const SOURCE_LABELS = { syslog: "syslog", loki: "syslog (Loki poll)", ssh: "SSH poll", switchboard: "Switchboard", timer: "timer" };

function severityType(s) {
  return s === "critical" ? "error" : s === "warning" ? "warning" : s === "ignore" ? "stopped" : "info";
}

function fmt(iso) {
  return iso ? new Date(iso).toLocaleString() : "-";
}

function duration(from, to) {
  if (!from) return "-";
  const ms = (to ? new Date(to) : new Date()) - new Date(from);
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
  return `${Math.round(s / 86400)} d`;
}

function StateCell({ ev }) {
  if (ev.resolved_at) return <StatusIndicator type="success">resolved by {ev.resolved_by || "-"}</StatusIndicator>;
  return <StatusIndicator type={severityType(ev.severity)}>open {duration(ev.raised_at)}</StatusIndicator>;
}

// One episode that keeps returning is one row, not a pile of them - so
// how often it came back is the number worth showing.
function ReportsCell({ ev }) {
  if (!ev.reopen_count) return ev.count;
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      <Box>{ev.count}</Box>
      <Badge color="severity-medium">came back {ev.reopen_count}x</Badge>
    </SpaceBetween>
  );
}

// --- the stream ------------------------------------------------------------

function EventDetail({ eventId, onClose, pushFlash, onResolved }) {
  const [ev, setEv] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    getEvent(eventId)
      .then((e) => alive && setEv(e))
      .catch((e) => pushFlash("error", `Could not load event ${eventId}: ${e.message}`));
    return () => {
      alive = false;
    };
  }, [eventId, pushFlash]);

  async function resolve() {
    setBusy(true);
    try {
      const r = await resolveEvent(eventId, note);
      setEv(r);
      onResolved(r);
      pushFlash("success", `Resolved ${r.title}`);
    } catch (e) {
      pushFlash("error", `Could not resolve: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      visible
      onDismiss={onClose}
      size="large"
      header={ev ? `${ev.kind_name}: ${ev.subject || ev.device}` : `Event ${eventId}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onClose}>
              Close
            </Button>
            {ev && !ev.resolved_at && (
              <Button variant="primary" onClick={resolve} loading={busy}>
                Resolve
              </Button>
            )}
          </SpaceBetween>
        </Box>
      }
    >
      {!ev ? (
        <StatusIndicator type="loading">loading</StatusIndicator>
      ) : (
        <SpaceBetween size="m">
          <KeyValuePairs
            columns={3}
            items={[
              { label: "State", value: <StateCell ev={ev} /> },
              { label: "Severity", value: <StatusIndicator type={severityType(ev.severity)}>{ev.severity}</StatusIndicator> },
              { label: "Device", value: ev.device },
              { label: "Subject", value: ev.subject || "-" },
              { label: "Kind", value: `${ev.kind_name} (${ev.kind})` },
              { label: "Detected by", value: SOURCE_LABELS[ev.source] || ev.source },
              { label: "Raised", value: fmt(ev.raised_at) },
              { label: "Last reported", value: `${fmt(ev.last_seen_at)} (${ev.count} time${ev.count === 1 ? "" : "s"})` },
              {
                label: "Came back",
                value: ev.reopen_count
                  ? `${ev.reopen_count} time${ev.reopen_count === 1 ? "" : "s"}, last ${fmt(ev.reopened_at)}`
                  : "not since it was raised",
              },
              { label: "Device time", value: ev.signal_at ? fmt(ev.signal_at) : "-" },
              { label: "Resolved", value: ev.resolved_at ? `${fmt(ev.resolved_at)} by ${ev.resolved_by}` : "not yet" },
              { label: "Lasted", value: duration(ev.raised_at, ev.resolved_at) },
              { label: "Resolve detail", value: ev.resolve_detail || "-" },
            ]}
          />
          <FormField label="What the device said" stretch>
            <Box variant="code">{ev.detail || "-"}</Box>
          </FormField>
          {Object.keys(ev.labels || {}).length > 0 && (
            <SpaceBetween direction="horizontal" size="xxs">
              {Object.entries(ev.labels).map(([k, v]) => (
                <Badge key={k}>
                  {k}={String(v)}
                </Badge>
              ))}
            </SpaceBetween>
          )}
          {!ev.resolved_at && (
            <FormField label="Resolve note" description="Optional. Resolving here is a correction, not an action: if the condition is still true the device will raise it again.">
              <Textarea value={note} onChange={({ detail }) => setNote(detail.value)} rows={2} />
            </FormField>
          )}
          <Box color="text-body-secondary">
            Share: <Link href={`#/events/${ev.id}`}>#/events/{ev.id}</Link>
          </Box>
        </SpaceBetween>
      )}
    </Modal>
  );
}

function StreamTab({ devices, pushFlash, eventId, onNavigate }) {
  const [events, setEvents] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [openOnly, setOpenOnly] = useState(true);
  const [severities, setSeverities] = useState([]);
  const [device, setDevice] = useState(null);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState(eventId ? Number(eventId) : null);

  const deviceOptions = [{ label: "All devices", value: "" }, ...(devices || []).map((d) => ({ label: d.name, value: d.id }))];

  async function refresh(quiet = false) {
    if (!quiet) setLoading(true);
    try {
      const r = await listEvents({
        open_only: openOnly,
        severity: severities.map((s) => s.value).join(","),
        device_id: device?.value || "",
        q,
        limit: 300,
      });
      setEvents(r.events);
      setSummary(r.summary);
    } catch (e) {
      pushFlash("error", `Could not load events: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(() => refresh(true), 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openOnly, severities, device, q]);

  useEffect(() => {
    setSelected(eventId ? Number(eventId) : null);
  }, [eventId]);

  const { pageItems, paginationProps } = useClientPagination(events, 25);

  const counters = summary
    ? [
        { label: "Open critical", value: <StatusIndicator type={summary.open.critical ? "error" : "success"}>{summary.open.critical}</StatusIndicator> },
        { label: "Open warning", value: <StatusIndicator type={summary.open.warning ? "warning" : "success"}>{summary.open.warning}</StatusIndicator> },
        { label: "Open info", value: <StatusIndicator type="info">{summary.open.info}</StatusIndicator> },
        { label: "Raised in the last 24 h", value: `${summary.last_24h.critical} critical, ${summary.last_24h.warning} warning, ${summary.last_24h.info} info` },
      ]
    : [];

  return (
    <SpaceBetween size="l">
      <Container>
        <KeyValuePairs columns={4} items={counters} />
      </Container>
      <Table
        variant="container"
        loading={loading}
        loadingText="Loading events"
        items={pageItems}
        trackBy="id"
        wrapLines
        header={
          <Header
            variant="h2"
            counter={`(${events.length})`}
            description="Open events first. Every row is one episode: raised once, counted while the device keeps reporting it, resolved once - by the device, the SSH poll, a timer, or you. A fault that returns soon after clearing re-opens its own row rather than starting a new one."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Toggle checked={openOnly} onChange={({ detail }) => setOpenOnly(detail.checked)}>
                  Open only
                </Toggle>
                <Button iconName="refresh" onClick={() => refresh()} ariaLabel="Refresh" />
              </SpaceBetween>
            }
          >
            Events
          </Header>
        }
        filter={
          <ColumnLayout columns={3}>
            <TextFilter filteringText={q} onChange={({ detail }) => setQ(detail.filteringText)} filteringPlaceholder="Search device, subject, text..." />
            <Multiselect
              selectedOptions={severities}
              onChange={({ detail }) => setSeverities(detail.selectedOptions)}
              options={SEVERITY_OPTIONS}
              placeholder="Any severity"
              inlineTokens
            />
            <Select selectedOption={device || deviceOptions[0]} onChange={({ detail }) => setDevice(detail.selectedOption)} options={deviceOptions} />
          </ColumnLayout>
        }
        pagination={<Pagination {...paginationProps} />}
        empty={<Box textAlign="center">{openOnly ? "Nothing open. Everything the devices reported has resolved." : "No events match."}</Box>}
        columnDefinitions={[
          { id: "sev", header: "Severity", width: 110, cell: (e) => <StatusIndicator type={severityType(e.severity)}>{e.severity}</StatusIndicator> },
          { id: "raised", header: "Raised", width: 170, cell: (e) => fmt(e.raised_at) },
          { id: "device", header: "Device", width: 150, cell: (e) => e.device },
          { id: "kind", header: "Event", width: 190, cell: (e) => e.kind_name },
          {
            id: "what",
            header: "What",
            minWidth: 240,
            cell: (e) => (
              <Link
                onFollow={(ev) => {
                  ev.preventDefault();
                  setSelected(e.id);
                }}
                href={`#/events/${e.id}`}
              >
                {e.title}
              </Link>
            ),
          },
          { id: "state", header: "State", width: 170, cell: (e) => <StateCell ev={e} /> },
          { id: "src", header: "Via", width: 120, cell: (e) => SOURCE_LABELS[e.source] || e.source },
          { id: "count", header: "Reports", width: 150, cell: (e) => <ReportsCell ev={e} /> },
        ]}
      />
      {selected && (
        <EventDetail
          eventId={selected}
          pushFlash={pushFlash}
          onClose={() => {
            setSelected(null);
            if (eventId && onNavigate) onNavigate("#/events");
          }}
          onResolved={() => refresh(true)}
        />
      )}
    </SpaceBetween>
  );
}

// --- the catalogue --------------------------------------------------------------

function CatalogTab({ pushFlash }) {
  const [groups, setGroups] = useState([]);
  const [kinds, setKinds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [drafts, setDrafts] = useState({});

  async function refresh() {
    setLoading(true);
    try {
      const r = await getEventCatalog();
      setGroups(r.groups);
      setKinds(r.kinds);
    } catch (e) {
      pushFlash("error", `Could not load the catalogue: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save(kind, body) {
    setBusy(kind);
    try {
      const updated = await updateEventKind(kind, body);
      setKinds((prev) => prev.map((k) => (k.kind === kind ? updated : k)));
      setDrafts((d) => ({ ...d, [kind]: undefined }));
    } catch (e) {
      pushFlash("error", `Could not update ${kind}: ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  async function reset(kind) {
    setBusy(kind);
    try {
      const updated = await resetEventKind(kind);
      setKinds((prev) => prev.map((k) => (k.kind === kind ? updated : k)));
      setDrafts((d) => ({ ...d, [kind]: undefined }));
    } catch (e) {
      pushFlash("error", `Could not reset ${kind}: ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  const byGroup = useMemo(() => {
    const m = {};
    for (const k of kinds) (m[k.group] = m[k.group] || []).push(k);
    return m;
  }, [kinds]);

  return (
    <SpaceBetween size="l">
      {groups.map((g) => (
        <Container
          key={g.key}
          header={
            <Header variant="h2" description={g.description}>
              {g.name}
            </Header>
          }
        >
          <Table
            variant="embedded"
            loading={loading}
            items={byGroup[g.key] || []}
            trackBy="kind"
            wrapLines
            columnDefinitions={[
              {
                id: "name",
                header: "Event",
                minWidth: 200,
                cell: (k) => (
                  <SpaceBetween size="xxs">
                    <Box fontWeight="bold">{k.name}</Box>
                    <Box color="text-body-secondary">{k.description}</Box>
                  </SpaceBetween>
                ),
              },
              {
                id: "severity",
                header: "Severity",
                width: 170,
                cell: (k) =>
                  k.fixed ? (
                    <Box color="text-body-secondary">per rule</Box>
                  ) : (
                    <Select
                      selectedOption={CHOICE_OPTIONS.find((o) => o.value === k.severity)}
                      onChange={({ detail }) => save(k.kind, { severity: detail.selectedOption.value })}
                      options={CHOICE_OPTIONS}
                      disabled={busy === k.kind}
                    />
                  ),
              },
              {
                id: "params",
                header: "Thresholds",
                minWidth: 220,
                cell: (k) =>
                  k.params && Object.keys(k.params).length ? (
                    <SpaceBetween direction="horizontal" size="xs">
                      {Object.entries(k.params).map(([name, value]) => (
                        <FormField key={name} label={name.replace(/_/g, " ")}>
                          <Input
                            type="number"
                            value={String(drafts[k.kind]?.[name] ?? value)}
                            onChange={({ detail }) => setDrafts((d) => ({ ...d, [k.kind]: { ...(d[k.kind] || {}), [name]: detail.value } }))}
                          />
                        </FormField>
                      ))}
                      {drafts[k.kind] && (
                        <Button onClick={() => save(k.kind, { params: drafts[k.kind] })} loading={busy === k.kind}>
                          Apply
                        </Button>
                      )}
                    </SpaceBetween>
                  ) : (
                    "-"
                  ),
              },
              { id: "sources", header: "Detected by", width: 130, cell: (k) => k.sources.map((s) => SOURCE_LABELS[s] || s).join(", ") },
              { id: "resolves", header: "Resolves", minWidth: 200, cell: (k) => k.resolves },
              {
                id: "reset",
                header: "",
                width: 110,
                cell: (k) =>
                  k.overridden ? (
                    <Button variant="inline-link" onClick={() => reset(k.kind)} disabled={busy === k.kind}>
                      Use default ({k.default})
                    </Button>
                  ) : (
                    ""
                  ),
              },
            ]}
          />
        </Container>
      ))}
    </SpaceBetween>
  );
}

// --- ports -------------------------------------------------------------------

const PORT_OPTIONS = [{ label: "default (link down setting)", value: "default" }, ...CHOICE_OPTIONS];

function PortsTab({ devices, pushFlash }) {
  const [deviceId, setDeviceId] = useState(devices?.[0]?.id || null);
  const [rows, setRows] = useState([]);
  const [defaultSeverity, setDefaultSeverity] = useState("warning");
  const [loading, setLoading] = useState(false);
  const [busyPort, setBusyPort] = useState(null);
  const [filter, setFilter] = useState("");
  const deviceOptions = (devices || []).map((d) => ({ label: d.name, value: d.id }));

  async function refresh(id) {
    if (!id) return;
    setLoading(true);
    try {
      const r = await getPortSettings(id);
      setRows(r.ports);
      setDefaultSeverity(r.default_severity);
    } catch (e) {
      pushFlash("error", `Could not load ports: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh(deviceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  async function set(port, severity) {
    setBusyPort(port);
    try {
      const r = await setPortSeverity(deviceId, port, severity);
      setRows((prev) => prev.map((p) => (p.port === port ? { ...p, severity: r.severity } : p)));
    } catch (e) {
      pushFlash("error", `Could not update ${port}: ${e.message}`);
    } finally {
      setBusyPort(null);
    }
  }

  const shown = rows.filter((p) => !filter || p.port.toLowerCase().includes(filter.toLowerCase()) || (p.description || "").toLowerCase().includes(filter.toLowerCase()));
  const { pageItems, paginationProps } = useClientPagination(shown, 30);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={`A link down on any port is an event at the catalogue's link-down severity (currently ${defaultSeverity}). Set a port here to make it critical, info, or ignored - an ignored port never raises. Ports come from the SSH poll.`}
        >
          Ports
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Select placeholder="Device" selectedOption={deviceOptions.find((o) => o.value === deviceId) || null} onChange={({ detail }) => setDeviceId(detail.selectedOption.value)} options={deviceOptions} />
        <Table
          variant="embedded"
          loading={loading}
          items={pageItems}
          trackBy="port"
          filter={<TextFilter filteringText={filter} onChange={({ detail }) => setFilter(detail.filteringText)} filteringPlaceholder="Filter ports" />}
          pagination={<Pagination {...paginationProps} />}
          empty={<Box textAlign="center">No ports polled yet for this device.</Box>}
          columnDefinitions={[
            { id: "port", header: "Port", width: 160, cell: (p) => p.port },
            { id: "desc", header: "Description", minWidth: 180, cell: (p) => p.description || "-" },
            {
              id: "state",
              header: "Link",
              width: 120,
              cell: (p) => (
                <StatusIndicator type={p.port_state === "up" ? "success" : p.port_state === "admin_down" ? "stopped" : "error"}>{p.port_state || "?"}</StatusIndicator>
              ),
            },
            {
              id: "severity",
              header: "Link down is",
              width: 240,
              cell: (p) => (
                <Select
                  selectedOption={PORT_OPTIONS.find((o) => o.value === (p.severity || "default"))}
                  onChange={({ detail }) => set(p.port, detail.selectedOption.value)}
                  options={PORT_OPTIONS}
                  disabled={busyPort === p.port}
                />
              ),
            },
          ]}
        />
      </SpaceBetween>
    </Container>
  );
}

// --- the page ---------------------------------------------------------------------

export default function EventsPage({ devices, eventId, pushFlash, onNavigate }) {
  const [activeTab, setActiveTab] = useState("stream");
  return (
    <SpaceBetween size="l">
      <Header variant="h1" description="Event-driven monitoring: syslog first, the SSH poll as the fallback. Info, warning, critical - and resolved.">
        Events
      </Header>
      <Tabs
        activeTabId={activeTab}
        onChange={({ detail }) => setActiveTab(detail.activeTabId)}
        tabs={[
          { id: "stream", label: "Events", content: <StreamTab devices={devices} pushFlash={pushFlash} eventId={eventId} onNavigate={onNavigate} /> },
          { id: "catalog", label: "Catalogue", content: <CatalogTab pushFlash={pushFlash} /> },
          { id: "ports", label: "Ports", content: <PortsTab devices={devices} pushFlash={pushFlash} /> },
          { id: "syslog", label: "Syslog rules & fast path", content: <SyslogRulesTab pushFlash={pushFlash} /> },
          { id: "audit", label: "Audit log", content: <AuditLogTab pushFlash={pushFlash} /> },
        ]}
      />
    </SpaceBetween>
  );
}
