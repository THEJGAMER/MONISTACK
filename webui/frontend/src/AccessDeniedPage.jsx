import React from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";

// Landed here straight from /api/auth/callback, not from anywhere inside
// the app - a Keycloak login succeeded but the account has no
// viewer/operator/admin client role assigned (see api_auth_callback in
// app.py), so no session was ever created. No AuthContext/user exists at
// this point, which is why this renders standalone rather than inside the
// normal AppLayout+SideNavigation shell every other page uses.
export default function AccessDeniedPage({ username }) {
  return (
    <AppLayout
      navigationHide
      toolsHide
      contentType="form"
      content={
        <Box padding={{ vertical: "xxl" }}>
          <Box textAlign="center" margin={{ bottom: "l" }}>
            <Header variant="h1">Access denied</Header>
          </Box>
          <Box margin={{ horizontal: "auto" }} maxWidth="500px">
            <Container>
              <SpaceBetween size="l">
                <Alert type="warning" header="No role assigned">
                  {username ? (
                    <>
                      You signed in to Keycloak as <b>{username}</b>, but that account has no
                      viewer/operator/admin role assigned for Switchboard.
                    </>
                  ) : (
                    <>You signed in to Keycloak, but that account has no role assigned for Switchboard.</>
                  )}
                </Alert>
                <Box color="text-body-secondary">
                  Ask an admin to assign you a role in Keycloak (Users → your account → Role mapping → filter by
                  clients → <code>switchboard</code>), then try again.
                </Box>
                <Button onClick={() => (window.location.href = "/api/auth/logout")}>Log out</Button>
              </SpaceBetween>
            </Container>
          </Box>
        </Box>
      }
    />
  );
}
