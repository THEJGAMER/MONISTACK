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

import { getSettings, updateSettings, getSettingsHealth } from "./api.js";
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
    key: "exporter_url",
    label: "Exporter URL",
    description:
      "Only used for the health check below - Prometheus scrapes the exporter directly, not the webui.",
    placeholder: "http://exporter-host:9101",
  },
];

export default function SettingsPage({ pushFlash }) {
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
    </SpaceBetween>
  );
}
