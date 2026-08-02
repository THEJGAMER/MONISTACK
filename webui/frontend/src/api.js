// Every backend call is bounded by finite server-side timeouts (SSH
// connect/run, Loki, DB reconnect all have their own - see ssh_client.py/
// loki_client.py/db.py), so a *hung* request from the browser's point of
// view means something's gone wrong outside those bounds (a dropped
// connection the OS never notices, a proxy sitting silent) - without a
// client-side timeout too, that shows the user an indefinite spinner
// instead of a clear, retry-able error. 60s covers the slowest routine
// case (a handful of sequential SSH commands against one device);
// `/api/topology` runs that same sequence per device across the whole
// fleet, so it gets a longer ceiling explicitly rather than one global
// number being wrong for everyone.
const DEFAULT_TIMEOUT_MS = 60_000;

async function api(path, opts, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(path, { ...opts, signal: controller.signal });
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export const getDevices = () => api("/api/devices");
// Returns { platform_id: [...command tree...] } - one tree per supported
// platform (currently "os9"/"junos") - index by the selected device's
// `platform` field, not a single flat tree.
export const getCommands = () => api("/api/commands");
export const getParamValues = (deviceId, paramName) => api(`/api/devices/${deviceId}/values/${paramName}`);

export const runCommand = (body) =>
  api("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const createDevice = (body) =>
  api("/api/devices", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const testDevice = (body) =>
  api("/api/devices/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const getDeviceForEdit = (id) => api(`/api/devices/${id}/edit`);

export const updateDevice = (id, body) =>
  api(`/api/devices/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const deleteDevice = (id) => api(`/api/devices/${id}`, { method: "DELETE" });

export const saveResult = (body) =>
  api("/api/results", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// Server-side paginated - returns { items, total, page, page_size }.
export const listResults = ({ deviceId, q, page = 1, pageSize = 10 } = {}) => {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (deviceId) params.set("device_id", deviceId);
  if (q) params.set("q", q);
  return api(`/api/results?${params.toString()}`);
};
export const getResult = (filename) => api(`/api/results/${filename}`);
export const deleteResult = (filename) => api(`/api/results/${filename}`, { method: "DELETE" });
export const exportResultUrl = (filename, format) => `/api/results/${encodeURIComponent(filename)}/export?format=${format}`;

// Runs the same allowlisted command across several devices in parallel -
// each gets its own result/error entry, one device failing doesn't abort
// the rest. Timeout matches getTopology's since this is also several
// sequential SSH round trips per device, just fanned out across devices
// instead of within one.
export const bulkRun = (body) =>
  api(
    "/api/bulk-run",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    180_000
  );

export const listSchedules = () => api("/api/schedules");
export const createSchedule = (body) =>
  api("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const updateSchedule = (id, body) =>
  api(`/api/schedules/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const deleteSchedule = (id) => api(`/api/schedules/${id}`, { method: "DELETE" });
export const runScheduleNow = (id) => api(`/api/schedules/${id}/run`, { method: "POST" });

export const getCompliance = () => api("/api/compliance", undefined, 180_000);
export const getComplianceConfig = () => api("/api/compliance/config");
export const updateComplianceConfig = (body) =>
  api("/api/compliance/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const getDeviceStatus = (deviceId, { interfaces = false } = {}) =>
  api(`/api/devices/${deviceId}/status${interfaces ? "?interfaces=true" : ""}`);

export const refreshDeviceStatus = (deviceId) => api(`/api/devices/${deviceId}/status/refresh`, { method: "POST" });

export const getSyslog = ({ deviceId, category, limit = 200 } = {}) => {
  const params = new URLSearchParams();
  if (deviceId) params.set("device_id", deviceId);
  if (category) params.set("category", category);
  if (limit) params.set("limit", limit);
  const qs = params.toString();
  return api(`/api/syslog${qs ? `?${qs}` : ""}`);
};

export const getAlarmHistory = (deviceId) => api(`/api/devices/${deviceId}/alarm-history`);

// Runs several sequential SSH commands per device across the whole fleet
// (LLDP, ARP, MAC table, port-channel membership) - the routine 60s
// default is right-sized for a single device, not this.
export const getTopology = () => api("/api/topology", undefined, 180_000);

export const saveTopologyBaseline = () => api("/api/topology/baseline", { method: "POST" });

export const acceptTopologyDrift = (added, removed) =>
  api("/api/topology/baseline/accept", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ added, removed }),
  });

export const clearTopologyBaseline = () => api("/api/topology/baseline", { method: "DELETE" });

export const getTrendSeries = (deviceId) => api(`/api/devices/${deviceId}/trends`);

export const getTrendData = (deviceId, metric, port, hours = 168) => {
  const params = new URLSearchParams({ hours: String(hours) });
  if (port) params.set("port", port);
  return api(`/api/devices/${deviceId}/trends/${metric}?${params.toString()}`);
};

// Unauthenticated - checked before login even applies, so the SPA can show
// a setup wizard on a fresh deploy instead of a basic-auth prompt.
export const getSetupStatus = () => api("/api/setup/status");

export const submitSetup = (body) =>
  api("/api/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const getAlerts = () => api("/api/alerts");
export const getLiveAlerts = () => api("/api/alerts/live");
export const getAlertsOverview = () => api("/api/alerts/overview");
export const getAuditLog = (limit = 200) => api(`/api/audit-log?limit=${limit}`);

// Alarm occurrences - one record per fired-to-resolved episode, each with
// its own id and its own shareable URL. Occurrences of the same alarm are
// linked (previous_occurrences) rather than merged.
export const getAlarms = (limit = 200, signature) =>
  api(`/api/alarms?limit=${limit}${signature ? `&signature=${encodeURIComponent(signature)}` : ""}`);
export const getAlarm = (id) => api(`/api/alarms/${id}`);
export const ackAlarm = (id, note) =>
  api(`/api/alarms/${id}/ack`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note: note || null }),
  });
export const unackAlarm = (id) => api(`/api/alarms/${id}/unack`, { method: "POST" });
export const resolveAlarm = (id) => api(`/api/alarms/${id}/resolve`, { method: "POST" });

// Paging control for one occurrence (see paging.py).
export const pageNow = (id) => api(`/api/alarms/${id}/page-now`, { method: "POST" });
export const delayPage = (id, seconds) =>
  api(`/api/alarms/${id}/delay-page`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds }),
  });
export const nargAlarm = (id, note) =>
  api(`/api/alarms/${id}/narg`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note: note || null }),
  });
export const enablePaging = (id) => api(`/api/alarms/${id}/enable-paging`, { method: "POST" });
export const addComment = (id, body) =>
  api(`/api/alarms/${id}/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body }),
  });
export const deleteComment = (id, commentId) =>
  api(`/api/alarms/${id}/comments/${commentId}`, { method: "DELETE" });

export const listSilences = () => api("/api/silences");
export const createSilence = (body) =>
  api("/api/silences", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const deleteSilence = (id) => api(`/api/silences/${encodeURIComponent(id)}`, { method: "DELETE" });

export const listAlertRules = () => api("/api/alert-rules");
export const updateAlertRule = (name, body) =>
  api(`/api/alert-rules/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const listInterfaceAlerts = (deviceId) => api(`/api/interface-alerts?device_id=${encodeURIComponent(deviceId)}`);
// `port` travels in the body, not the URL - real port names like
// "Te 1/47" contain a "/" that a path segment can't safely carry (see
// app.py's InterfaceAlertUpdateRequest for the live-confirmed 404 this
// avoids).
export const updateInterfaceAlert = (deviceId, port, body) =>
  api(`/api/interface-alerts/${encodeURIComponent(deviceId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ port, ...body }),
  });

export const getAlertHistory = (limit = 200) => api(`/api/alert-history?limit=${limit}`);

export const getSettings = () => api("/api/settings");

export const updateSettings = (body) =>
  api("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
