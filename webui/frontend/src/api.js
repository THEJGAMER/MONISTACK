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

export const listResults = (deviceId) => api(deviceId ? `/api/results?device_id=${encodeURIComponent(deviceId)}` : "/api/results");
export const getResult = (filename) => api(`/api/results/${filename}`);
export const deleteResult = (filename) => api(`/api/results/${filename}`, { method: "DELETE" });

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

export const getSettings = () => api("/api/settings");

export const updateSettings = (body) =>
  api("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
