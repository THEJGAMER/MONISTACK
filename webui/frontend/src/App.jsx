import React, { useEffect, useState, useCallback, Suspense, lazy } from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Flashbar from "@cloudscape-design/components/flashbar";
import Spinner from "@cloudscape-design/components/spinner";
import Box from "@cloudscape-design/components/box";
import { applyMode, applyDensity, Mode, Density } from "@cloudscape-design/global-styles";

import SetupWizard from "./SetupWizard.jsx";
import AccessDeniedPage from "./AccessDeniedPage.jsx";
import { getDevices, getCommands, getSetupStatus, getCurrentUser, logout } from "./api.js";
import { AuthProvider, useHasRole } from "./AuthContext.jsx";

// Each page is its own chunk - board-components (Console) and the
// markdown/plotting bits (Topology/Trends) are the heaviest deps in the
// bundle and most sessions only ever visit one or two of these pages.
const ConsolePage = lazy(() => import("./ConsolePage.jsx"));
const DevicesPage = lazy(() => import("./DevicesPage.jsx"));
const ResultsPage = lazy(() => import("./ResultsPage.jsx"));
const SettingsPage = lazy(() => import("./SettingsPage.jsx"));
const TopologyPage = lazy(() => import("./TopologyPage.jsx"));
const TrendsPage = lazy(() => import("./TrendsPage.jsx"));
const SflowPage = lazy(() => import("./SflowPage.jsx"));
const BulkRunPage = lazy(() => import("./BulkRunPage.jsx"));
const SchedulesPage = lazy(() => import("./SchedulesPage.jsx"));
const CompliancePage = lazy(() => import("./CompliancePage.jsx"));
const AlertsPage = lazy(() => import("./AlertsPage.jsx"));
const AlarmsPage = lazy(() => import("./AlarmsPage.jsx"));
const AccountPage = lazy(() => import("./AccountPage.jsx"));

function PageFallback() {
  return (
    <Box textAlign="center" padding="xxl">
      <Spinner size="large" />
    </Box>
  );
}

let flashId = 0;

/**
 * Real hash routing, replacing the plain useState this app used to keep the
 * current page in. Two things that only work with the URL as the source of
 * truth: pasting a link to a specific alarm actually opens that alarm for
 * whoever you sent it to, and the browser's back button works. Previously
 * the initial hash was ignored entirely, so every deep link silently landed
 * on the Console.
 */
function useHashRoute() {
  const read = () => window.location.hash || "#/console";
  const [href, setHref] = useState(read);
  useEffect(() => {
    const onChange = () => setHref(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const navigate = useCallback((next) => {
    // Assigning to location.hash fires hashchange, which updates state; the
    // else-branch covers navigating to where we already are (a no-op for
    // the browser, but callers still expect a render).
    if (window.location.hash !== next) window.location.hash = next;
    else setHref(next);
  }, []);
  return [href, navigate];
}

function usePreference(key, defaultValue) {
  const [value, setValue] = useState(() => localStorage.getItem(key) || defaultValue);
  useEffect(() => {
    localStorage.setItem(key, value);
  }, [key, value]);
  return [value, setValue];
}

export default function App() {
  const [activeHref, setActiveHref] = useHashRoute();
  // Landed here straight from /api/auth/callback denying a login with no
  // role assigned - no session exists, so the identity-check effect below
  // must not run for this route, or it immediately bounces back to
  // Keycloak before the page (and its Log out button) can ever render.
  const deniedMatch = activeHref.match(/^#\/access-denied\/?(.*)$/);
  const [devices, setDevices] = useState([]);
  const [commandTree, setCommandTree] = useState([]);
  const [flashes, setFlashes] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [configured, setConfigured] = useState(null); // null = still checking
  const [user, setUser] = useState(null); // {username, role}, null = still checking
  const [preselectDeviceId, setPreselectDeviceId] = useState(null);
  const [devicePrefill, setDevicePrefill] = useState(null);

  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const [mode, setMode] = usePreference("switchboard-mode", prefersDark ? Mode.Dark : Mode.Light);
  const [density, setDensity] = usePreference("switchboard-density", Density.Comfortable);

  useEffect(() => {
    applyMode(mode);
  }, [mode]);

  useEffect(() => {
    applyDensity(density);
  }, [density]);

  const pushFlash = useCallback((type, content) => {
    const id = `flash-${++flashId}`;
    setFlashes((prev) => [
      ...prev,
      {
        id,
        type,
        content,
        dismissible: true,
        onDismiss: () => setFlashes((f) => f.filter((x) => x.id !== id)),
      },
    ]);
  }, []);

  const openConsoleFor = useCallback((deviceId) => {
    setPreselectDeviceId(deviceId);
    setActiveHref("#/console");
  }, []);

  const openAddDevice = useCallback((prefill) => {
    setDevicePrefill(prefill);
    setActiveHref("#/devices");
  }, []);

  const refreshDevices = useCallback(async () => {
    try {
      setDevices(await getDevices());
    } catch (e) {
      pushFlash("error", `Could not load devices: ${e.message}`);
    }
  }, [pushFlash]);

  useEffect(() => {
    (async () => {
      try {
        const status = await getSetupStatus();
        setConfigured(status.configured);
      } catch (e) {
        // Setup-status itself is unauthenticated and should never fail once
        // the server is reachable at all - treat a failure here as "not
        // configured yet" so the user at least sees something actionable.
        setConfigured(false);
      }
    })();
  }, []);

  // Identity check - getCurrentUser() is the one call api.js doesn't
  // auto-redirect on a 401 for (see api.js), so a logged-out visit lands
  // here and this effect does the redirect itself, only once setup is
  // confirmed done (no point sending someone to Keycloak before there's
  // even a deployment to log in to).
  useEffect(() => {
    if (!configured || deniedMatch) return;
    (async () => {
      try {
        setUser(await getCurrentUser());
      } catch (e) {
        window.location.href = "/api/auth/login";
      }
    })();
  }, [configured, deniedMatch]);

  useEffect(() => {
    if (!configured || !user || deniedMatch) return;
    (async () => {
      try {
        const [devs, cmds] = await Promise.all([getDevices(), getCommands()]);
        setDevices(devs);
        setCommandTree(cmds);
      } catch (e) {
        pushFlash("error", `Failed to load: ${e.message}`);
      } finally {
        setLoaded(true);
      }
    })();
  }, [configured, user, deniedMatch, pushFlash]);

  // "#/alarms/6da766d164443d00" -> section "alarms", param "6da766d164443d00".
  // Only the Alarms page takes a path parameter today; everything else is a
  // bare section, so unknown sections still fall through to the Console.
  const [section, routeParam] = activeHref.replace(/^#\/?/, "").split("/");
  const KNOWN_PAGES = [
    "devices",
    "results",
    "topology",
    "trends",
    "sflow",
    "bulk-run",
    "schedules",
    "compliance",
    "alerts",
    "alarms",
    "settings",
    "account",
  ];
  const page = KNOWN_PAGES.includes(section) ? section : "console";
  const pageTitles = {
    console: "Console",
    devices: "Devices",
    results: "Saved Results",
    topology: "Topology",
    trends: "Trends",
    "bulk-run": "Bulk Run",
    schedules: "Schedules",
    compliance: "Compliance",
    alerts: "Alerts",
    alarms: "Alarms",
    settings: "Settings",
    account: "My Account",
  };
  // A deep-linked alarm gets its own breadcrumb level, so someone who
  // arrives from a pasted link can see where they are and get back to the
  // full list without knowing the URL scheme.
  const breadcrumbs = [
    { text: "Switchboard", href: "#/console" },
    { text: pageTitles[page], href: page === "alarms" ? "#/alarms" : activeHref },
  ];
  if (page === "alarms" && routeParam) {
    breadcrumbs.push({ text: routeParam, href: activeHref });
  }

  if (configured === null) {
    return null;
  }

  if (!configured) {
    return <SetupWizard onConfigured={() => setConfigured(true)} />;
  }

  if (deniedMatch) {
    return <AccessDeniedPage username={deniedMatch[1] ? decodeURIComponent(deniedMatch[1]) : null} />;
  }

  if (!user) {
    return null; // identity check in flight, or about to redirect to Keycloak
  }

  return (
    <AuthProvider user={user}>
      <div id="top-nav">
        <TopNavigation
          identity={{ href: "#/console", title: "Switchboard", logo: undefined }}
          utilities={[
            {
              type: "button",
              iconName: "settings",
              text: "Settings",
              href: "#/settings",
              onClick: (e) => {
                e.preventDefault();
                setActiveHref("#/settings");
              },
            },
            {
              type: "button",
              text: density === Density.Compact ? "Comfortable density" : "Compact density",
              onClick: () => setDensity(density === Density.Compact ? Density.Comfortable : Density.Compact),
            },
            {
              type: "button",
              text: mode === Mode.Dark ? "Light mode" : "Dark mode",
              onClick: () => setMode(mode === Mode.Dark ? Mode.Light : Mode.Dark),
            },
            {
              type: "menu-dropdown",
              text: `${user.username} (${user.role})`,
              iconName: "user-profile",
              items: [
                { id: "account", text: "My account" },
                { id: "logout", text: "Log out" },
              ],
              onItemClick: (e) => {
                if (e.detail.id === "logout") logout();
                else if (e.detail.id === "account") setActiveHref("#/account");
              },
            },
          ]}
        />
      </div>
      <AppLayout
        headerSelector="#top-nav"
        navigationHide={false}
        toolsHide
        navigation={
          <SideNavigation
            activeHref={activeHref}
            onFollow={(e) => {
              e.preventDefault();
              setActiveHref(e.detail.href);
            }}
            items={[
              { type: "link", text: "Console", href: "#/console" },
              { type: "link", text: "Devices", href: "#/devices" },
              { type: "link", text: "Topology", href: "#/topology" },
              { type: "link", text: "Trends", href: "#/trends" },
              { type: "link", text: "Traffic", href: "#/sflow" },
              { type: "link", text: "Saved Results", href: "#/results" },
              { type: "divider" },
              { type: "link", text: "Bulk Run", href: "#/bulk-run" },
              { type: "link", text: "Schedules", href: "#/schedules" },
              { type: "link", text: "Compliance", href: "#/compliance" },
              { type: "link", text: "Alerts", href: "#/alerts" },
              { type: "link", text: "Alarms", href: "#/alarms" },
              { type: "divider" },
              { type: "link", text: "Settings", href: "#/settings" },
            ]}
          />
        }
        breadcrumbs={
          <BreadcrumbGroup
            items={breadcrumbs}
            onFollow={(e) => {
              e.preventDefault();
              setActiveHref(e.detail.href);
            }}
          />
        }
        notifications={<Flashbar items={flashes} />}
        content={
          loaded && (
            <Suspense fallback={<PageFallback />}>
              {page === "devices" ? (
                <DevicesPage
                  devices={devices}
                  refreshDevices={refreshDevices}
                  pushFlash={pushFlash}
                  prefill={devicePrefill}
                  onPrefillConsumed={() => setDevicePrefill(null)}
                />
              ) : page === "results" ? (
                <ResultsPage pushFlash={pushFlash} />
              ) : page === "settings" ? (
                <SettingsPage pushFlash={pushFlash} />
              ) : page === "account" ? (
                <AccountPage user={user} />
              ) : page === "topology" ? (
                <TopologyPage pushFlash={pushFlash} onOpenConsole={openConsoleFor} onAddDevice={openAddDevice} />
              ) : page === "trends" ? (
                <TrendsPage devices={devices} pushFlash={pushFlash} />
              ) : page === "sflow" ? (
                <SflowPage devices={devices} pushFlash={pushFlash} />
              ) : page === "bulk-run" ? (
                <BulkRunPage devices={devices} commandTree={commandTree} pushFlash={pushFlash} />
              ) : page === "schedules" ? (
                <SchedulesPage devices={devices} commandTree={commandTree} pushFlash={pushFlash} />
              ) : page === "compliance" ? (
                <CompliancePage pushFlash={pushFlash} />
              ) : page === "alerts" ? (
                <AlertsPage devices={devices} pushFlash={pushFlash} />
              ) : page === "alarms" ? (
                <AlarmsPage
                  fingerprint={routeParam || null}
                  pushFlash={pushFlash}
                  onNavigate={setActiveHref}
                />
              ) : (
                <ConsolePage
                  devices={devices}
                  commandTree={commandTree}
                  pushFlash={pushFlash}
                  preselectDeviceId={preselectDeviceId}
                  onPreselectConsumed={() => setPreselectDeviceId(null)}
                />
              )}
            </Suspense>
          )
        }
      />
    </AuthProvider>
  );
}
