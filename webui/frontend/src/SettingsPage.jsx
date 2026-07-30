import React, { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";

import { getSettings, updateSettings } from "./api.js";

export default function SettingsPage({ pushFlash }) {
  const [loading, setLoading] = useState(true);
  const [current, setCurrent] = useState(null);
  const [form, setForm] = useState({ webui_user: "", webui_pass: "", database_url: "", loki_url: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const data = await getSettings();
        setCurrent(data);
        setForm({ webui_user: data.webui_user, webui_pass: "", database_url: "", loki_url: data.loki_url || "" });
      } catch (e) {
        pushFlash("error", `Could not load settings: ${e.message}`);
      } finally {
        setLoading(false);
      }
    })();
  }, [pushFlash]);

  function setField(name, value) {
    setForm((prev) => ({ ...prev, [name]: value }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await updateSettings({
        webui_user: form.webui_user,
        webui_pass: form.webui_pass || null,
        database_url: form.database_url.trim() || null,
        loki_url: form.loki_url,
      });
      pushFlash("success", "Settings saved.");
      const data = await getSettings();
      setCurrent(data);
      setForm({ webui_user: data.webui_user, webui_pass: "", database_url: "", loki_url: data.loki_url || "" });
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
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
      <form onSubmit={handleSubmit}>
        <Form
          actions={
            <Button variant="primary" formAction="submit" loading={saving}>
              Save changes
            </Button>
          }
        >
          <SpaceBetween size="l">
            <Container header={<Header variant="h2">Admin login</Header>}>
              <SpaceBetween size="m">
                <FormField label="Username">
                  <Input value={form.webui_user} onChange={(e) => setField("webui_user", e.detail.value)} />
                </FormField>
                <FormField label="Password" description="Leave blank to keep the current password.">
                  <Input
                    type="password"
                    value={form.webui_pass}
                    onChange={(e) => setField("webui_pass", e.detail.value)}
                    placeholder="••••••••"
                  />
                </FormField>
              </SpaceBetween>
            </Container>
            <Container header={<Header variant="h2">Data sources</Header>}>
              <SpaceBetween size="m">
                <FormField
                  label="Postgres connection string"
                  description={`Currently: ${current?.database_url_display || "not set"}. Leave blank to keep it.`}
                  constraintText="postgresql://user:password@host:5432/dbname"
                >
                  <Input
                    value={form.database_url}
                    onChange={(e) => setField("database_url", e.detail.value)}
                    placeholder="postgresql://user:password@host:5432/switchboard"
                  />
                </FormField>
                <FormField label="Loki URL" description="Feeds the Syslog page and Alarm History.">
                  <Input value={form.loki_url} onChange={(e) => setField("loki_url", e.detail.value)} />
                </FormField>
              </SpaceBetween>
            </Container>
            {error && <Alert type="error">{error}</Alert>}
          </SpaceBetween>
        </Form>
      </form>
    </SpaceBetween>
  );
}
