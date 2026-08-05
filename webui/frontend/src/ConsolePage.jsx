import React, { useEffect, useMemo, useRef, useState } from "react";
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
import Modal from "@cloudscape-design/components/modal";
import Pagination from "@cloudscape-design/components/pagination";
import Board from "@cloudscape-design/board-components/board";
import BoardItem from "@cloudscape-design/board-components/board-item";

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
  listCommandHistory,
  clearCommandHistory,
  listFavorites,
  addFavorite,
  deleteFavorite,
} from "./api.js";
import { resultToMarkdown } from "./markdown.js";
import MiniMarkdown from "./MiniMarkdown.jsx";
import { useClientPagination } from "./useClientPagination.js";
import { CATEGORY_OPTIONS, severityType, formatTime } from "./syslogUtils.js";
import FrontPanelView from "./FrontPanelView.jsx";
import { defaultProfileId } from "./chassisProfiles.js";
import { DEFAULT_BOARD_ITEMS, boardI18nStrings, boardItemI18nStrings } from "./boardConfig.js";

const BOARD_LAYOUT_KEY = "switchboard-console-board-layout";

function loadBoardLayout() {
  try {
    const raw = localStorage.getItem(BOARD_LAYOUT_KEY);
    if (!raw) return DEFAULT_BOARD_ITEMS;
    const saved = JSON.parse(raw);
    const defById = Object.fromEntries(DEFAULT_BOARD_ITEMS.map((d) => [d.id, d]));
    const savedIds = new Set(saved.map((i) => i.id));
    // Board has no explicit row-position field - vertical stacking order
    // comes from array order plus columnOffset/rowSpan, so a drag-reorder
    // has to be reflected in array order here, not just merged onto each
    // item by id (that preserved sizes but silently dropped every reorder
    // on reload). Known ids keep the saved order and get their saved
    // size/position merged over the current default (so `data`/
    // `definition` stay in sync with boardConfig.js); ids not present in
    // the saved layout (e.g. a panel added after it was saved) are
    // appended in their default order rather than breaking the board.
    const ordered = saved.filter((i) => defById[i.id]).map((i) => ({ ...defById[i.id], ...i }));
    const missing = DEFAULT_BOARD_ITEMS.filter((d) => !savedIds.has(d.id));
    return [...ordered, ...missing];
  } catch {
    return DEFAULT_BOARD_ITEMS;
  }
}

function saveBoardLayout(items) {
  const slim = items.map(({ id, columnSpan, rowSpan, columnOffset }) => ({ id, columnSpan, rowSpan, columnOffset }));
  localStorage.setItem(BOARD_LAYOUT_KEY, JSON.stringify(slim));
}

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

export default function ConsolePage({ devices, commandTree, pushFlash, preselectDeviceId, onPreselectConsumed }) {
  const [filterText, setFilterText] = useState("");
  const [selected, setSelected] = useState(null);
  const [paramValues, setParamValues] = useState({}); // `${deviceId}:${param}` -> [values]
  const [paramSelections, setParamSelections] = useState({}); // `${categoryId}:${commandId}` -> value
  const [running, setRunning] = useState(null); // `${categoryId}:${commandId}` while in flight
  const [result, setResult] = useState(null); // { device, command, output, summary, deviceName, host, categoryId, commandId }
  const [runError, setRunError] = useState(null);
  const [viewMode, setViewMode] = useState("markdown"); // "raw" | "markdown"
  const [status, setStatus] = useState(null);
  const [recentResults, setRecentResults] = useState([]);
  const [viewing, setViewing] = useState(null); // { filename, content }
  const [syslogEvents, setSyslogEvents] = useState([]);
  const [syslogLoading, setSyslogLoading] = useState(false);
  const [syslogCategory, setSyslogCategory] = useState("");
  const [syslogFilterText, setSyslogFilterText] = useState("");
  const [syslogLimit, setSyslogLimit] = useState(200);
  const [syslogHasMore, setSyslogHasMore] = useState(false);
  const syslogLimitRef = useRef(200);
  useEffect(() => {
    syslogLimitRef.current = syslogLimit;
  }, [syslogLimit]);
  const [statusWithInterfaces, setStatusWithInterfaces] = useState(null);
  const [profileId, setProfileId] = useState("generic-48");
  const [statusRefreshing, setStatusRefreshing] = useState(false);
  const [alarmHistory, setAlarmHistory] = useState([]);
  const [alarmHistoryLoading, setAlarmHistoryLoading] = useState(false);
  const [boardItems, setBoardItems] = useState(loadBoardLayout);
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [favorites, setFavorites] = useState([]);

  function handleBoardItemsChange({ detail }) {
    setBoardItems(detail.items);
    saveBoardLayout(detail.items);
  }

  function handleResetLayout() {
    setBoardItems(DEFAULT_BOARD_ITEMS);
    localStorage.removeItem(BOARD_LAYOUT_KEY);
  }

  useEffect(() => {
    if (devices.length === 1 && !selected) setSelected(devices[0]);
  }, [devices, selected]);

  // Jumped here from Topology's "open in Console" - takes priority over
  // the single-device auto-select above since it's an explicit user action.
  useEffect(() => {
    if (!preselectDeviceId) return;
    const match = devices.find((d) => d.id === preselectDeviceId);
    if (match) setSelected(match);
    onPreselectConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preselectDeviceId, devices]);

  const platformCommandTree = (selected && commandTree[selected.platform]) || [];

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    (async () => {
      const names = distinctParamNames(platformCommandTree);
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, selected?.platform, commandTree]);

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
      const res = await listResults({ deviceId, pageSize: 100 });
      setRecentResults(res.items);
    } catch {
      setRecentResults([]);
    }
  }

  // History is deliberately not scoped to the selected device: "what did I
  // run recently" is usually asked *across* devices (you remember the
  // command, not which switch you were on). The table has a device column
  // and its own filter for narrowing.
  async function refreshHistory() {
    setHistoryLoading(true);
    try {
      const res = await listCommandHistory({ limit: 200 });
      setHistory(res.items);
    } catch {
      setHistory([]);
    } finally {
      setHistoryLoading(false);
    }
  }

  useEffect(() => {
    refreshHistory();
    listFavorites()
      .then(setFavorites)
      .catch(() => setFavorites([]));
  }, []);

  async function refreshSyslog(deviceId, category, limit) {
    setSyslogLoading(true);
    try {
      const events = await getSyslog({ deviceId, category: category || undefined, limit });
      setSyslogEvents(events);
      // Loki's query is capped at `limit`, not offset-paginated - a full
      // page back means there's probably more further back in the window,
      // so surface a "Load more" affordance instead of silently capping
      // at 200 rows forever.
      setSyslogHasMore(events.length >= limit);
    } catch (e) {
      pushFlash("error", `Could not load syslog: ${e.message}`);
    } finally {
      setSyslogLoading(false);
    }
  }

  function loadMoreSyslog() {
    const nextLimit = syslogLimit + 200;
    setSyslogLimit(nextLimit);
    refreshSyslog(selected.id, syslogCategory, nextLimit);
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
    setSyslogLimit(200);
    refreshStatus(selected.id);
    refreshFrontPanelStatus(selected.id);
    refreshRecentResults(selected.id);
    refreshSyslog(selected.id, syslogCategory, 200);
    refreshAlarmHistory(selected.id);
    const t = setInterval(() => {
      refreshStatus(selected.id);
      refreshFrontPanelStatus(selected.id);
      refreshSyslog(selected.id, syslogCategory, syslogLimitRef.current);
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

  // The shared run path. Split out of handleRun so the Favourites and
  // History lists can re-run something by id without duplicating the
  // request/flash/refresh handling - they have a category/command/params
  // triple but no command-tree `item` object to hand over.
  async function runResolved({ categoryId, commandId, params, label, runKey }) {
    if (!selected) return;
    setRunning(runKey);
    setRunError(null);
    try {
      const res = await runCommand({
        device_id: selected.id, category_id: categoryId, command_id: commandId, params,
      });
      setResult({ ...res, deviceName: selected.name, host: selected.host, categoryId, commandId });
      // Every run is auto-saved server-side now - just reflect that here.
      refreshRecentResults(selected.id);
      refreshHistory();
    } catch (e) {
      setResult(null);
      setRunError(`${label}: ${e.message}`);
      // A failed run is still recorded server-side (see command_history.py),
      // so the History tab has to be refreshed on this path too - otherwise
      // the one kind of entry most worth finding again is the one the UI
      // silently omits until the next successful run.
      refreshHistory();
    } finally {
      setRunning(null);
    }
  }

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
    await runResolved({
      categoryId: cat.id, commandId: item.id, params, label: item.label, runKey: key,
    });
  }

  /** Re-run a favourite or a history entry against the selected device. */
  async function handleRerun(entry, runKey) {
    const cat = platformCommandTree.find((c) => c.id === entry.category_id);
    const item = cat?.items.find((i) => i.id === entry.command_id);
    if (!item) {
      pushFlash(
        "error",
        `"${entry.command_id}" isn't available on ${selected?.platform ?? "this platform"}.`,
      );
      return;
    }
    await runResolved({
      categoryId: entry.category_id,
      commandId: entry.command_id,
      params: entry.params || {},
      label: item.label,
      runKey,
    });
  }

  function favoriteFor(categoryId, commandId, params) {
    const wanted = JSON.stringify(params || {});
    return favorites.find(
      (f) =>
        f.category_id === categoryId &&
        f.command_id === commandId &&
        JSON.stringify(f.params || {}) === wanted &&
        (f.device_id || null) === (selected?.id || null),
    );
  }

  async function toggleFavorite(cat, item) {
    if (!selected) return;
    const key = `${cat.id}:${item.id}`;
    const params = {};
    if (item.param) {
      const value = paramSelections[key];
      if (!value) {
        pushFlash("error", `Choose a value for ${item.param} first, so the favourite knows what to run.`);
        return;
      }
      params[item.param] = value;
    }
    const existing = favoriteFor(cat.id, item.id, params);
    try {
      if (existing) {
        await deleteFavorite(existing.id);
      } else {
        await addFavorite({
          device_id: selected.id, category_id: cat.id, command_id: item.id, params, label: item.label,
        });
      }
      setFavorites(await listFavorites());
    } catch (e) {
      pushFlash("error", `Could not update favourites: ${e.message}`);
    }
  }

  async function removeFavorite(fav) {
    try {
      await deleteFavorite(fav.id);
      setFavorites(await listFavorites());
    } catch (e) {
      pushFlash("error", `Could not remove favourite: ${e.message}`);
    }
  }

  async function handleClearHistory() {
    try {
      await clearCommandHistory();
      setHistory([]);
      pushFlash("success", "Command history cleared.");
    } catch (e) {
      pushFlash("error", `Could not clear history: ${e.message}`);
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
    <SpaceBetween size="m">
      <Header variant="h1" actions={<Button onClick={handleResetLayout}>Reset layout</Button>}>
        Console
      </Header>
      {(() => {
        const panels = [
          {
            id: "devices",
            label: "Devices",
            content: (
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
            ),
          },
          {
            id: "deviceSummary",
            label: "Device summary",
            content: !selected ? (
              <Box color="text-status-inactive">Select a device first.</Box>
            ) : (
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
            ),
          },
          {
            id: "commands",
            label: "Commands",
            content: !selected ? (
              <Box color="text-status-inactive">Select a device first.</Box>
            ) : (
              <SpaceBetween size="xs">
              {platformCommandTree.length === 0 && (
                <Box color="text-status-inactive">
                  No command tree wired up yet for platform "{selected.platform}".
                </Box>
              )}
              {platformCommandTree.map((cat) => (
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
                            <Button
                              variant="icon"
                              iconName={
                                favoriteFor(cat.id, item.id, item.param ? { [item.param]: paramSelections[key] } : {})
                                  ? "star-filled"
                                  : "star"
                              }
                              ariaLabel={
                                favoriteFor(cat.id, item.id, item.param ? { [item.param]: paramSelections[key] } : {})
                                  ? `Remove ${item.label} from favourites`
                                  : `Add ${item.label} to favourites`
                              }
                              onClick={() => toggleFavorite(cat, item)}
                            />
                          </SpaceBetween>
                        </Box>
                      );
                    })}
                  </SpaceBetween>
                </ExpandableSection>
              ))}
            </SpaceBetween>
            ),
          },
          {
            id: "favorites",
            label: `Favourites${favorites.length ? ` (${favorites.length})` : ""}`,
            content: !selected ? (
              <Box color="text-status-inactive">Select a device first.</Box>
            ) : favorites.length === 0 ? (
              <Box color="text-status-inactive">
                No favourites yet - star a command on the Commands tab to pin it here.
              </Box>
            ) : (
              <Table
                variant="embedded"
                items={favorites}
                trackBy="id"
                columnDefinitions={[
                  { id: "label", header: "Command", cell: (f) => f.label || f.command_id },
                  {
                    id: "params",
                    header: "Params",
                    cell: (f) =>
                      Object.keys(f.params || {}).length
                        ? Object.entries(f.params).map(([k, v]) => `${k}=${v}`).join(", ")
                        : "-",
                  },
                  { id: "device", header: "Device", cell: (f) => f.device_id || "any" },
                  {
                    id: "actions",
                    header: "Actions",
                    cell: (f) => (
                      <SpaceBetween size="xs" direction="horizontal">
                        <Button
                          loading={running === `fav:${f.id}`}
                          onClick={() => handleRerun(f, `fav:${f.id}`)}
                        >
                          Run
                        </Button>
                        <Button variant="icon" iconName="close" ariaLabel="Remove favourite"
                                onClick={() => removeFavorite(f)} />
                      </SpaceBetween>
                    ),
                  },
                ]}
              />
            ),
          },
          {
            id: "history",
            label: "History",
            content: (
              <SpaceBetween size="s">
                <SpaceBetween size="xs" direction="horizontal" alignItems="center">
                  <Button iconName="refresh" onClick={refreshHistory} loading={historyLoading}>
                    Refresh
                  </Button>
                  <Button onClick={handleClearHistory} disabled={history.length === 0}>
                    Clear my history
                  </Button>
                  <Box color="text-status-inactive" fontSize="body-s">
                    Your own runs, newest first. Clearing this does not affect the admin audit log.
                  </Box>
                </SpaceBetween>
                <Table
                  variant="embedded"
                  loading={historyLoading}
                  loadingText="Loading history"
                  items={history}
                  trackBy="id"
                  empty={<Box color="text-status-inactive">Nothing run yet.</Box>}
                  columnDefinitions={[
                    { id: "ts", header: "When", cell: (h) => formatTime(h.ts) },
                    { id: "device", header: "Device", cell: (h) => h.device_name || h.device_id },
                    { id: "command", header: "Command", cell: (h) => <Box variant="code">{h.command}</Box> },
                    {
                      id: "status",
                      header: "Status",
                      cell: (h) =>
                        h.status === "ok" ? (
                          <StatusIndicator type="success">ok</StatusIndicator>
                        ) : (
                          <StatusIndicator type="error">{h.error || "error"}</StatusIndicator>
                        ),
                    },
                    {
                      id: "duration",
                      header: "Took",
                      cell: (h) => (h.duration_ms == null ? "-" : `${(h.duration_ms / 1000).toFixed(2)}s`),
                    },
                    {
                      id: "actions",
                      header: "Actions",
                      cell: (h) => (
                        <Button
                          disabled={!selected || h.device_id !== selected.id}
                          loading={running === `hist:${h.id}`}
                          onClick={() => handleRerun(h, `hist:${h.id}`)}
                        >
                          Run again
                        </Button>
                      ),
                    },
                  ]}
                />
              </SpaceBetween>
            ),
          },
          {
            id: "output",
                label: "Output",
                content: !selected ? (
                  <Box color="text-status-inactive">Select a device first.</Box>
                ) : (
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
                content: !selected ? (
                  <Box color="text-status-inactive">Select a device first.</Box>
                ) : (
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
                content: !selected ? (
                  <Box color="text-status-inactive">Select a device first.</Box>
                ) : (
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
                        onClick={() => refreshSyslog(selected.id, syslogCategory, syslogLimit)}
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
                    {syslogHasMore && (
                      <Box textAlign="center">
                        <Button loading={syslogLoading} onClick={loadMoreSyslog}>
                          Load 200 more
                        </Button>
                      </Box>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: "alarmHistory",
                label: "Alarm History",
                content: !selected ? (
                  <Box color="text-status-inactive">Select a device first.</Box>
                ) : (
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
                content: !selected ? (
                  <Box color="text-status-inactive">Select a device first.</Box>
                ) : selected.platform === "opnsense" ? (
                  <Box color="text-status-inactive">
                    No front-panel view for this platform - OPNsense is a firewall appliance, not a switch
                    chassis with a fixed port layout to illustrate.
                  </Box>
                ) : (
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
                content: !selected ? (
                  <Box color="text-status-inactive">Select a device first.</Box>
                ) : (
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
                                {f.removed ? "Removed" : f.fan1_rpm != null ? `${f.fan1_rpm} rpm` : "OK"}
                              </StatusIndicator>
                            ),
                          },
                          {
                            id: "fan2",
                            header: "Fan 2",
                            cell: (f) =>
                              // Junos only reports one fan per bay - fan2_status is null there,
                              // not a fault, so don't render a fake error state for it.
                              f.fan2_status == null ? (
                                "-"
                              ) : (
                                <StatusIndicator type={f.fan2_status === "up" ? "success" : "error"}>
                                  {f.removed ? "Removed" : f.fan2_rpm != null ? `${f.fan2_rpm} rpm` : "OK"}
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
                            cell: (p) =>
                              // Junos doesn't report PSU wattage at all (power_watts is null there)
                              p.removed || p.power_watts == null ? "-" : `${p.power_watts} W (avg ${p.avg_power_watts} W)`,
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
                          // Dell's sensors are bare numbers (append " C");
                          // Junos's are already a full descriptive string
                          // ("36 degrees C / 96 degrees F") - don't double up.
                          value: typeof val === "string" && val.includes("degrees") ? val : `${val} C`,
                        }))}
                      />
                    </ExpandableSection>
                  </SpaceBetween>
                ),
              },
            ];
            return (
              <Board
                items={boardItems}
                onItemsChange={handleBoardItemsChange}
                i18nStrings={boardI18nStrings}
                empty={<Box textAlign="center">No panels.</Box>}
                renderItem={(item) => {
                  const panel = panels.find((p) => p.id === item.id);
                  return (
                    <BoardItem
                      header={<Header variant="h2">{panel.label}</Header>}
                      i18nStrings={boardItemI18nStrings(panel.label)}
                    >
                      {panel.content}
                    </BoardItem>
                  );
                }}
              />
            );
          })()}
    </SpaceBetween>

    <Modal visible={!!viewing} onDismiss={() => setViewing(null)} header={viewing?.filename} size="large">
      {viewing && <MiniMarkdown source={viewing.content} />}
    </Modal>
    </>
  );
}
