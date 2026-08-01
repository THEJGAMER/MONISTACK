import React, { useEffect, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import TokenGroup from "@cloudscape-design/components/token-group";
import Input from "@cloudscape-design/components/input";
import ColumnLayout from "@cloudscape-design/components/column-layout";

import { getCompliance, getComplianceConfig, updateComplianceConfig } from "./api.js";

const CHECK_LABELS = {
  ntp_configured: "NTP configured",
  expected_vlans_present: "Expected VLANs present",
  lag_uplinks_healthy: "LAG uplinks healthy",
};

function statusType(status) {
  if (status === "pass") return "success";
  if (status === "fail") return "error";
  return "info";
}

export default function CompliancePage({ pushFlash }) {
  const [findings, setFindings] = useState([]);
  const [summary, setSummary] = useState(null);
  const [running, setRunning] = useState(false);
  const [expectedVlans, setExpectedVlans] = useState([]);
  const [vlanInput, setVlanInput] = useState("");
  const [savingConfig, setSavingConfig] = useState(false);

  async function loadConfig() {
    try {
      const cfg = await getComplianceConfig();
      setExpectedVlans(cfg.expected_vlans || []);
    } catch (e) {
      pushFlash("error", `Could not load compliance config: ${e.message}`);
    }
  }

  useEffect(() => {
    loadConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runChecks() {
    setRunning(true);
    try {
      const res = await getCompliance();
      setFindings(res.findings);
      setSummary(res.summary);
    } catch (e) {
      pushFlash("error", `Compliance run failed: ${e.message}`);
    } finally {
      setRunning(false);
    }
  }

  async function addVlan() {
    const n = parseInt(vlanInput, 10);
    if (!Number.isFinite(n) || n < 1 || n > 4094 || expectedVlans.includes(n)) return;
    const next = [...expectedVlans, n].sort((a, b) => a - b);
    setSavingConfig(true);
    try {
      await updateComplianceConfig({ expected_vlans: next });
      setExpectedVlans(next);
      setVlanInput("");
    } catch (e) {
      pushFlash("error", `Could not save: ${e.message}`);
    } finally {
      setSavingConfig(false);
    }
  }

  async function removeVlan(n) {
    const next = expectedVlans.filter((v) => v !== n);
    setSavingConfig(true);
    try {
      await updateComplianceConfig({ expected_vlans: next });
      setExpectedVlans(next);
    } catch (e) {
      pushFlash("error", `Could not save: ${e.message}`);
    } finally {
      setSavingConfig(false);
    }
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h2" description="VLANs every device is expected to carry. Leave empty to skip the VLAN check.">
            Expected VLANs
          </Header>
        }
      >
        <SpaceBetween size="s">
          <SpaceBetween size="xs" direction="horizontal">
            <Input
              type="number"
              placeholder="VLAN ID"
              value={vlanInput}
              onChange={({ detail }) => setVlanInput(detail.value)}
              onKeyDown={({ detail }) => detail.key === "Enter" && addVlan()}
            />
            <Button loading={savingConfig} onClick={addVlan}>
              Add
            </Button>
          </SpaceBetween>
          <TokenGroup
            items={expectedVlans.map((n) => ({ label: String(n) }))}
            onDismiss={({ detail }) => removeVlan(expectedVlans[detail.itemIndex])}
          />
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            description="Fleet-wide invariants, checked live against every device - not cached."
            actions={
              <Button variant="primary" loading={running} onClick={runChecks}>
                Run checks
              </Button>
            }
          >
            Compliance
          </Header>
        }
      >
        {summary && (
          <Box padding={{ bottom: "m" }}>
            <ColumnLayout columns={3} variant="text-grid">
              <StatusIndicator type="success">{summary.pass} passing</StatusIndicator>
              <StatusIndicator type="error">{summary.fail} failing</StatusIndicator>
              <StatusIndicator type="info">{summary.skip} skipped</StatusIndicator>
            </ColumnLayout>
          </Box>
        )}
        <Table
          variant="embedded"
          loading={running}
          items={findings}
          columnDefinitions={[
            { id: "device", header: "Device", cell: (f) => f.device_name },
            { id: "check", header: "Check", cell: (f) => CHECK_LABELS[f.check] || f.check },
            { id: "status", header: "Status", cell: (f) => <StatusIndicator type={statusType(f.status)}>{f.status}</StatusIndicator> },
            { id: "detail", header: "Detail", cell: (f) => f.detail },
          ]}
          empty={<Box textAlign="center">Click "Run checks" to sweep the fleet.</Box>}
        />
      </Container>
    </SpaceBetween>
  );
}
