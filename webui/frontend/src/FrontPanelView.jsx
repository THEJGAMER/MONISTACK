import React, { useMemo } from "react";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Popover from "@cloudscape-design/components/popover";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import StatusIndicator from "@cloudscape-design/components/status-indicator";

import { CHASSIS_PROFILES } from "./chassisProfiles.js";

function parsePort(port, regex) {
  const m = regex.exec(port);
  if (!m) return null;
  return { prefix: m[1], num: parseInt(m[2], 10) };
}

function portStateLabel(state) {
  return { up: "Up", down: "Down (no link)", admin_down: "Administratively down" }[state] || "Unknown";
}

function portStateIndicator(state) {
  return { up: "success", down: "error", admin_down: "stopped" }[state] || "info";
}

function transceiverItems(iface) {
  const t = iface.transceiver;
  if (!t) return [];
  if (!t.present) {
    return [{ label: "Transceiver", value: "Not present" }];
  }
  const items = [{ label: "Transceiver type", value: t.type || "Unknown" }];
  if (t.dom_supported) {
    items.push(
      { label: "Temperature", value: t.temperature_c != null ? `${t.temperature_c} C` : "-" },
      { label: "Voltage", value: t.voltage_v != null ? `${t.voltage_v} V` : "-" },
      { label: "Tx bias current", value: t.tx_bias_ma != null ? `${t.tx_bias_ma} mA` : "-" },
      { label: "Tx power", value: t.tx_power_dbm != null ? `${t.tx_power_dbm} dBm` : "-" },
      { label: "Rx power", value: t.rx_power_dbm != null ? `${t.rx_power_dbm} dBm` : "-" }
    );
  } else {
    items.push({ label: "Optical diagnostics", value: "Not available (DAC/AOC cable - no light readings)" });
  }
  return items;
}

function getPortState(iface) {
  if (!iface) return "admin_down";
  return iface.port_state || (iface.status === "Up" ? "up" : "down");
}

// A port "slot" mirrors the real faceplate: a printed number above/below a
// port cage - the link LED itself lives in the shared MidRow between the
// top and bottom cage (see below), not up here, matching where the
// reference photo actually shows it: right at the middle of the SFP+ pair.
function NumRow({ num }) {
  return (
    <div className="switch-port-numrow">
      <span>{num}</span>
    </div>
  );
}

function Cage({ wide, accurate }) {
  return (
    <div className={`switch-port-cage${accurate ? " accurate" : ""}`} style={wide ? { width: 46 } : undefined}>
      {accurate && <div className="switch-port-latch" />}
    </div>
  );
}

// Four arrows in a single row between the top and bottom cage, matching a
// real close-up photo's "LNK 1▲▼2" silk-screening: left pair is Link (up
// arrow = top port's state, down arrow = bottom port's), right pair is
// Activity (real traffic from the switch's own Rate info, not a
// fabricated blink) - same arrow shapes, different color logic.
function MidRow({ top, bottom, ledStyle }) {
  const topState = getPortState(top);
  const bottomState = getPortState(bottom);
  if (ledStyle === "single") {
    // Real EX3300 reference photos show one small link LED per port
    // column (not Dell's four-arrow link/activity pair) - a single dot
    // per port, stacked to match the top/bottom port it belongs to.
    return (
      <div className="switch-port-midrow single">
        <span className="switch-port-dot" data-state={topState} />
        <span className="switch-port-dot" data-state={bottomState} />
      </div>
    );
  }
  return (
    <div className="switch-port-midrow">
      <span className="switch-port-arrow up link" data-state={topState} />
      <span className="switch-port-arrow down link" data-state={bottomState} />
      <span className="switch-port-arrow up act" data-active={top?.activity ? "true" : "false"} />
      <span className="switch-port-arrow down act" data-active={bottom?.activity ? "true" : "false"} />
    </div>
  );
}

function Port({ iface, num, wide, variant, accurate }) {
  const state = getPortState(iface);
  const hoverBits = [iface.port, portStateLabel(state)];
  if (iface.description) hoverBits.push(iface.description);
  if (iface.transceiver?.present && iface.transceiver.type) hoverBits.push(iface.transceiver.type);
  if (iface.speed && iface.speed !== "Auto") hoverBits.push(iface.speed);
  if (iface.activity) hoverBits.push("active");
  const body =
    variant === "top" ? (
      <>
        <NumRow num={num} />
        <Cage wide={wide} accurate={accurate} />
      </>
    ) : (
      <>
        <Cage wide={wide} accurate={accurate} />
        <NumRow num={num} />
      </>
    );

  return (
    <Popover
      triggerType="custom"
      header={iface.port}
      content={
        <KeyValuePairs
          columns={1}
          items={[
            { label: "State", value: <StatusIndicator type={portStateIndicator(state)}>{portStateLabel(state)}</StatusIndicator> },
            {
              label: "Activity",
              value:
                iface.input_mbps != null
                  ? `${iface.activity ? "Active" : "Idle"} (in ${iface.input_mbps} Mbit/s, out ${iface.output_mbps} Mbit/s)`
                  : "-",
            },
            { label: "Description", value: iface.description || "-" },
            { label: "Speed", value: iface.speed || "-" },
            { label: "Duplex", value: iface.duplex || "-" },
            { label: "Vlan", value: iface.vlan || "-" },
            ...transceiverItems(iface),
          ]}
        />
      }
    >
      <div title={hoverBits.join(" - ")} style={{ display: "flex", flexDirection: "column", cursor: "default" }}>
        {body}
      </div>
    </Popover>
  );
}

function EmptyPort({ num, wide, variant, accurate }) {
  const numRow = <NumRow num={num} />;
  const cage = <Cage wide={wide} accurate={accurate} />;
  return (
    <div style={{ display: "flex", flexDirection: "column", opacity: 0.4 }} title={`Port ${num} - no data`}>
      {variant === "top" ? (
        <>
          {numRow}
          {cage}
        </>
      ) : (
        <>
          {cage}
          {numRow}
        </>
      )}
    </div>
  );
}

// A block of columns (the 48-port main bank, or the 6-port QSFP+ uplink
// bank) - both share the same top-numbers / top-cages / link-LEDs /
// bottom-cages / bottom-numbers structure seen in the reference photo, the
// uplink bank just has wider cages and its own "QSFP+" divider label.
function PortBlock({ columns, staggered, wide, accurate, dividerLabel, ledStyle }) {
  return (
    <div className="switch-port-block">
      <div className="switch-port-columns">
        {columns.map((col, idx) => (
          <div key={idx} style={{ display: "flex", flexDirection: "column", marginRight: col.groupBreak ? 12 : 1 }}>
            {col.top ? (
              <Port iface={col.top} num={col.topNum} wide={wide} variant="top" accurate={accurate} />
            ) : (
              <EmptyPort num={col.topNum} wide={wide} variant="top" accurate={accurate} />
            )}
            {staggered && <MidRow top={col.top} bottom={col.bottom} ledStyle={ledStyle} />}
            {staggered &&
              (col.bottom ? (
                <Port iface={col.bottom} num={col.bottomNum} wide={wide} variant="bottom" accurate={accurate} />
              ) : (
                <EmptyPort num={col.bottomNum} wide={wide} variant="bottom" accurate={accurate} />
              ))}
          </div>
        ))}
      </div>
      {staggered && accurate && dividerLabel && (
        <div className="switch-port-divider">
          <span>{dividerLabel}</span>
        </div>
      )}
    </div>
  );
}

function Led({ label, ok, title }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 3 }} title={title}>
      <div
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: ok ? "#2bb534" : "#db0000",
          boxShadow: ok ? "0 0 3px 1px #2bb534" : "0 0 3px 1px #db0000",
          border: "1px solid #0e0f11",
        }}
      />
      <span style={{ color: "#8a8f98", fontSize: 8 }}>{label}</span>
    </div>
  );
}

export default function FrontPanelView({ device, status, profileId, onProfileChange, onRefresh, refreshing }) {
  const profile = CHASSIS_PROFILES[profileId] || CHASSIS_PROFILES["generic-48"];
  const isAccurate = profile.chassisType !== "generic";
  const isJuniper = profile.chassisType === "juniper";

  const { mainByNum, uplinkByNum, uplinkCount } = useMemo(() => {
    const interfaces = status?.interfaces || [];
    const byNum = new Map();
    interfaces
      .filter((i) => parsePort(i.port, profile.portRegex)?.prefix === profile.mainPrefix)
      .forEach((i) => byNum.set(parsePort(i.port, profile.portRegex).num, i));
    const upByNum = new Map();
    let count = 0;
    const uplinkRegex = profile.uplinkPortRegex || profile.portRegex;
    if (profile.uplinkPrefix) {
      interfaces
        .filter((i) => parsePort(i.port, uplinkRegex)?.prefix === profile.uplinkPrefix)
        .forEach((i) => upByNum.set(parsePort(i.port, uplinkRegex).num, i));
      count = upByNum.size;
    }
    return { mainByNum: byNum, uplinkByNum: upByNum, uplinkCount: count };
  }, [status, profile]);

  const mainColumns = useMemo(() => {
    const start = profile.mainStart;
    if (profile.staggered) {
      const numCols = profile.mainCount / 2;
      const colsPerGroup = profile.groupSize / 2;
      return Array.from({ length: numCols }, (_, c) => ({
        top: mainByNum.get(start + 2 * c),
        bottom: mainByNum.get(start + 2 * c + 1),
        topNum: start + 2 * c,
        bottomNum: start + 2 * c + 1,
        groupBreak: (c + 1) % colsPerGroup === 0 && c !== numCols - 1,
      }));
    }
    return Array.from({ length: profile.mainCount }, (_, c) => ({
      top: mainByNum.get(start + c),
      bottom: null,
      topNum: start + c,
      bottomNum: null,
      groupBreak: (c + 1) % profile.groupSize === 0 && c !== profile.mainCount - 1,
    }));
  }, [profile, mainByNum]);

  const uplinkColumns = useMemo(() => {
    if (!profile.uplinkCount) return [];
    const start = profile.uplinkStart;
    if (profile.uplinkStaggered) {
      const numCols = profile.uplinkCount / 2;
      return Array.from({ length: numCols }, (_, c) => ({
        top: uplinkByNum.get(start + 2 * c),
        bottom: uplinkByNum.get(start + 2 * c + 1),
        topNum: start + 2 * c,
        bottomNum: start + 2 * c + 1,
        groupBreak: false,
      }));
    }
    return Array.from({ length: profile.uplinkCount }, (_, c) => ({
      top: uplinkByNum.get(start + c),
      bottom: null,
      topNum: start + c,
      bottomNum: null,
      groupBreak: false,
    }));
  }, [profile, uplinkByNum]);

  const fanAlarm = (status?.alarms || []).some((a) => a.toLowerCase().includes("fan"));
  const psuAlarm = (status?.alarms || []).some((a) => a.toLowerCase().includes("psu"));

  const profileOptions = Object.values(CHASSIS_PROFILES).map((p) => ({ label: p.label, value: p.id }));

  return (
    <SpaceBetween size="l">
      <SpaceBetween size="s" direction="horizontal" alignItems="center">
        <Select
          selectedOption={profileOptions.find((o) => o.value === profileId) ?? profileOptions[0]}
          onChange={({ detail }) => onProfileChange(detail.selectedOption.value)}
          options={profileOptions}
        />
        <Button iconName="refresh" loading={refreshing} onClick={onRefresh}>
          Refresh
        </Button>
      </SpaceBetween>

      {!status ? (
        <Box color="text-status-inactive">Loading live port status...</Box>
      ) : (
        <SpaceBetween size="xs">
          <div className="switch-chassis">
            <div className="switch-rack-ear left">
              <span className="screw" />
              <span className="screw" />
            </div>

            <div className="switch-mgmt">
              {profile.chassisType === "dell" ? (
                <>
                  <div className="switch-logo">DELL</div>
                  <div className="switch-icon-grid">
                    <span>⚑</span>
                    <span>⇅</span>
                    <span>↺</span>
                    <span>i</span>
                  </div>
                  <div className="switch-model-label">S4048-ON</div>
                </>
              ) : isJuniper ? (
                // Real photos show a small, subtle "Juniper" wordmark at
                // the far left above the port bank - not a prominent
                // colored badge like Dell's - the model name/status
                // instead lives in the LCD panel between the port banks.
                <div className="switch-juniper-logo">Juniper</div>
              ) : (
                <div className="switch-model-label" style={{ writingMode: "vertical-rl" }}>
                  {profile.label}
                </div>
              )}
            </div>

            <div className="switch-ports">
              {/* Both groups get the same header-slot/gap/port-stack structure,
                  even though only the QSFP+ group's slot has content - so the
                  actual port rows land at the same y-offset in both groups
                  regardless of container alignment. Centering two groups of
                  different total height independently (empty slot vs a real
                  header) was what caused the ports to visually drift apart -
                  this way there's nothing to misalign. */}
              <div className="switch-port-group">
                <div className="switch-port-group-header" />
                <PortBlock columns={mainColumns} staggered={profile.staggered} accurate={isAccurate} dividerLabel={isJuniper ? null : "SFP+"} ledStyle={profile.ledStyle} />
              </div>

              {isJuniper && (
                // Real photos show the "EX3300" label, an LCD status
                // display, and a couple of small indicator LEDs clustered
                // together between the RJ45 bank and the SFP+ uplinks -
                // not before all the ports like Dell's mgmt panel.
                <div className="switch-juniper-status">
                  <div className="switch-juniper-model">EX3300</div>
                  <div className="switch-juniper-lcd">
                    {status.state === "down" ? "DOWN" : status.state === "alarm" ? "ALARM" : "OK"}
                  </div>
                  <SpaceBetween size="xs" direction="horizontal" alignItems="center">
                    <Led label="STAT" ok={status.state !== "down"} title={`System: ${status.state}`} />
                    <Led label="ALM" ok={status.state !== "alarm"} title={status.state === "alarm" ? "Active alarm" : "No alarms"} />
                  </SpaceBetween>
                </div>
              )}

              {profile.uplinkCount > 0 && (
                <div className="switch-port-group">
                  <div className="switch-port-group-header">
                    {profile.chassisType === "dell" && (
                      <>
                        <div className="switch-stackid">
                          <div className="switch-stackid-digit">1</div>
                          <span>Stack ID</span>
                        </div>
                        <SpaceBetween size="xs" direction="horizontal" alignItems="center">
                          <Led label="SYS" ok={status.state !== "down"} title={`System: ${status.state}`} />
                          <Led label="FAN" ok={!fanAlarm} title={fanAlarm ? "Fan alarm" : "Fans OK"} />
                          <Led label="PSU" ok={!psuAlarm} title={psuAlarm ? "PSU alarm" : "PSUs OK"} />
                        </SpaceBetween>
                      </>
                    )}
                  </div>
                  <PortBlock
                    columns={uplinkColumns}
                    staggered={profile.uplinkStaggered}
                    wide={profile.chassisType === "dell"}
                    accurate={isAccurate}
                    dividerLabel={isJuniper ? null : "QSFP+"}
                    ledStyle={profile.ledStyle}
                  />
                </div>
              )}
            </div>

            <div className="switch-rack-ear right">
              <span className="screw" />
              <span className="screw" />
            </div>
          </div>

          <span style={{ color: "#8c8c94", fontSize: 12 }}>
            {mainByNum.size} x {profile.mainPrefix} ports
            {profile.uplinkCount ? ` + ${uplinkCount} x ${profile.uplinkPrefix} uplinks` : ""} - updated{" "}
            {status.age_seconds != null ? `${Math.round(status.age_seconds)}s ago` : "never"}
          </span>
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
