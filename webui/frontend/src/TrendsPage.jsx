import React, { useEffect, useMemo, useState, useCallback } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import FormField from "@cloudscape-design/components/form-field";
import Select from "@cloudscape-design/components/select";
import LineChart from "@cloudscape-design/components/line-chart";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Spinner from "@cloudscape-design/components/spinner";
import Grid from "@cloudscape-design/components/grid";

import { getTrendSeries, getTrendData } from "./api.js";

// The metrics ROADMAP.md 3.4 calls out, in the order they're most likely
// to matter to someone checking this page: optic health first (the
// "predicts failing optics before links drop" use case), then the two
// general-purpose trending/forecasting metrics.
const METRIC_ORDER = [
  "optic_rx_power_dbm",
  "optic_tx_power_dbm",
  "optic_temp_c",
  "psu_power_watts",
  "iface_input_mbps",
  "iface_output_mbps",
  "iface_input_errors",
  "iface_output_errors",
];

const HOURS_OPTIONS = [
  { label: "Last 24 hours", value: "24" },
  { label: "Last 7 days", value: "168" },
  { label: "Last 30 days", value: "720" },
  { label: "Last 90 days", value: "2160" },
];

function seriesKey(s) {
  return `${s.metric}:${s.port || ""}`;
}

export default function TrendsPage({ devices, pushFlash }) {
  const [deviceId, setDeviceId] = useState(devices[0]?.id || null);
  const [series, setSeries] = useState([]);
  const [seriesLoading, setSeriesLoading] = useState(true);
  const [selectedKey, setSelectedKey] = useState(null);
  const [hours, setHours] = useState("168");
  const [data, setData] = useState(null);
  const [dataLoading, setDataLoading] = useState(false);

  useEffect(() => {
    if (!deviceId) return;
    setSeriesLoading(true);
    setSelectedKey(null);
    setData(null);
    getTrendSeries(deviceId)
      .then(({ series: s }) => {
        const sorted = [...s].sort((a, b) => {
          const ai = METRIC_ORDER.indexOf(a.metric);
          const bi = METRIC_ORDER.indexOf(b.metric);
          return ai - bi || (a.port || "").localeCompare(b.port || "");
        });
        setSeries(sorted);
        if (sorted.length > 0) setSelectedKey(seriesKey(sorted[0]));
      })
      .catch((e) => pushFlash("error", `Could not load trend series: ${e.message}`))
      .finally(() => setSeriesLoading(false));
  }, [deviceId, pushFlash]);

  const selected = useMemo(() => series.find((s) => seriesKey(s) === selectedKey) || null, [series, selectedKey]);

  const loadData = useCallback(async () => {
    if (!deviceId || !selected) return;
    setDataLoading(true);
    try {
      const result = await getTrendData(deviceId, selected.metric, selected.port, Number(hours));
      setData(result);
    } catch (e) {
      pushFlash("error", `Could not load trend data: ${e.message}`);
    } finally {
      setDataLoading(false);
    }
  }, [deviceId, selected, hours, pushFlash]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const deviceOptions = devices.map((d) => ({ label: d.name, value: d.id }));
  const seriesOptions = series.map((s) => ({
    label: s.port ? `${s.label} — ${s.port}` : s.label,
    value: seriesKey(s),
  }));

  const chartSeries = data
    ? [
        {
          title: data.label,
          type: "line",
          data: data.samples.map((s) => ({ x: new Date(s.recorded_at), y: s.value })),
        },
      ]
    : [];

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description="Trends over already-collected metrics - optic Rx/Tx power and temperature, PSU power draw, and interface utilization/error counts - to catch gradual degradation before it becomes an outage."
          >
            Trends
          </Header>
        }
      >
        <Grid gridDefinition={[{ colspan: 4 }, { colspan: 5 }, { colspan: 3 }]}>
          <FormField label="Device">
            <Select
              selectedOption={deviceOptions.find((o) => o.value === deviceId) || null}
              onChange={({ detail }) => setDeviceId(detail.selectedOption.value)}
              options={deviceOptions}
              placeholder="Select a device"
            />
          </FormField>
          <FormField label="Metric">
            <Select
              selectedOption={seriesOptions.find((o) => o.value === selectedKey) || null}
              onChange={({ detail }) => setSelectedKey(detail.selectedOption.value)}
              options={seriesOptions}
              placeholder={seriesLoading ? "Loading..." : "No trend data yet"}
              disabled={seriesLoading || seriesOptions.length === 0}
              statusType={seriesLoading ? "loading" : "finished"}
            />
          </FormField>
          <FormField label="Time range">
            <Select
              selectedOption={HOURS_OPTIONS.find((o) => o.value === hours)}
              onChange={({ detail }) => setHours(detail.selectedOption.value)}
              options={HOURS_OPTIONS}
            />
          </FormField>
        </Grid>
      </Container>

      {!seriesLoading && series.length === 0 && (
        <Alert type="info">
          No trend data yet for this device. Samples are recorded on the status poller's slow cadence (every 5
          minutes) - check back shortly after the device has been polling, or confirm it supports the relevant
          metric (optics/PSU wattage are Dell OS9-only today).
        </Alert>
      )}

      {selected && (
        <Container header={<Header variant="h2">{data?.label || selected.label}{selected.port ? ` — ${selected.port}` : ""}</Header>}>
          <SpaceBetween size="m">
            {data?.alert && (
              <Alert type="warning" header="Trend threshold crossed">
                {data.alert.message}
              </Alert>
            )}
            {data?.forecast && (
              <Alert type="info" header="Capacity forecast">
                {data.forecast.message}
              </Alert>
            )}
            {dataLoading ? (
              <Spinner size="large" />
            ) : !data || data.samples.length === 0 ? (
              <Box textAlign="center" color="text-body-secondary">
                No samples in this time range yet.
              </Box>
            ) : (
              <LineChart
                series={chartSeries}
                xScaleType="time"
                height={300}
                hideFilter
                xTitle="Time"
                yTitle={data.label}
                i18nStrings={{
                  xTickFormatter: (d) => new Date(d).toLocaleString(),
                  yTickFormatter: (v) => `${v}`,
                }}
              />
            )}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
