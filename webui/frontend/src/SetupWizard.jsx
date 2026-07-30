import React, { useState } from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";

import { submitSetup } from "./api.js";

const EMPTY = {
  webui_user: "admin",
  webui_pass: "",
  database_url: "",
  loki_url: "",
};

export default function SetupWizard({ onConfigured }) {
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  function setField(name, value) {
    setForm((prev) => ({ ...prev, [name]: value }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await submitSetup(form);
      onConfigured();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  const canSubmit = form.webui_user.trim() && form.webui_pass && form.database_url.trim();

  return (
    <AppLayout
      navigationHide
      toolsHide
      contentType="form"
      content={
        <Box padding={{ vertical: "xxl" }}>
          <Box textAlign="center" margin={{ bottom: "l" }}>
            <Header variant="h1">Set up Switchboard</Header>
            <Box color="text-body-secondary">
              This deployment hasn't been configured yet. These settings can be changed later from the Settings page.
            </Box>
          </Box>
          <Box margin={{ horizontal: "auto" }} maxWidth="500px">
            <form onSubmit={handleSubmit}>
              <Form
                actions={
                  <Button variant="primary" formAction="submit" loading={saving} disabled={!canSubmit}>
                    Save and continue
                  </Button>
                }
              >
                <Container header={<Header variant="h2">Admin login</Header>}>
                  <SpaceBetween size="m">
                    <FormField label="Username" description="Used to sign in to this console (HTTP basic auth).">
                      <Input value={form.webui_user} onChange={(e) => setField("webui_user", e.detail.value)} />
                    </FormField>
                    <FormField label="Password">
                      <Input
                        type="password"
                        value={form.webui_pass}
                        onChange={(e) => setField("webui_pass", e.detail.value)}
                      />
                    </FormField>
                  </SpaceBetween>
                </Container>
                <Container header={<Header variant="h2">Data sources</Header>}>
                  <SpaceBetween size="m">
                    <FormField
                      label="Postgres connection string"
                      description="Where Switchboard stores devices added through the UI and saved command results."
                      constraintText="postgresql://user:password@host:5432/dbname"
                    >
                      <Input
                        value={form.database_url}
                        onChange={(e) => setField("database_url", e.detail.value)}
                        placeholder="postgresql://user:password@host:5432/switchboard"
                      />
                    </FormField>
                    <FormField
                      label="Loki URL (optional)"
                      description="Feeds the Syslog page and Alarm History. Can be added later if you don't have one yet."
                    >
                      <Input
                        value={form.loki_url}
                        onChange={(e) => setField("loki_url", e.detail.value)}
                        placeholder="http://loki-host:3100"
                      />
                    </FormField>
                  </SpaceBetween>
                </Container>
                {error && <Alert type="error">{error}</Alert>}
              </Form>
            </form>
          </Box>
        </Box>
      }
    />
  );
}
