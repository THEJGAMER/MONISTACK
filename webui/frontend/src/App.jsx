import React, { useEffect, useState, useCallback } from "react";
import AppLayout from "@cloudscape-design/components/app-layout";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Flashbar from "@cloudscape-design/components/flashbar";
import { applyMode, applyDensity, Mode, Density } from "@cloudscape-design/global-styles";

import ConsolePage from "./ConsolePage.jsx";
import DevicesPage from "./DevicesPage.jsx";
import ResultsPage from "./ResultsPage.jsx";
import { getDevices, getCommands } from "./api.js";

let flashId = 0;

function usePreference(key, defaultValue) {
  const [value, setValue] = useState(() => localStorage.getItem(key) || defaultValue);
  useEffect(() => {
    localStorage.setItem(key, value);
  }, [key, value]);
  return [value, setValue];
}

export default function App() {
  const [activeHref, setActiveHref] = useState("#/console");
  const [devices, setDevices] = useState([]);
  const [commandTree, setCommandTree] = useState([]);
  const [flashes, setFlashes] = useState([]);
  const [loaded, setLoaded] = useState(false);

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
        const [devs, cmds] = await Promise.all([getDevices(), getCommands()]);
        setDevices(devs);
        setCommandTree(cmds);
      } catch (e) {
        pushFlash("error", `Failed to load: ${e.message}`);
      } finally {
        setLoaded(true);
      }
    })();
  }, [pushFlash]);

  const page = activeHref === "#/devices" ? "devices" : activeHref === "#/results" ? "results" : "console";
  const pageTitles = { console: "Console", devices: "Devices", results: "Saved Results" };

  return (
    <>
      <div id="top-nav">
        <TopNavigation
          identity={{ href: "#/console", title: "Switchboard", logo: undefined }}
          utilities={[
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
            header={{ href: "#/console", text: "Switchboard" }}
            onFollow={(e) => {
              e.preventDefault();
              setActiveHref(e.detail.href);
            }}
            items={[
              { type: "link", text: "Console", href: "#/console" },
              { type: "link", text: "Devices", href: "#/devices" },
              { type: "link", text: "Saved Results", href: "#/results" },
            ]}
          />
        }
        breadcrumbs={
          <BreadcrumbGroup
            items={[
              { text: "Switchboard", href: "#/console" },
              { text: pageTitles[page], href: activeHref },
            ]}
          />
        }
        notifications={<Flashbar items={flashes} />}
        content={
          loaded &&
          (page === "devices" ? (
            <DevicesPage devices={devices} refreshDevices={refreshDevices} pushFlash={pushFlash} />
          ) : page === "results" ? (
            <ResultsPage pushFlash={pushFlash} />
          ) : (
            <ConsolePage devices={devices} commandTree={commandTree} pushFlash={pushFlash} />
          ))
        }
      />
    </>
  );
}
