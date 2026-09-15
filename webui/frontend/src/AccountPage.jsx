import React from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";
import Select from "@cloudscape-design/components/select";
import Toggle from "@cloudscape-design/components/toggle";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import { useState } from "react";
import { usePush, useInstallPrompt } from "./usePush.js";

// Internal, read-only identity/permission info - the "why do I have this
// role" debugging view. Deliberately doesn't touch password/MFA/session
// management at all; that's Keycloak's own account console, linked out to
// below, not reimplemented here (see api_auth_me's docstring in app.py).
export default function AccountPage({user, pushFlash }) {
  if (!user) return null;

  const loginAt = user.login_at ? new Date(user.login_at).toLocaleString() : "-";
  const expiresAt = user.expires_at ? new Date(user.expires_at * 1000).toLocaleString() : "-";

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Identity &amp; permissions</Header>}>
        <SpaceBetween size="l">
          <KeyValuePairs
            columns={2}
            items={[
              { label: "Username", value: user.username },
              { label: "Email", value: user.email || "-" },
              {
                label: "Role",
                value: (
                  <Badge color={user.role === "admin" ? "red" : user.role === "operator" ? "blue" : "grey"}>
                    {user.role}
                  </Badge>
                ),
              },
              {
                label: "Granted roles (Keycloak claim)",
                value:
                  user.roles_claim && user.roles_claim.length > 0 ? (
                    <SpaceBetween size="xs" direction="horizontal">
                      {user.roles_claim.map((r) => (
                        <Badge key={r}>{r}</Badge>
                      ))}
                    </SpaceBetween>
                  ) : (
                    <Box color="text-body-secondary">none assigned - defaulted to viewer</Box>
                  ),
              },
              { label: "Logged in", value: loginAt },
              { label: "Session expires", value: expiresAt },
            ]}
          />
          {user.roles_claim && user.roles_claim.length === 0 && (
            <Alert type="info">
              No client roles are assigned to you in Keycloak, so you're on the viewer tier by default. Ask an admin
              to assign a role on the Keycloak client if you expect more access.
            </Alert>
          )}
        </SpaceBetween>
      </Container>
      <Container header={<Header variant="h2">Password &amp; security</Header>}>
        <SpaceBetween size="m">
          <Box color="text-body-secondary">
            Password changes, MFA, and active-session management all happen in Keycloak's own account console, not
            here.
          </Box>
          <Box>
            <Button
              iconName="external"
              href={user.account_url || undefined}
              target="_blank"
              rel="noopener noreferrer"
              disabled={!user.account_url}
            >
              Manage password &amp; security in Keycloak
            </Button>
          </Box>
        </SpaceBetween>
      </Container>
      <PagingSection pushFlash={pushFlash} />
    </SpaceBetween>
  );
}

// Paging to *this browser on this device*. A subscription is bound to the
// browser, not the account: the same person's phone and laptop subscribe
// separately, and each sets its own floor - the phone on critical only,
// the laptop on everything. Adapted from PROXMON's push UI and extended
// with the per-device severity floor, the resolve flag, a test page, and
// the install prompt (a home-screen install is what makes notifications
// reliable on iOS at all).
const SEVERITIES = [
  { label: "Critical only", value: "critical" },
  { label: "Warning and above", value: "warning" },
  { label: "Everything, including info", value: "info" },
];

export function PagingSection({ pushFlash }) {
  const push = usePush();
  const install = useInstallPrompt();
  const [minSeverity, setMinSeverity] = useState(SEVERITIES[1]);
  const [notifyResolved, setNotifyResolved] = useState(true);
  const [busy, setBusy] = useState(false);
  const flash = (type, text) => (pushFlash ? pushFlash(type, text) : null);

  async function onSubscribe() {
    setBusy(true);
    try {
      await push.subscribe({ minSeverity: minSeverity.value, notifyResolved });
      flash("success", "This device will now be paged.");
    } catch (e) {
      flash("error", e.message);
    } finally {
      setBusy(false);
    }
  }
  async function onUnsubscribe() {
    setBusy(true);
    try {
      await push.unsubscribe();
      flash("info", "This device will no longer be paged.");
    } catch (e) {
      flash("error", e.message);
    } finally {
      setBusy(false);
    }
  }
  async function onTest() {
    setBusy(true);
    try {
      const r = await push.test();
      flash(r.ok ? "success" : "error", r.ok ? "Test page sent - it should appear in a moment." : `Test failed: ${r.error}`);
    } catch (e) {
      flash("error", e.message);
    } finally {
      setBusy(false);
    }
  }

  const serverOff = push.config && !push.config.enabled;
  const blocked = !push.supported || !push.secure || serverOff;
  const reason = !push.supported
    ? "This browser does not support Web Push."
    : !push.secure
      ? "Push needs HTTPS (or localhost). Open the site over https:// to enable it."
      : serverOff
        ? "Push is not enabled on the server (pywebpush is not installed, or the VAPID key could not be created)."
        : null;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Alarms are delivered to this device as notifications, with the app closed, through the browser's push service. No third-party pager involved."
        >
          Paging on this device
        </Header>
      }
    >
      <SpaceBetween size="l">
        {reason ? <Alert type="warning">{reason}</Alert> : null}
        {push.error ? <Alert type="error">{push.error}</Alert> : null}
        {!install.standalone && install.available ? (
          <Alert
            type="info"
            header="Install as an app"
            action={<Button onClick={() => install.promptInstall()}>Install</Button>}
          >
            Installing to the home screen keeps notifications reliable - on iOS it is the only way they arrive at all.
          </Alert>
        ) : null}
        <SpaceBetween size="s" direction="horizontal" alignItems="center">
          <StatusIndicator type={push.subscribed ? "success" : "stopped"}>
            {push.subscribed ? "This device is subscribed" : "This device is not subscribed"}
          </StatusIndicator>
          <Box color="text-status-inactive" fontSize="body-s">permission: {push.permission}</Box>
        </SpaceBetween>
        {!push.subscribed ? (
          <SpaceBetween size="s">
            <Select selectedOption={minSeverity} onChange={({ detail }) => setMinSeverity(detail.selectedOption)} options={SEVERITIES} disabled={blocked} />
            <Toggle checked={notifyResolved} onChange={({ detail }) => setNotifyResolved(detail.checked)} disabled={blocked}>
              Also tell me when an alarm resolves
            </Toggle>
            <Button variant="primary" onClick={onSubscribe} loading={busy || push.busy} disabled={blocked}>
              Page this device
            </Button>
          </SpaceBetween>
        ) : (
          <SpaceBetween size="xs" direction="horizontal">
            <Button onClick={onTest} loading={busy || push.busy}>Send a test page</Button>
            <Button onClick={onUnsubscribe} loading={busy || push.busy}>Stop paging this device</Button>
          </SpaceBetween>
        )}
        <Table
          variant="embedded"
          items={push.config?.subscriptions || []}
          empty={<Box color="text-status-inactive">No devices subscribed for your account yet.</Box>}
          columnDefinitions={[
            { id: "label", header: "Device", cell: (r) => (r.label || "unknown browser").slice(0, 60) },
            { id: "sev", header: "Pages on", cell: (r) => r.min_severity },
            { id: "res", header: "Resolves", cell: (r) => (r.notify_resolved ? "yes" : "no") },
            { id: "used", header: "Last paged", cell: (r) => (r.last_used_at ? new Date(r.last_used_at).toLocaleString() : "never") },
            {
              id: "health",
              header: "Health",
              cell: (r) =>
                r.failures ? (
                  <StatusIndicator type="warning">{r.failures} failed{r.last_error ? ` - ${r.last_error.slice(0, 60)}` : ""}</StatusIndicator>
                ) : (
                  <StatusIndicator type="success">ok</StatusIndicator>
                ),
            },
          ]}
        />
      </SpaceBetween>
    </Container>
  );
}
