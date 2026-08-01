import React, { useEffect, useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Select from "@cloudscape-design/components/select";
import Input from "@cloudscape-design/components/input";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Toggle from "@cloudscape-design/components/toggle";
import StatusIndicator from "@cloudscape-design/components/status-indicator";

import {
  createSchedule,
  deleteSchedule,
  getParamValues,
  listSchedules,
  runScheduleNow,
  updateSchedule,
} from "./api.js";

function formatTime(iso) {
  if (!iso) return "never";
  return new Date(iso).toLocaleString();
}

export default function SchedulesPage({ devices, commandTree, pushFlash }) {
  const [schedules, setSchedules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [deviceId, setDeviceId] = useState(null);
  const [category, setCategory] = useState(null);
  const [command, setCommand] = useState(null);
  const [paramValue, setParamValue] = useState(null);
  const [paramOptions, setParamOptions] = useState([]);
  const [intervalMinutes, setIntervalMinutes] = useState("60");
  const [creating, setCreating] = useState(false);
  const [runningId, setRunningId] = useState(null);

  async function refresh() {
    setLoading(true);
    try {
      setSchedules(await listSchedules());
    } catch (e) {
      pushFlash("error", `Could not load schedules: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const deviceOptions = devices.map((d) => ({ label: d.name, value: d.id }));
  const device = devices.find((d) => d.id === deviceId);
  const categories = device ? commandTree[device.platform] || [] : [];
  const categoryOptions = categories.map((c) => ({ label: c.label, value: c.id }));
  const items = category ? categories.find((c) => c.id === category)?.items || [] : [];
  const commandOptions = items.map((it) => ({ label: it.label, value: it.id }));
  const selectedItem = items.find((it) => it.id === command);

  async function onCommandChange(item) {
    setCommand(item.value);
    setParamValue(null);
    setParamOptions([]);
    const spec = items.find((it) => it.id === item.value);
    if (spec?.param && deviceId) {
      try {
        const values = await getParamValues(deviceId, spec.param);
        setParamOptions(values.map((v) => ({ label: v, value: v })));
      } catch (e) {
        pushFlash("error", `Could not load ${spec.param} values: ${e.message}`);
      }
    }
  }

  async function handleCreate() {
    if (!deviceId || !category || !command) return;
    const minutes = parseInt(intervalMinutes, 10);
    if (!Number.isFinite(minutes) || minutes < 5) {
      pushFlash("error", "Interval must be at least 5 minutes.");
      return;
    }
    if (selectedItem?.param && !paramValue) {
      pushFlash("error", `Choose a value for ${selectedItem.param} first.`);
      return;
    }
    setCreating(true);
    try {
      await createSchedule({
        device_id: deviceId,
        category_id: category,
        command_id: command,
        params: selectedItem?.param ? { [selectedItem.param]: paramValue } : undefined,
        interval_minutes: minutes,
      });
      pushFlash("success", "Schedule created.");
      setDeviceId(null);
      setCategory(null);
      setCommand(null);
      setParamValue(null);
      refresh();
    } catch (e) {
      pushFlash("error", `Could not create schedule: ${e.message}`);
    } finally {
      setCreating(false);
    }
  }

  async function handleToggle(schedule, enabled) {
    try {
      await updateSchedule(schedule.id, { enabled });
      refresh();
    } catch (e) {
      pushFlash("error", `Could not update schedule: ${e.message}`);
    }
  }

  async function handleRunNow(schedule) {
    setRunningId(schedule.id);
    try {
      await runScheduleNow(schedule.id);
      pushFlash("success", "Ran now - see Saved Results.");
      refresh();
    } catch (e) {
      pushFlash("error", `Run failed: ${e.message}`);
    } finally {
      setRunningId(null);
    }
  }

  async function handleDelete(schedule) {
    try {
      await deleteSchedule(schedule.id);
      setSchedules((prev) => prev.filter((s) => s.id !== schedule.id));
    } catch (e) {
      pushFlash("error", `Could not delete schedule: ${e.message}`);
    }
  }

  function deviceName(id) {
    return devices.find((d) => d.id === id)?.name || id;
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h2" description="Runs a command on a repeating interval and auto-saves the output, same as any other run.">
            New schedule
          </Header>
        }
      >
        <SpaceBetween size="s" direction="horizontal">
          <Select
            placeholder="Device"
            selectedOption={deviceOptions.find((o) => o.value === deviceId) || null}
            onChange={({ detail }) => {
              setDeviceId(detail.selectedOption.value);
              setCategory(null);
              setCommand(null);
              setParamValue(null);
              setParamOptions([]);
            }}
            options={deviceOptions}
          />
          <Select
            placeholder="Category"
            disabled={!deviceId}
            selectedOption={categoryOptions.find((o) => o.value === category) || null}
            onChange={({ detail }) => {
              setCategory(detail.selectedOption.value);
              setCommand(null);
              setParamValue(null);
              setParamOptions([]);
            }}
            options={categoryOptions}
          />
          <Select
            placeholder="Command"
            disabled={!category}
            selectedOption={commandOptions.find((o) => o.value === command) || null}
            onChange={({ detail }) => onCommandChange(detail.selectedOption)}
            options={commandOptions}
          />
          {selectedItem?.param && (
            <Select
              placeholder={selectedItem.param}
              selectedOption={paramOptions.find((o) => o.value === paramValue) || null}
              onChange={({ detail }) => setParamValue(detail.selectedOption.value)}
              options={paramOptions}
            />
          )}
          <Input
            type="number"
            placeholder="Minutes"
            value={intervalMinutes}
            onChange={({ detail }) => setIntervalMinutes(detail.value)}
          />
          <Button variant="primary" loading={creating} disabled={!deviceId || !command} onClick={handleCreate}>
            Create
          </Button>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Schedules</Header>}>
        <Table
          variant="embedded"
          loading={loading}
          items={schedules}
          columnDefinitions={[
            { id: "device", header: "Device", cell: (s) => deviceName(s.device_id) },
            { id: "command", header: "Command", cell: (s) => `${s.category_id}/${s.command_id}` },
            { id: "interval", header: "Every", cell: (s) => `${s.interval_minutes}m` },
            {
              id: "enabled",
              header: "Enabled",
              cell: (s) => <Toggle checked={s.enabled} onChange={({ detail }) => handleToggle(s, detail.checked)} />,
            },
            { id: "last_run", header: "Last run", cell: (s) => formatTime(s.last_run_at) },
            { id: "next_run", header: "Next run", cell: (s) => (s.enabled ? formatTime(s.next_run_at) : "-") },
            {
              id: "status",
              header: "Status",
              cell: (s) =>
                s.last_error ? (
                  <StatusIndicator type="error">{s.last_error}</StatusIndicator>
                ) : s.last_run_at ? (
                  <StatusIndicator type="success">ok</StatusIndicator>
                ) : (
                  <StatusIndicator type="pending">not run yet</StatusIndicator>
                ),
            },
            {
              id: "actions",
              header: "",
              cell: (s) => (
                <SpaceBetween size="xs" direction="horizontal">
                  <Button variant="inline-link" loading={runningId === s.id} onClick={() => handleRunNow(s)}>
                    Run now
                  </Button>
                  <Button variant="inline-link" onClick={() => handleDelete(s)}>
                    Delete
                  </Button>
                </SpaceBetween>
              ),
            },
          ]}
          empty={<Box textAlign="center">No schedules yet.</Box>}
        />
      </Container>
    </SpaceBetween>
  );
}
