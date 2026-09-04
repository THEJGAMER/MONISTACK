import React from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";

// Reached only after the login has already been retried and failed again
// (see api_auth_callback). A single failure is usually just a stale state
// cookie and is retried silently, so getting here means something is
// actually wrong rather than merely expired.
//
// Standalone, like AccessDeniedPage: no session exists at this point, so
// there is no AuthContext and none of the normal shell can render. It must
// also be excluded from App's identity check, or the page bounces back to
// Keycloak before anyone can read it - which is the exact loop this page
// exists to end.
export default function LoginFailedPage({ reason }) {
  return (
    <AppLayout
      navigationHide
      toolsHide
      contentType="form"
      content={
        <Box padding={{ vertical: "xxl" }}>
          <Box textAlign="center" margin={{ bottom: "l" }}>
            <Header variant="h1">Could not sign you in</Header>
          </Box>
          <Box margin={{ horizontal: "auto" }} maxWidth="560px">
            <Container>
              <SpaceBetween size="l">
                <Alert type="error" header="Login failed after retrying">
                  Switchboard sent you back to Keycloak to try again and the sign-in failed a
                  second time, so this is not simply an expired login.
                </Alert>
                {reason ? (
                  <Box>
                    <Box variant="awsui-key-label">What the identity provider reported</Box>
                    <Box variant="code" fontSize="body-s">{reason}</Box>
                  </Box>
                ) : null}
                <Box color="text-body-secondary">
                  Logging out is what usually breaks this: it ends the Keycloak session that is
                  silently re-authenticating into the same failure. If it persists, the usual
                  causes are a client secret that no longer matches, a redirect URI Keycloak does
                  not recognise, or clock skew between the two hosts.
                </Box>
                <SpaceBetween size="xs" direction="horizontal">
                  <Button variant="primary" onClick={() => (window.location.href = "/api/auth/logout")}>
                    Log out and start over
                  </Button>
                  <Button onClick={() => (window.location.href = "/api/auth/login")}>
                    Try again
                  </Button>
                </SpaceBetween>
              </SpaceBetween>
            </Container>
          </Box>
        </Box>
      }
    />
  );
}
