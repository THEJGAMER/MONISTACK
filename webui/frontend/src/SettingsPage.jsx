import Select from "@cloudscape-design/components/select";
import React, { useCallback, useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";
import Table from "@cloudscape-design/components/table";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";

import Multiselect from "@cloudscape-design/components/multiselect";
import Toggle from "@cloudscape-design/components/toggle";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import {
  getSettings, updateSettings, getSettingsHealth,
  listApiTokens, createApiToken, revokeApiToken,
  listEvents, listWebhooks, createWebhook, updateWebhook, deleteWebhook, testWebhook,
  listPushSubscriptions, unsubscribePush,
} from "./api.js";
import { useHasRole } from "./AuthContext.jsx";

const SERVICE_FIELDS = [
  {
    key: "loki_url",
    label: "Loki URL",
    description: "Feeds the Syslog tab and Alarm History.",
    placeholder: "http://loki-host:3100",
  },
  {
    key: "alertmanager_url",
    label: "Alertmanager URL",
    description:
      "Where alarms are posted and paging holds (silences) are created. Wrong here means alerts fire into nothing.",
    placeholder: "http://alertmanager-host:9093",
  },
  {
    key: "prometheus_url",
    label: "Prometheus URL",
    description: "Read for pending-rule state on the Alerts page.",
    placeholder: "http://prometheus-host:9090",
  },
  {
    key: "prometheus_reload_url",
    label: "Prometheus reload URL",
    description:
      "Called after the Rules tab writes alerts.yml. Leave blank to derive it from the Prometheus URL above.",
    placeholder: "(derived from Prometheus URL)",
  },
  {
    key: "sflow_collector",
    label: "sFlow collector",
    description:
      "Where sfacctd runs, as host:port. The webui never connects to it - flows arrive via Postgres - so this is not a connection string. It is the address the health check names when sFlow goes quiet, so \"no flows\" comes with somewhere to look.",
    placeholder: "192.168.0.155:6343",
  },
  {
    key: "syslog_receiver",
    label: "Syslog receiver",
    description:
      "Where the devices send syslog (Vector), as host:port. Only the fast-path self-test on the Alerts page uses it: it sends one line there and times it back through Vector into Switchboard.",
    placeholder: "192.168.0.144:514",
  },
  {
    key: "exporter_url",
    label: "Exporter URL",
    description:
      "Only used for the health check below - Prometheus scrapes the exporter directly, not the webui.",
    placeholder: "http://exporter-host:9101",
  },
];


// --- API tokens -------------------------------------------------------
// The token is shown exactly once, in the Alert below the form. It is not
// stored anywhere in clear text, so closing the alert really is the last
// time anyone sees it - the copy button is there for that reason.
const TOKEN_ROLES = [
  { label: "viewer - read only", value: "viewer" },
  { label: "operator - run commands, work alarms", value: "operator" },
  { label: "admin - change configuration", value: "admin" },
];

function ApiTokensSection({ pushFlash }) {
  const [tokens, setTokens] = useState([]);
  const [name, setName] = useState("");
  const [role, setRole] = useState(TOKEN_ROLES[0]);
  const [expiresDays, setExpiresDays] = useState("");
  const [minted, setMinted] = useState(null); // {name, token}
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setTokens(await listApiTokens());
    } catch (e) {
      pushFlash("error", `Could not load API tokens: ${e.message}`);
    }
  }, [pushFlash]);
  useEffect(() => {
    load();
  }, [load]);

  async function create() {
    setBusy(true);
    try {
      const row = await createApiToken({ name: name.trim(), role: role.value, expires_in_days: expiresDays ? Number(expiresDays) : null });
      setMinted({ name: row.name, token: row.token });
      setName("");
      await load();
    } catch (e) {
      pushFlash("error", `Could not create token: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }
  async function revoke(id) {
    try {
      await revokeApiToken(id);
      await load();
    } catch (e) {
      pushFlash("error", `Could not revoke: ${e.message}`);
    }
  }

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={
            <>
              Bearer credentials for scripts and integrations. Send <Box variant="code" display="inline">Authorization: Bearer sb_…</Box> to any
              endpoint; the API is documented at <a href="/api/docs" target="_blank" rel="noreferrer">/api/docs</a>. A Keycloak access token
              works the same way with no token needed here.
            </>
          }
        >
          API tokens
        </Header>
      }
    >
      <SpaceBetween size="l">
        {minted ? (
          <Alert
            type="success"
            dismissible
            onDismiss={() => setMinted(null)}
            header={`Token "${minted.name}" created - copy it now, it will not be shown again`}
            action={<CopyToClipboard copyButtonText="Copy token" copyErrorText="Could not copy" copySuccessText="Copied" textToCopy={minted.token} />}
          >
            <Box variant="code">{minted.token}</Box>
          </Alert>
        ) : null}
        <SpaceBetween size="s" direction="horizontal" alignItems="end">
          <FormField label="Name" description="What uses it, e.g. 'grafana' or 'backup script'.">
            <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="ci-pipeline" />
          </FormField>
          <FormField label="Role" description="Never more than your own.">
            <Select selectedOption={role} onChange={({ detail }) => setRole(detail.selectedOption)} options={TOKEN_ROLES} />
          </FormField>
          <FormField label="Expires in (days)" description="Blank = never.">
            <Input value={expiresDays} onChange={({ detail }) => setExpiresDays(detail.value.replace(/[^0-9]/g, ""))} placeholder="90" inputMode="numeric" />
          </FormField>
          <Button variant="primary" onClick={create} loading={busy} disabled={!name.trim()}>
            Create token
          </Button>
        </SpaceBetween>
        <Table
          variant="embedded"
          items={tokens}
          empty={<Box color="text-status-inactive">No API tokens yet.</Box>}
          columnDefinitions={[
            { id: "name", header: "Name", cell: (t) => t.name },
            { id: "prefix", header: "Starts with", cell: (t) => <Box variant="code">{t.prefix}…</Box> },
            { id: "role", header: "Role", cell: (t) => t.role },
            { id: "by", header: "Created by", cell: (t) => t.created_by },
            { id: "used", header: "Last used", cell: (t) => (t.last_used_at ? new Date(t.last_used_at).toLocaleString() : "never") },
            {
              id: "state",
              header: "State",
              cell: (t) =>
                t.revoked_at ? (
                  <StatusIndicator type="stopped">revoked</StatusIndicator>
                ) : t.expires_at && new Date(t.expires_at) < new Date() ? (
                  <StatusIndicator type="warning">expired</StatusIndicator>
                ) : (
                  <StatusIndicator type="success">active{t.expires_at ? ` until ${new Date(t.expires_at).toLocaleDateString()}` : ""}</StatusIndicator>
                ),
            },
            {
              id: "actions",
              header: "",
              cell: (t) => (!t.revoked_at ? <Button variant="inline-link" onClick={() => revoke(t.id)}>Revoke</Button> : null),
            },
          ]}
        />
      </SpaceBetween>
    </Container>
  );
}

// --- Webhooks ---------------------------------------------------------
function WebhooksSection({ pushFlash }) {
  const [hooks, setHooks] = useState([]);
  const [eventOptions, setEventOptions] = useState([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [selectedEvents, setSelectedEvents] = useState([]);
  const [minted, setMinted] = useState(null); // {name, secret}
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [h, ev] = await Promise.all([listWebhooks(), listEvents()]);
      setHooks(h);
      setEventOptions(ev.map((e) => ({ label: e.name, value: e.name, description: e.description })));
    } catch (e) {
      pushFlash("error", `Could not load webhooks: ${e.message}`);
    }
  }, [pushFlash]);
  useEffect(() => {
    load();
  }, [load]);

  async function create() {
    setBusy(true);
    try {
      const row = await createWebhook({ name: name.trim(), url: url.trim(), events: selectedEvents.length ? selectedEvents.map((o) => o.value) : ["*"] });
      setMinted({ name: row.name, secret: row.secret });
      setName(""); setUrl(""); setSelectedEvents([]);
      await load();
    } catch (e) {
      pushFlash("error", `Could not create webhook: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }
  async function toggle(h) {
    try {
      await updateWebhook(h.id, { name: h.name, url: h.url, events: h.events, enabled: !h.enabled });
      await load();
    } catch (e) {
      pushFlash("error", e.message);
    }
  }
  async function test(h) {
    try {
      const r = await testWebhook(h.id);
      pushFlash(r.error ? "error" : "success", r.error ? `Test delivery failed: ${r.error}` : `Test delivered (HTTP ${r.status}).`);
      await load();
    } catch (e) {
      pushFlash("error", e.message);
    }
  }
  async function remove(h) {
    try {
      await deleteWebhook(h.id);
      await load();
    } catch (e) {
      pushFlash("error", e.message);
    }
  }

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={
            <>
              Switchboard POSTs each event as JSON to the URL, signed with <Box variant="code" display="inline">X-Switchboard-Signature: sha256=&lt;hmac&gt;</Box> over
              the raw body using the secret shown once at creation. Three tries with backoff; failures are counted here, never silently disabled.
            </>
          }
        >
          Webhooks
        </Header>
      }
    >
      <SpaceBetween size="l">
        {minted ? (
          <Alert
            type="success"
            dismissible
            onDismiss={() => setMinted(null)}
            header={`Webhook "${minted.name}" created - copy the signing secret now, it will not be shown again`}
            action={<CopyToClipboard copyButtonText="Copy secret" copyErrorText="Could not copy" copySuccessText="Copied" textToCopy={minted.secret} />}
          >
            <Box variant="code">{minted.secret}</Box>
          </Alert>
        ) : null}
        <SpaceBetween size="s">
          <SpaceBetween size="s" direction="horizontal" alignItems="end">
            <FormField label="Name">
              <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="ticketing" />
            </FormField>
            <FormField label="URL" description="http:// or https://">
              <Input value={url} onChange={({ detail }) => setUrl(detail.value)} placeholder="https://hooks.example.com/switchboard" inputMode="url" />
            </FormField>
          </SpaceBetween>
          <FormField label="Events" description="Leave empty for every event.">
            <Multiselect
              selectedOptions={selectedEvents}
              onChange={({ detail }) => setSelectedEvents(detail.selectedOptions)}
              options={eventOptions}
              placeholder="All events"
              filteringType="auto"
            />
          </FormField>
          <Button variant="primary" onClick={create} loading={busy} disabled={!name.trim() || !/^https?:\/\//.test(url.trim())}>
            Add webhook
          </Button>
        </SpaceBetween>
        <Table
          variant="embedded"
          items={hooks}
          empty={<Box color="text-status-inactive">No webhooks yet.</Box>}
          columnDefinitions={[
            { id: "name", header: "Name", cell: (h) => h.name },
            { id: "url", header: "URL", cell: (h) => <Box variant="code">{h.url}</Box> },
            { id: "events", header: "Events", cell: (h) => (h.events.includes("*") ? "all" : h.events.join(", ")) },
            {
              id: "enabled",
              header: "Enabled",
              cell: (h) => <Toggle checked={h.enabled} onChange={() => toggle(h)} ariaLabel={`Enable ${h.name}`} />,
            },
            {
              id: "last",
              header: "Last delivery",
              cell: (h) =>
                !h.last_delivery_at ? (
                  <Box color="text-status-inactive">never</Box>
                ) : h.last_error ? (
                  <StatusIndicator type="error">{h.consecutive_failures} failed - {h.last_error.slice(0, 50)}</StatusIndicator>
                ) : (
                  <StatusIndicator type="success">HTTP {h.last_status} at {new Date(h.last_delivery_at).toLocaleTimeString()}</StatusIndicator>
                ),
            },
            {
              id: "actions",
              header: "",
              cell: (h) => (
                <SpaceBetween size="xs" direction="horizontal">
                  <Button variant="inline-link" onClick={() => test(h)}>Test</Button>
                  <Button variant="inline-link" onClick={() => remove(h)}>Delete</Button>
                </SpaceBetween>
              ),
            },
          ]}
        />
      </SpaceBetween>
    </Container>
  );
}

// --- Enrolled pager devices (every user) --------------------------------
// Admins see and can remove any device: a phone that changed hands, or a
// laptop that keeps failing, should not keep being paged for the team.
function PagingDevicesSection({ pushFlash }) {
  const [rows, setRows] = useState([]);
  const load = useCallback(async () => {
    try {
      const r = await listPushSubscriptions();
      setRows(r);
    } catch (e) {
      pushFlash("error", `Could not load enrolled devices: ${e.message}`);
    }
  }, [pushFlash]);
  useEffect(() => {
    load();
  }, [load]);
  async function remove(r) {
    try {
      await unsubscribePush(r.endpoint);
      await load();
      pushFlash("info", `Removed ${r.username}'s device.`);
    } catch (e) {
      pushFlash("error", e.message);
    }
  }
  return (
    <Container header={<Header variant="h2" counter={`(${rows.length})`} description="Every browser enrolled for paging, across all accounts.">Pager devices</Header>}>
      <Table
        variant="embedded"
        items={rows}
        empty={<Box color="text-status-inactive">No devices enrolled.</Box>}
        columnDefinitions={[
          { id: "user", header: "Account", cell: (r) => r.username },
          { id: "label", header: "Device", cell: (r) => (r.label || "unknown browser").slice(0, 50) },
          { id: "sev", header: "Pages on", cell: (r) => r.min_severity },
          { id: "rep", header: "Repeats", cell: (r) => (r.repeat_minutes ? `every ${r.repeat_minutes} min, up to ${r.max_repeats}` : "once") },
          { id: "used", header: "Last paged", cell: (r) => (r.last_used_at ? new Date(r.last_used_at).toLocaleString() : "never") },
          {
            id: "health",
            header: "Health",
            cell: (r) => (r.failures ? <StatusIndicator type="warning">{r.failures} failed</StatusIndicator> : <StatusIndicator type="success">ok</StatusIndicator>),
          },
          { id: "rm", header: "", cell: (r) => <Button variant="inline-link" onClick={() => remove(r)}>Remove</Button> },
        ]}
      />
    </Container>
  );
}

export default function SettingsPage({ pushFlash }) {
  const isAdmin = useHasRole("admin");
  // Saving is admin-tier server-side (require_admin_no_db on the PUT route
  // in app.py) - viewers/operators can still see the page and the health
  // panel (GET is any-authenticated-user) but the form is read-only.
  const canEdit = useHasRole("admin");
  const [loading, setLoading] = useState(true);
  const [current, setCurrent] = useState(null);
  const [form, setForm] = useState({ database_url: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [health, setHealth] = useState(null);
  const [healthLoading, setHealthLoading] = useState(false);

  const refreshHealth = useCallback(async () => {
    setHealthLoading(true);
    try {
      setHealth(await getSettingsHealth());
    } catch (e) {
      // A failed health *fetch* is itself worth showing, rather than
      // leaving the panel silently stale and looking fine.
      //
      // A 404 specifically means this endpoint doesn't exist on the
      // server, which - since this panel only ships alongside it - means
      // the frontend bundle is newer than the Python backend serving it.
      // That happens when a deploy copies webui/frontend/dist but not
      // webui/*.py (or doesn't restart the service), and it surfaced
      // exactly that way in production. "Not Found" gives no clue; say
      // what's actually wrong.
      const stale = /HTTP 404|Not Found/i.test(e.message || "");
      setHealth({
        checks: [],
        error: stale
          ? "This page is newer than the server it's talking to - /api/settings/health doesn't exist there yet. " +
            "Copy webui/*.py and common/*.py to the app directory and restart the service, then reload."
          : e.message,
      });
    } finally {
      setHealthLoading(false);
    }
  }, []);

  const loadSettings = useCallback(async () => {
    const data = await getSettings();
    setCurrent(data);
    const next = { database_url: "" };
    SERVICE_FIELDS.forEach((f) => {
      next[f.key] = data[f.key] || "";
    });
    setForm(next);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        await loadSettings();
        await refreshHealth();
      } catch (e) {
        pushFlash("error", `Could not load settings: ${e.message}`);
      } finally {
        setLoading(false);
      }
    })();
  }, [pushFlash, loadSettings, refreshHealth]);

  // Health is a point-in-time probe of five separate services, so it goes
  // stale quickly - refreshed on a timer rather than only on page load,
  // which would keep showing a service as reachable long after it stopped.
  useEffect(() => {
    const id = setInterval(refreshHealth, 30_000);
    return () => clearInterval(id);
  }, [refreshHealth]);

  function setField(name, value) {
    setForm((prev) => ({ ...prev, [name]: value }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    const payload = { database_url: form.database_url.trim() || null };
    SERVICE_FIELDS.forEach((f) => {
      payload[f.key] = form[f.key] ?? "";
    });
    try {
      await updateSettings(payload);
      pushFlash("success", "Settings saved.");
      await loadSettings();
    } catch (err) {
      // The server saves the service URLs even when Postgres is
      // unreachable - that's deliberate, since this page is how a broken
      // deployment gets fixed. So this is a warning beside the form, not
      // an error implying nothing was written.
      setError(err.message);
    } finally {
      setSaving(false);
      await refreshHealth();
    }
  }

  if (loading) return <Spinner />;

  return (
    <SpaceBetween size="l">
      {current?.db_error && (
        <Alert type="error" header="Database unavailable">
          {current.db_error} — devices, results, and status polling won't work until this is fixed below.
        </Alert>
      )}
      {!canEdit && (
        <Alert type="info">
          You need the admin role to change these settings. Contact an admin if something here needs updating.
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description="Whether each service answers, and whether data is still arriving from the two that feed this app. Refreshes every 30s."
            actions={
              <Button iconName="refresh" loading={healthLoading} onClick={refreshHealth}>
                Check now
              </Button>
            }
          >
            Service health
          </Header>
        }
      >
        {health?.error ? (
          <Alert type="error">Could not run health checks: {health.error}</Alert>
        ) : (
          <Table
            variant="embedded"
            items={health?.checks || []}
            loading={healthLoading && !health}
            loadingText="Checking services"
            trackBy="name"
            empty={<Box color="text-status-inactive">No checks reported.</Box>}
            columnDefinitions={[
              { id: "name", header: "Service", cell: (c) => <Box fontWeight="bold">{c.name}</Box> },
              {
                id: "status",
                header: "Status",
                // A freshness check reads red when the far end is up but
                // silent, so "unreachable" would send someone to debug the
                // wrong thing entirely.
                cell: (c) =>
                  c.ok ? (
                    <StatusIndicator type="success">
                      {c.kind === "flow" ? "flowing" : "reachable"}
                    </StatusIndicator>
                  ) : (
                    <StatusIndicator type="error">
                      {c.kind === "flow" ? "no data" : "unreachable"}
                    </StatusIndicator>
                  ),
              },
              { id: "target", header: "Target", cell: (c) => <Box variant="code">{c.target || "-"}</Box> },
              {
                id: "detail",
                header: "Detail",
                cell: (c) => (
                  <Box color={c.ok ? "text-status-inactive" : "text-status-error"}>{c.detail}</Box>
                ),
              },
            ]}
          />
        )}
      </Container>

      <form onSubmit={handleSubmit}>
        <Form
          actions={
            canEdit && (
              <Button variant="primary" formAction="submit" loading={saving}>
                Save changes
              </Button>
            )
          }
        >
          <SpaceBetween size="l">
            <Container header={<Header variant="h2">Database</Header>}>
              <FormField
                label="Postgres connection string"
                description={`Currently: ${current?.database_url_display || "not set"}. Leave blank to keep it.`}
                constraintText="postgresql://user:password@host:5432/dbname"
              >
                <Input
                  value={form.database_url}
                  onChange={(e) => setField("database_url", e.detail.value)}
                  placeholder="postgresql://user:password@host:5432/switchboard"
                  disabled={!canEdit}
                />
              </FormField>
            </Container>

            <Container
              header={
                <Header
                  variant="h2"
                  description="Applied immediately on save - no restart needed. These seed from environment variables on first boot; after that, what's saved here wins."
                >
                  Services
                </Header>
              }
            >
              <SpaceBetween size="m">
                {SERVICE_FIELDS.map((f) => (
                  <FormField key={f.key} label={f.label} description={f.description}>
                    <Input
                      value={form[f.key] ?? ""}
                      onChange={(e) => setField(f.key, e.detail.value)}
                      placeholder={f.placeholder}
                      disabled={!canEdit}
                    />
                  </FormField>
                ))}
              </SpaceBetween>
            </Container>

            {error && <Alert type="warning">{error}</Alert>}
          </SpaceBetween>
        </Form>
      </form>
      {isAdmin ? <ApiTokensSection pushFlash={pushFlash} /> : null}
      {isAdmin ? <WebhooksSection pushFlash={pushFlash} /> : null}
  {isAdmin ? <PagingDevicesSection pushFlash={pushFlash} /> : null}
    </SpaceBetween>
  );
}
