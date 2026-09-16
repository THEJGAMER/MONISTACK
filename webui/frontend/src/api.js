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
    if (res.status === 401 && path !== "/api/auth/me") {
      // No/expired session - bounce to Keycloak rather than surface a raw
      // 401 to a page that has no login form of its own to show. Excludes
      // /api/auth/me itself: App.jsx's own startup check calls that and
      // needs to see the 401 to decide whether to redirect at all (a
      // logged-out visit to a not-yet-configured deployment shouldn't
      // bounce to a login page before setup has even happened).
      window.location.href = "/api/auth/login";
      return new Promise(() => {}); // navigating away, never resolves
    }
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
// Command history / favourites (ROADMAP Phase 4). History is the caller's
// own by default; `allUsers` is admin-only server-side.
export const listCommandHistory = ({ deviceId, status, q, limit = 100, offset = 0, allUsers = false } = {}) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (deviceId) params.set("device_id", deviceId);
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  if (allUsers) params.set("all_users", "true");
  return api(`/api/command-history?${params.toString()}`);
};
export const getRecentCommands = (limit = 10) => api(`/api/command-history/recent?limit=${limit}`);
export const clearCommandHistory = () => api("/api/command-history", { method: "DELETE" });

export const listFavorites = () => api("/api/favorites");
export const addFavorite = (body) =>
  api("/api/favorites", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
export const deleteFavorite = (id) => api(`/api/favorites/${id}`, { method: "DELETE" });

export const getSettingsHealth = () => api("/api/settings/health");

// sFlow traffic views. One request returns all four panels over the same
// window - four separate calls could each land in a different window and
// disagree with each other.
// Every sFlow request carries the same window, relative or absolute, so
// the drill-downs cover exactly the span the page behind them shows.
const sflowWindow = ({ minutes = 60, agent, start, end, q, source } = {}) => {
  const p = new URLSearchParams({ minutes: String(minutes) });
  if (agent) p.set("agent", agent);
  if (q) p.set("q", q);
  // Which vantage point. Always sent explicitly - the two views must
  // never be confused for each other, so the drill-downs carry it too.
  if (source) p.set("source", source);
  // Absolute bounds win server-side; minutes is still sent as the
  // fallback a clamped or unparseable range resolves to.
  if (start && end) { p.set("start", start); p.set("end", end); }
  return p;
};
export const getSflowOverview = (opts = {}) => {
  const p = sflowWindow(opts);
  p.set("limit", String(opts.limit ?? 20));
  return api(`/api/sflow/overview?${p.toString()}`, undefined, 60_000);
};
export const getSflowHost = (host, opts = {}) =>
  api(`/api/sflow/host/${encodeURIComponent(host)}?${sflowWindow(opts).toString()}`, undefined, 60_000);
export const getSflowPort = (iface, opts = {}) =>
  api(`/api/sflow/port/${iface}?${sflowWindow(opts).toString()}`, undefined, 60_000);

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


// Runs several sequential SSH commands per device across the whole fleet
// (LLDP, ARP, MAC table, port-channel membership) - the routine 60s
// default is right-sized for a single device, not this.
export const getTopology = ({ refresh } = {}) => api(`/api/topology${refresh ? "?refresh=1" : ""}`, undefined, 180_000);

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
// a setup wizard on a fresh deploy instead of bouncing to Keycloak.
export const getSetupStatus = () => api("/api/setup/status");

// { username, role } for the current session, or throws on 401 (see api()'s
// special-case above - this is the one call that's allowed to see a 401
// rather than being auto-redirected, since App.jsx uses it to decide
// whether to redirect at all).
export const getCurrentUser = () => api("/api/auth/me");

// A real page navigation, not a fetch - logout has to redirect through
// Keycloak's end_session_endpoint (a different origin) to actually end its
// SSO session, not just clear our own cookie (see api_auth_logout's
// docstring - without this, the very next login silently re-authenticates
// with no prompt, which looked exactly like "logout doesn't work").
export const logout = () => {
  window.location.href = "/api/auth/logout";
};

export const submitSetup = (body) =>
  api("/api/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const getAuditLog = (limit = 200) => api(`/api/audit-log?limit=${limit}`);

// Alarm occurrences - one record per fired-to-resolved episode, each with
// its own id and its own shareable URL. Occurrences of the same alarm are
// linked (previous_occurrences) rather than merged.

// Paging control for one occurrence (see paging.py).
export const addComment = (id, body) =>
  api(`/api/alarms/${id}/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body }),
  });
export const deleteComment = (id, commentId) =>
  api(`/api/alarms/${id}/comments/${commentId}`, { method: "DELETE" });



// `port` travels in the body, not the URL - real port names like
// "Te 1/47" contain a "/" that a path segment can't safely carry (see
// app.py's InterfaceAlertUpdateRequest for the live-confirmed 404 this
// avoids).


export const getSettings = () => api("/api/settings");

export const updateSettings = (body) =>
  api("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });


// --- public API tokens (admin) ------------------------------------------
export const listApiTokens = () => api("/api/tokens");
export const createApiToken = (body) =>
  api("/api/tokens", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const revokeApiToken = (id) => api(`/api/tokens/${id}`, { method: "DELETE" });

// --- outbound webhooks (admin) ------------------------------------------
export const listWebhooks = () => api("/api/webhooks");
export const createWebhook = (body) =>
  api("/api/webhooks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const updateWebhook = (id, body) =>
  api(`/api/webhooks/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const deleteWebhook = (id) => api(`/api/webhooks/${id}`, { method: "DELETE" });
export const testWebhook = (id) => api(`/api/webhooks/${id}/test`, { method: "POST" });

// --- web push (per browser) ---------------------------------------------
export const getPushConfig = () => api("/api/push/config");
export const subscribePush = (body) =>
  api("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const unsubscribePush = (endpoint) =>
  api("/api/push/subscribe", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint }) });
export const listPushSubscriptions = () => api("/api/push/subscriptions");
export const updatePushPrefs = (body) =>
  api("/api/push/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const testPush = (endpoint) =>
  api("/api/push/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint }) });

// --- the syslog fast path + syslog rules ---------------------------------
const JSON_HEADERS = { "Content-Type": "application/json" };
// The self-test waits for the line to come back through Vector (up to ~12s).
export const listSyslogRules = () => api("/api/syslog-rules");
export const createSyslogRule = (body) =>
  api("/api/syslog-rules", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) });
export const updateSyslogRule = (id, body) =>
  api(`/api/syslog-rules/${id}`, { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(body) });
export const deleteSyslogRule = (id) => api(`/api/syslog-rules/${id}`, { method: "DELETE" });
export const matchSyslogRules = (body) =>
  api("/api/syslog-rules/match", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) });

// --- events -------------------------------------------------------------------
export const listEvents = (params = {}) => {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "" && v !== false) q.set(k, v === true ? "1" : v);
  });
  const qs = q.toString();
  return api(`/api/events${qs ? `?${qs}` : ""}`);
};
export const getEvent = (id) => api(`/api/events/${id}`);
export const resolveEvent = (id, note) =>
  api(`/api/events/${id}/resolve`, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ note }) });
export const getDeviceEvents = (deviceId, limit = 50) => api(`/api/devices/${encodeURIComponent(deviceId)}/events?limit=${limit}`);
export const getEventCatalog = () => api("/api/events/catalog");
export const updateEventKind = (kind, body) =>
  api(`/api/events/catalog/${encodeURIComponent(kind)}`, { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(body) });
export const resetEventKind = (kind) => api(`/api/events/catalog/${encodeURIComponent(kind)}`, { method: "DELETE" });
export const getPortSettings = (deviceId) => api(`/api/events/ports/${encodeURIComponent(deviceId)}`);
export const setPortSeverity = (deviceId, port, severity) =>
  api(`/api/events/ports/${encodeURIComponent(deviceId)}/${encodeURIComponent(port)}`, { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify({ severity }) });
export const getFastPath = () => api("/api/events/fast-path");
// The self-test waits for the line to come back through Vector (up to ~12s).
export const testFastPath = (severity) =>
  api("/api/events/fast-path/test", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ severity }) }, 30_000);
export const listWebhookEvents = () => api("/api/webhooks/events");

export const getInsights = (refresh = false) => api(`/api/insights${refresh ? "?refresh=1" : ""}`, undefined, 60_000);
