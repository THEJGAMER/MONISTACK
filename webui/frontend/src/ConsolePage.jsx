import React, { useEffect, useMemo, useState } from "react";
import Grid from "@cloudscape-design/components/grid";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Select from "@cloudscape-design/components/select";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Tabs from "@cloudscape-design/components/tabs";
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";

import {
  getParamValues,
  runCommand,
  getDeviceStatus,
  refreshDeviceStatus,
  listResults,
  getResult,
  deleteResult,
  getSyslog,
  getAlarmHistory,
} from "./api.js";
import { resultToMarkdown } from "./markdown.js";
import MiniMarkdown from "./MiniMarkdown.jsx";
import { useClientPagination } from "./useClientPagination.js";
import { CATEGORY_OPTIONS, severityType, formatTime } from "./syslogUtils.js";
import FrontPanelView from "./FrontPanelView.jsx";
import { defaultProfileId } from "./chassisProfiles.js";

function distinctParamNames(commandTree) {
  const names = new Set();
  commandTree.forEach((cat) => cat.items.forEach((item) => item.param && names.add(item.param)));
  return [...names];
}

function formatAge(seconds) {
  if (seconds == null) return "no data yet";
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function statusIndicatorType(state) {
  return { up: "success", alarm: "warning", down: "error" }[state] || "info";
}

function statusLabel(state) {
  return { up: "Up", alarm: "Alarm", down: "Down" }[state] || "Unknown";
}

function formatBytes(n) {
  if (n == null) return "-";
  if (n > 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n > 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n > 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
}

export default function ConsolePage({ devices, commandTree, pushFlash }) {
  const [filterText, setFilterText] = useState("");
  const [selected, setSelected] = useState(null);
  const [paramValues, setParamValues] = useState({}); // `${deviceId}:${param}` -> [values]
  const [paramSelections, setParamSelections] = useState({}); // `${categoryId}:${commandId}` -> value
  const [running, setRunning] = useState(null); // `${categoryId}:${commandId}` while in flight
  const [result, setResult] = useState(null); // { device, command, output, summary, deviceName, host, categoryId, commandId }
  const [runError, setRunError] = useState(null);
  const [viewMode, setViewMode] = useState("markdown"); // "raw" | "markdown"
  const [activeTab, setActiveTab] = useState("output");
  const [status, setStatus] = useState(null);
  const [recentResults, setRecentResults] = useState([]);
  const [viewing, setViewing] = useState(null); // { filename, content }
  const [syslogEvents, setSyslogEvents] = useState([]);
  const [syslogLoading, setSyslogLoading] = useState(false);
  const [syslogCategory, setSyslogCategory] = useState("");
  const [syslogFilterText, setSyslogFilterText] = useState("");
  const [statusWithInterfaces, setStatusWithInterfaces] = useState(null);
  const [profileId, setProfileId] = useState("generic-48");
  const [statusRefreshing, setStatusRefreshing] = useState(false);
  const [alarmHistory, setAlarmHistory] = useState([]);
  const [alarmHistoryLoading, setAlarmHistoryLoading] = useState(false);

  useEffect(() => {
    if (devices.length === 1 && !selected) setSelected(devices[0]);
  }, [devices, selected]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    (async () => {
      const names = distinctParamNames(commandTree);
      const updates = {};
      await Promise.all(
        names.map(async (name) => {
          const key = `${selected.id}:${name}`;
          try {
            const data = await getParamValues(selected.id, name);
            updates[key] = data.values;
          } catch {
            updates[key] = [];
          }
        })
      );
      if (!cancelled) setParamValues((prev) => ({ ...prev, ...updates }));
    })();
    return () => {
      cancelled = true;
    };
  }, [selected, commandTree]);

  async function refreshStatus(deviceId) {
    try {
      setStatus(await getDeviceStatus(deviceId));
    } catch {
      setStatus(null);
    }
  }

  async function refreshFrontPanelStatus(deviceId) {
    try {
      setStatusWithInterfaces(await getDeviceStatus(deviceId, { interfaces: true }));
    } catch {
      setStatusWithInterfaces(null);
    }
  }

  async function handleManualRefresh() {
    if (!selected) return;
    setStatusRefreshing(true);
    try {
      const fresh = await refreshDeviceStatus(selected.id);
      setStatus(fresh);
      setStatusWithInterfaces(fresh);
      pushFlash("success", "Status refreshed.");
    } catch (e) {
      pushFlash("error", `Could not refresh status: ${e.message}`);
    } finally {
      setStatusRefreshing(false);
    }
  }

  async function refreshRecentResults(deviceId) {
    try {
      setRecentResults(await listResults(deviceId));
    } catch {
      setRecentResults([]);
    }
  }

  async function refreshSyslog(deviceId, category) {
    setSyslogLoading(true);
    try {
      setSyslogEvents(await getSyslog({ deviceId, category: category || undefined, limit: 200 }));
    } catch (e) {
      pushFlash("error", `Could not load syslog: ${e.message}`);
    } finally {
      setSyslogLoading(false);
    }
  }

  async function refreshAlarmHistory(deviceId) {
    setAlarmHistoryLoading(true);
    try {
      setAlarmHistory(await getAlarmHistory(deviceId));
    } catch (e) {
      pushFlash("error", `Could not load alarm history: ${e.message}`);
    } finally {
      setAlarmHistoryLoading(false);
    }
  }

  useEffect(() => {
    if (selected) setProfileId(defaultProfileId(selected));
  }, [selected]);

  useEffect(() => {
    if (!selected) {
      setStatus(null);
      setStatusWithInterfaces(null);
      setRecentResults([]);
      setSyslogEvents([]);
      setAlarmHistory([]);
      return;
    }
    refreshStatus(selected.id);
    refreshFrontPanelStatus(selected.id);
    refreshRecentResults(selected.id);
    refreshSyslog(selected.id, syslogCategory);
    refreshAlarmHistory(selected.id);
    const t = setInterval(() => {
      refreshStatus(selected.id);
      refreshFrontPanelStatus(selected.id);
      refreshSyslog(selected.id, syslogCategory);
      refreshAlarmHistory(selected.id);
    }, 20000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, syslogCategory]);

  const filteredDevices = useMemo(() => {
    const q = filterText.toLowerCase();
    if (!q) return devices;
    return devices.filter((d) => d.name.toLowerCase().includes(q) || d.host.includes(q));
  }, [devices, filterText]);

  const { pageItems: recentResultsPage, paginationProps: recentResultsPagination } = useClientPagination(recentResults, 8);

  const filteredSyslog = useMemo(() => {
    const q = syslogFilterText.toLowerCase();
    if (!q) return syslogEvents;
    return syslogEvents.filter((e) => (e.detail || e.message || "").toLowerCase().includes(q));
  }, [syslogEvents, syslogFilterText]);
  const { pageItems: syslogPage, paginationProps: syslogPagination } = useClientPagination(filteredSyslog, 8);
  const { pageItems: alarmHistoryPage, paginationProps: alarmHistoryPagination } = useClientPagination(alarmHistory, 8);

  async function handleRun(cat, item) {
    if (!selected) return;
    const key = `${cat.id}:${item.id}`;
    const params = {};
    if (item.param) {
      const value = paramSelections[key];
      if (!value) {
        pushFlash("error", `Choose a value for ${item.param} first.`);
        return;
      }
      params[item.param] = value;
    }

    setRunning(key);
    setRunError(null);
    try {
      const res = await runCommand({ device_id: selected.id, category_id: cat.id, command_id: item.id, params });
      setResult({ ...res, deviceName: selected.name, host: selected.host, categoryId: cat.id, commandId: item.id });
      setActiveTab("output");
      // Every run is auto-saved server-side now - just reflect that here.
      refreshRecentResults(selected.id);
    } catch (e) {
      setResult(null);
      setRunError(`${item.label}: ${e.message}`);
    } finally {
      setRunning(null);
    }
  }

  function handleDownload() {
    if (!result) return;
    const markdown = resultToMarkdown({
      deviceName: result.deviceName,
      host: result.host,
      command: result.command,
      summary: result.summary,
      output: result.output,
    });
    const blob = new Blob([markdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = result.saved_as || `${result.device}-${result.categoryId}-${result.commandId}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function handleView(filename) {
    try {
      setViewing(await getResult(filename));
    } catch (e) {
      pushFlash("error", `Could not open ${filename}: ${e.message}`);
    }
  }

  async function handleDelete(filename) {
    try {
      await deleteResult(filename);
      setRecentResults((prev) => prev.filter((r) => r.filename !== filename));
      pushFlash("success", `Deleted ${filename}.`);
    } catch (e) {
      pushFlash("error", `Could not delete ${filename}: ${e.message}`);
    }
  }

  const summaryItems = selected && [
    {
      type: "pair",
      label: "Status",
      value: (
        <StatusIndicator type={statusIndicatorType(status?.state)}>
          {statusLabel(status?.state)}
        </StatusIndicator>
      ),
    },
    { type: "pair", label: "Data age", value: formatAge(status?.age_seconds) },
    { type: "pair", label: "Host", value: <Box variant="code">{selected.host}</Box> },
    { type: "pair", label: "Make / model", value: [selected.make, selected.model].filter(Boolean).join(" / ") || "-" },
    { type: "pair", label: "Platform", value: selected.platform },
    {
      type: "pair",
      label: "Interfaces up",
      value: status?.interfaces_total != null ? `${status.interfaces_up} / ${status.interfaces_total}` : "-",
    },
  ];

  return (
    <>
    <Grid gridDefinition={[{ colspan: { default: 12, xs: 4 } }, { colspan: { default: 12, xs: 8 } }]}>
      <SpaceBetween size="l">
        <Container header={<Header variant="h2">Devices</Header>}>
          <Table
            variant="embedded"
            columnDefinitions={[
              { id: "name", header: "Name", cell: (d) => d.name },
              { id: "host", header: "Host", cell: (d) => d.host },
            ]}
            items={filteredDevices}
            selectionType="single"
            selectedItems={selected ? [selected] : []}
            onSelectionChange={({ detail }) => setSelected(detail.selectedItems[0] ?? null)}
            filter={
              <TextFilter
                filteringText={filterText}
                onChange={({ detail }) => setFilterText(detail.filteringText)}
                filteringPlaceholder="Search devices..."
              />
            }
            empty={<Box textAlign="center">No devices match.</Box>}
          />
        </Container>

        {selected && (
          <Container header={<Header variant="h2">Device summary</Header>}>
            <SpaceBetween size="l">
              <KeyValuePairs columns={2} items={summaryItems} />
              {status?.alarms?.length > 0 && (
                <Alert type="warning">
                  {status.alarms.map((a) => (
                    <div key={a}>{a}</div>
                  ))}
                </Alert>
              )}
              <Box>
                <Box variant="awsui-key-label">Recent actions</Box>
                {recentResults.length === 0 ? (
                  <Box color="text-status-inactive" fontSize="body-s">
                    Nothing saved for this device yet.
                  </Box>
                ) : (
                  <SpaceBetween size="xs">
                    {recentResults.slice(0, 5).map((r) => (
                      <Button key={r.filename} variant="inline-link" onClick={() => handleView(r.filename)}>
                        {r.title || r.filename}
                      </Button>
                    ))}
                  </SpaceBetween>
                )}
              </Box>
            </SpaceBetween>
          </Container>
        )}

        <Container header={<Header variant="h2">Commands</Header>}>
          {!selected ? (
            <Box color="text-status-inactive">Select a device first.</Box>
          ) : (
            <SpaceBetween size="xs">
              {commandTree.map((cat) => (
                <ExpandableSection key={cat.id} headerText={cat.label}>
                  <SpaceBetween size="xs">
                    {cat.items.map((item) => {
                      const key = `${cat.id}:${item.id}`;
                      const options = item.param
                        ? (paramValues[`${selected.id}:${item.param}`] || []).map((v) => ({ label: v, value: v }))
                        : [];
                      const selectedOption = options.find((o) => o.value === paramSelections[key]) ?? null;
                      return (
                        <Box key={item.id}>
                          <SpaceBetween size="xs" direction="horizontal" alignItems="center">
                            <Box minWidth="180px">{item.label}</Box>
                            {item.param && (
                              <span style={{ minWidth: 140 }}>
                                <Select
                                  selectedOption={selectedOption}
                                  onChange={({ detail }) =>
                                    setParamSelections((prev) => ({ ...prev, [key]: detail.selectedOption.value }))
                                  }
                                  options={options}
                                  placeholder={item.param}
                                />
                              </span>
                            )}
                            <Button loading={running === key} onClick={() => handleRun(cat, item)}>
                              Run
                            </Button>
                          </SpaceBetween>
                        </Box>
                      );
                    })}
                  </SpaceBetween>
                </ExpandableSection>
              ))}
            </SpaceBetween>
          )}
        </Container>
      </SpaceBetween>

      <Container header={<Header variant="h2">{selected ? `${selected.name} (${selected.host})` : "No device selected"}</Header>}>
        {!selected ? (
          <Box color="text-status-inactive">Pick a device, then a command, on the left.</Box>
        ) : (
          <Tabs
            activeTabId={activeTab}
            onChange={({ detail }) => setActiveTab(detail.activeTabId)}
            tabs={[
              {
                id: "output",
                label: "Output",
                content: (
                  <SpaceBetween size="m">
                    {runError && <Alert type="error">{runError}</Alert>}
                    {!result && <Box color="text-status-inactive">Pick a command on the left, then Run.</Box>}
                    {result && (
                      <>
                        {/* The summary is the primary way to read a result - shown
                            plainly up top. The full CLI output is secondary, tucked
                            into the collapsed dropdown below, since most of the time
                            the one-line summary is the answer. */}
                        <SpaceBetween size="xs">
                          <Box variant="awsui-key-label">Summary</Box>
                          {result.summary ? (
                            <Box variant="p">{result.summary}</Box>
                          ) : (
                            <Box color="text-status-inactive" variant="p">
                              No summary available for this command - see the full output below.
                            </Box>
                          )}
                          <Box color="text-status-inactive" fontSize="body-s">
                            <Box variant="code" display="inline">
                              {result.command}
                            </Box>
                            {result.saved_as && <> · auto-saved as {result.saved_as}</>}
                          </Box>
                        </SpaceBetween>

                        <ExpandableSection
                          variant="container"
                          headerText="Command output"
                          headerDescription="Full CLI output as returned by the device"
                          headerActions={
                            <SpaceBetween size="xs" direction="horizontal" alignItems="center">
                              <SegmentedControl
                                selectedId={viewMode}
                                onChange={({ detail }) => setViewMode(detail.selectedId)}
                                options={[
                                  { id: "raw", text: "Raw" },
                                  { id: "markdown", text: "Markdown" },
                                ]}
                              />
                              <Button onClick={handleDownload} iconName="download">
                                Download .md
                              </Button>
                            </SpaceBetween>
                          }
                        >
                          {viewMode === "raw" ? (
                            <Box variant="code" display="block" className="terminal-output">
                              {result.output || "(no output)"}
                            </Box>
                          ) : (
                            <MiniMarkdown
                              source={resultToMarkdown({
                                deviceName: result.deviceName,
                                host: result.host,
                                command: result.command,
                                summary: result.summary,
                                output: result.output,
                              })}
                            />
                          )}
                        </ExpandableSection>
                      </>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: "recent",
                label: "Recent results",
                content: (
                  <Table
                    variant="embedded"
                    items={recentResultsPage}
                    pagination={<Pagination {...recentResultsPagination} />}
                    columnDefinitions={[
                      { id: "title", header: "Result", cell: (r) => r.title || r.filename },
                      { id: "saved_at", header: "Saved at", cell: (r) => r.saved_at },
                      {
                        id: "kind",
                        header: "Kind",
                        cell: (r) => (
                          <StatusIndicator type={r.auto_saved ? "info" : "success"}>
                            {r.auto_saved ? "Auto" : "Manual"}
                          </StatusIndicator>
                        ),
                      },
                      {
                        id: "actions",
                        header: "",
                        cell: (r) => (
                          <SpaceBetween size="xs" direction="horizontal">
                            <Button variant="inline-link" onClick={() => handleView(r.filename)}>
                              View
                            </Button>
                            <Button variant="inline-link" onClick={() => handleDelete(r.filename)}>
                              Delete
                            </Button>
                          </SpaceBetween>
                        ),
                      },
                    ]}
                    empty={<Box textAlign="center">No saved results for this device yet.</Box>}
                  />
                ),
              },
              {
                id: "syslog",
                label: "Syslog",
                content: (
                  <SpaceBetween size="m">
                    <SpaceBetween size="s" direction="horizontal" alignItems="center">
                      <Select
                        selectedOption={CATEGORY_OPTIONS.find((o) => o.value === syslogCategory) ?? CATEGORY_OPTIONS[0]}
                        onChange={({ detail }) => setSyslogCategory(detail.selectedOption.value)}
                        options={CATEGORY_OPTIONS}
                      />
                      <Button
                        iconName="refresh"
                        loading={syslogLoading}
                        onClick={() => refreshSyslog(selected.id, syslogCategory)}
                      >
                        Refresh
                      </Button>
                    </SpaceBetween>
                    <Table
                      variant="embedded"
                      loading={syslogLoading}
                      items={syslogPage}
                      filter={
                        <TextFilter
                          filteringText={syslogFilterText}
                          onChange={({ detail }) => setSyslogFilterText(detail.filteringText)}
                          filteringPlaceholder="Search message text..."
                        />
                      }
                      pagination={<Pagination {...syslogPagination} />}
                      columnDefinitions={[
                        { id: "time", header: "Time", cell: (e) => formatTime(e.device_timestamp) },
                        {
                          id: "severity",
                          header: "Severity",
                          cell: (e) => (
                            <StatusIndicator type={severityType(e.severity_num)}>{e.severity || "-"}</StatusIndicator>
                          ),
                        },
                        { id: "category", header: "Category", cell: (e) => e.event_category || "-" },
                        { id: "interface", header: "Interface", cell: (e) => e.interface || "-" },
                        { id: "message", header: "Message", cell: (e) => e.detail || e.message },
                      ]}
                      empty={<Box textAlign="center">No syslog events for this device in this window.</Box>}
                    />
                  </SpaceBetween>
                ),
              },
              {
                id: "alarmHistory",
                label: "Alarm History",
                content: (
                  <SpaceBetween size="m">
                    <Button
                      iconName="refresh"
                      loading={alarmHistoryLoading}
                      onClick={() => refreshAlarmHistory(selected.id)}
                    >
                      Refresh
                    </Button>
                    <Table
                      variant="embedded"
                      loading={alarmHistoryLoading}
                      items={alarmHistoryPage}
                      pagination={<Pagination {...alarmHistoryPagination} />}
                      columnDefinitions={[
                        { id: "time", header: "Time", cell: (e) => formatTime(e.device_timestamp) },
                        {
                          id: "severity",
                          header: "Severity",
                          cell: (e) => {
                            if (e.alarm_severity === "critical") return <StatusIndicator type="error">Critical</StatusIndicator>;
                            if (e.alarm_severity === "minor") return <StatusIndicator type="warning">Minor</StatusIndicator>;
                            return <StatusIndicator type="success">Recovery</StatusIndicator>;
                          },
                        },
                        { id: "component", header: "Component", cell: (e) => e.alarm_component || "-" },
                        { id: "message", header: "Message", cell: (e) => e.detail || e.message },
                        {
                          id: "current",
                          header: "",
                          cell: (e) => (e.is_current ? <StatusIndicator type="in-progress">Active</StatusIndicator> : null),
                        },
                      ]}
                      empty={
                        <Box textAlign="center">
                          No hardware alarm history for this device yet - fan/PSU faults and recoveries will
                          appear here as they're logged.
                        </Box>
                      }
                    />
                  </SpaceBetween>
                ),
              },
              {
                id: "frontpanel",
                label: "Front Panel",
                content: (
                  <FrontPanelView
                    device={selected}
                    status={statusWithInterfaces}
                    profileId={profileId}
                    onProfileChange={setProfileId}
                    onRefresh={handleManualRefresh}
                    refreshing={statusRefreshing}
                  />
                ),
              },
              {
                id: "switchStatus",
                label: "Switch Status",
                content: (
                  <SpaceBetween size="l">
                    <Button iconName="refresh" loading={statusRefreshing} onClick={handleManualRefresh}>
                      Refresh
                    </Button>

                    {status?.alarms?.length > 0 && (
                      <Alert type="warning">
                        {status.alarms.map((a) => (
                          <div key={a}>{a}</div>
                        ))}
                      </Alert>
                    )}

                    <KeyValuePairs
                      columns={3}
                      items={[
                        {
                          type: "pair",
                          label: "Status",
                          value: <StatusIndicator type={statusIndicatorType(status?.state)}>{statusLabel(status?.state)}</StatusIndicator>,
                        },
                        { type: "pair", label: "Data age", value: formatAge(status?.age_seconds) },
                        {
                          type: "pair",
                          label: "CPU (5s / 1m / 5m)",
                          value: status?.cpu?.overall
                            ? `${status.cpu.overall["5sec"]}% / ${status.cpu.overall["1min"]}% / ${status.cpu.overall["5min"]}%`
                            : "-",
                        },
                        { type: "pair", label: "Memory used", value: formatBytes(status?.memory?.used) },
                        { type: "pair", label: "Memory free", value: formatBytes(status?.memory?.free) },
                        { type: "pair", label: "Memory total", value: formatBytes(status?.memory?.total) },
                      ]}
                    />

                    <ExpandableSection
                      headerText="Alarms (show alarms)"
                      defaultExpanded={(status?.device_alarms?.minor?.length || 0) + (status?.device_alarms?.major?.length || 0) > 0}
                    >
                      <SpaceBetween size="s">
                        {!status?.device_alarms?.major?.length && !status?.device_alarms?.minor?.length && (
                          <Box color="text-status-inactive">No minor or major alarms.</Box>
                        )}
                        {status?.device_alarms?.major?.map((a, i) => (
                          <Alert key={`major-${i}`} type="error">
                            {a.text}
                            {a.duration && ` - ${a.duration}`}
                          </Alert>
                        ))}
                        {status?.device_alarms?.minor?.map((a, i) => (
                          <Alert key={`minor-${i}`} type="warning">
                            {a.text}
                            {a.duration && ` - ${a.duration}`}
                          </Alert>
                        ))}
                      </SpaceBetween>
                    </ExpandableSection>

                    <ExpandableSection headerText="Fans" defaultExpanded>
                      <Table
                        variant="embedded"
                        items={status?.env?.fans || []}
                        columnDefinitions={[
                          { id: "unit", header: "Unit", cell: (f) => f.unit },
                          { id: "bay", header: "Bay", cell: (f) => f.bay },
                          {
                            id: "fan1",
                            header: "Fan 1",
                            cell: (f) => (
                              <StatusIndicator type={f.fan1_status === "up" ? "success" : "error"}>
                                {f.removed ? "Removed" : `${f.fan1_rpm} rpm`}
                              </StatusIndicator>
                            ),
                          },
                          {
                            id: "fan2",
                            header: "Fan 2",
                            cell: (f) => (
                              <StatusIndicator type={f.fan2_status === "up" ? "success" : "error"}>
                                {f.removed ? "Removed" : `${f.fan2_rpm} rpm`}
                              </StatusIndicator>
                            ),
                          },
                        ]}
                        empty={<Box textAlign="center">No fan data yet.</Box>}
                      />
                    </ExpandableSection>

                    <ExpandableSection headerText="Power supplies" defaultExpanded>
                      <Table
                        variant="embedded"
                        items={status?.env?.psus || []}
                        columnDefinitions={[
                          { id: "unit", header: "Unit", cell: (p) => p.unit },
                          { id: "bay", header: "Bay", cell: (p) => p.bay },
                          {
                            id: "status",
                            header: "Status",
                            cell: (p) => (
                              <StatusIndicator type={p.status === "up" ? "success" : "error"}>
                                {p.removed ? "Removed" : p.status}
                              </StatusIndicator>
                            ),
                          },
                          { id: "type", header: "Type", cell: (p) => p.type },
                          {
                            id: "power",
                            header: "Power draw",
                            cell: (p) => (p.removed ? "-" : `${p.power_watts} W (avg ${p.avg_power_watts} W)`),
                          },
                        ]}
                        empty={<Box textAlign="center">No PSU data yet.</Box>}
                      />
                    </ExpandableSection>

                    <ExpandableSection headerText="Thermal sensors">
                      <KeyValuePairs
                        columns={5}
                        items={Object.entries(status?.env?.sensors || {}).map(([name, val]) => ({
                          label: name.toUpperCase(),
                          value: `${val} C`,
                        }))}
                      />
                    </ExpandableSection>
                  </SpaceBetween>
                ),
              },
            ]}
          />
        )}
      </Container>
    </Grid>

    <Modal visible={!!viewing} onDismiss={() => setViewing(null)} header={viewing?.filename} size="large">
      {viewing && <MiniMarkdown source={viewing.content} />}
    </Modal>
    </>
  );
}
