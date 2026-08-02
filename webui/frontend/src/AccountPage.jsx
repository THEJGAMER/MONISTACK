import React from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";

// Internal, read-only identity/permission info - the "why do I have this
// role" debugging view. Deliberately doesn't touch password/MFA/session
// management at all; that's Keycloak's own account console, linked out to
// below, not reimplemented here (see api_auth_me's docstring in app.py).
export default function AccountPage({ user }) {
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
    </SpaceBetween>
  );
}
