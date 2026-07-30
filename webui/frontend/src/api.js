async function api(path, opts) {
  const res = await fetch(path, opts);
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
