import React, { useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Select from "@cloudscape-design/components/select";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Alert from "@cloudscape-design/components/alert";

import { bulkRun, getParamValues } from "./api.js";
import { lineDiff } from "./diff.js";

export default function BulkRunPage({ devices, commandTree, pushFlash }) {
  const [selectedDevices, setSelectedDevices] = useState([]);
  const [platform, setPlatform] = useState(null);
  const [category, setCategory] = useState(null);
  const [command, setCommand] = useState(null);
  const [paramValue, setParamValue] = useState(null);
  const [paramOptions, setParamOptions] = useState([]);
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState(null);
  const [expandedDevice, setExpandedDevice] = useState(null);

  const platformOptions = useMemo(
    () => Object.keys(commandTree || {}).map((p) => ({ label: p, value: p })),
    [commandTree]
  );
  const categories = platform ? commandTree[platform] || [] : [];
  const categoryOptions = categories.map((c) => ({ label: c.label, value: c.id }));
  const items = category ? categories.find((c) => c.id === category)?.items || [] : [];
  const commandOptions = items.map((it) => ({ label: it.label, value: it.id }));
  const selectedItem = items.find((it) => it.id === command);

  async function onSelectionChange({ detail }) {
    setSelectedDevices(detail.selectedItems);
  }

  function onPlatformChange(value) {
    setPlatform(value);
    setCategory(null);
    setCommand(null);
    setParamValue(null);
    setParamOptions([]);
  }

  async function onCommandChange(item) {
    setCommand(item.value);
    setParamValue(null);
    setParamOptions([]);
    const spec = items.find((it) => it.id === item.value);
    if (spec?.param && selectedDevices.length > 0) {
      try {
        const values = await getParamValues(selectedDevices[0].id, spec.param);
        setParamOptions(values.map((v) => ({ label: v, value: v })));
      } catch (e) {
        pushFlash("error", `Could not load ${spec.param} values: ${e.message}`);
      }
    }
  }

  async function handleRun() {
    if (selectedDevices.length === 0 || !category || !command) return;
    if (selectedItem?.param && !paramValue) {
      pushFlash("error", `Choose a value for ${selectedItem.param} first.`);
      return;
    }
    setRunning(true);
    setResults(null);
    try {
      const params = selectedItem?.param ? { [selectedItem.param]: paramValue } : undefined;
      const res = await bulkRun({
        device_ids: selectedDevices.map((d) => d.id),
        category_id: category,
        command_id: command,
        params,
      });
      setResults(res.results);
    } catch (e) {
      pushFlash("error", `Bulk run failed: ${e.message}`);
    } finally {
      setRunning(false);
    }
  }

  const baseline = results?.find((r) => !r.error);

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            description="Run one command across several devices at once and see where the output diverges."
          >
            Bulk Run
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Table
            variant="embedded"
            selectionType="multi"
            selectedItems={selectedDevices}
            onSelectionChange={onSelectionChange}
            items={devices}
            columnDefinitions={[
              { id: "name", header: "Device", cell: (d) => d.name },
              { id: "platform", header: "Platform", cell: (d) => d.platform },
              { id: "host", header: "Host", cell: (d) => d.host },
            ]}
            empty={<Box textAlign="center">No devices configured.</Box>}
          />

          <SpaceBetween size="s" direction="horizontal">
            <Select
              placeholder="Platform"
              selectedOption={platformOptions.find((o) => o.value === platform) || null}
              onChange={({ detail }) => onPlatformChange(detail.selectedOption.value)}
              options={platformOptions}
            />
            <Select
              placeholder="Category"
              disabled={!platform}
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
            <Button
              variant="primary"
              loading={running}
              disabled={selectedDevices.length === 0 || !command}
              onClick={handleRun}
            >
              Run on {selectedDevices.length || 0} device(s)
            </Button>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {results && (
        <Container header={<Header variant="h2">Results</Header>}>
          <SpaceBetween size="s">
            {!baseline && <Alert type="error">Every device failed - see details below.</Alert>}
            <Table
              variant="embedded"
              items={results}
              columnDefinitions={[
                { id: "device", header: "Device", cell: (r) => r.device_name },
                {
                  id: "status",
                  header: "Result",
                  cell: (r) =>
                    r.error ? (
                      <StatusIndicator type="error">{r.error}</StatusIndicator>
                    ) : baseline && r.output === baseline.output ? (
                      <StatusIndicator type="success">Matches baseline</StatusIndicator>
                    ) : r === baseline ? (
                      <StatusIndicator type="info">Baseline ({r.device_name})</StatusIndicator>
                    ) : (
                      <StatusIndicator type="warning">Differs from baseline</StatusIndicator>
                    ),
                },
                {
                  id: "actions",
                  header: "",
                  cell: (r) =>
                    !r.error && (
                      <Button
                        variant="inline-link"
                        onClick={() => setExpandedDevice(expandedDevice === r.device_id ? null : r.device_id)}
                      >
                        {expandedDevice === r.device_id ? "Hide" : "View"}
                      </Button>
                    ),
                },
              ]}
            />
            {results
              .filter((r) => !r.error && r.device_id === expandedDevice)
              .map((r) => (
                <ExpandableSection key={r.device_id} headerText={`${r.device_name} output`} defaultExpanded>
                  {baseline && r !== baseline ? (
                    <Box variant="pre">
                      <pre style={{ margin: 0, fontFamily: "monospace", fontSize: "12px", whiteSpace: "pre-wrap" }}>
                        {lineDiff(baseline.output, r.output).map((row, idx) => (
                          <div
                            key={idx}
                            style={{
                              background:
                                row.type === "added" ? "rgba(0,178,86,0.15)" : row.type === "removed" ? "rgba(217,45,32,0.15)" : "transparent",
                            }}
                          >
                            {row.type === "added" ? "+ " : row.type === "removed" ? "- " : "  "}
                            {row.text}
                          </div>
                        ))}
                      </pre>
                    </Box>
                  ) : (
                    <pre style={{ margin: 0, fontFamily: "monospace", fontSize: "12px", whiteSpace: "pre-wrap" }}>{r.output}</pre>
                  )}
                </ExpandableSection>
              ))}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
