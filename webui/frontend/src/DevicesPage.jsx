import React, { useMemo, useState } from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Table from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import Button from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Textarea from "@cloudscape-design/components/textarea";
import Select from "@cloudscape-design/components/select";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";

import { createDevice, deleteDevice, getDeviceForEdit, testDevice, updateDevice } from "./api.js";
import { useClientPagination } from "./useClientPagination.js";

const PLATFORM_OPTIONS = [
  { label: "Dell OS9 / Force10 (fully supported)", value: "os9" },
  { label: "Juniper Junos (fully supported)", value: "junos" },
  { label: "Cisco IOS (experimental)", value: "ios" },
  { label: "Arista EOS (experimental)", value: "eos" },
  { label: "Cisco NX-OS (experimental)", value: "nxos" },
  { label: "Other (experimental)", value: "other" },
];

const EMPTY_FORM = {
  name: "",
  host: "",
  make: "",
  model: "",
  platform: "os9",
  username: "",
  authMethod: "password",
  password: "",
  private_key: "",
  passphrase: "",
  enable_password: "",
  portsJson: "",
};

// Parses the optional "Interface whitelist" textarea: a single JSON object
// shaped like { "ports": [...], "port_channels": {...} } - the same shape
// devices.yaml uses for statically-configured devices - so parameterized
// commands (e.g. "show interfaces <port> transceiver"/"diagnostics optics")
// have a value list to offer. Returns null (not an error) for blank input;
// throws for genuinely malformed JSON so the caller can surface it.
function parsePortsJson(text) {
  if (!text.trim()) return { ports: null, port_channels: null };
  const parsed = JSON.parse(text);
  return { ports: parsed.ports ?? null, port_channels: parsed.port_channels ?? null };
}

function toRequestBody(form, editId) {
  const { ports, port_channels } = parsePortsJson(form.portsJson);
  return {
    name: form.name,
    host: form.host,
    make: form.make,
    model: form.model,
    platform: form.platform,
    username: form.username,
    auth_method: form.authMethod,
    password: form.authMethod === "password" ? form.password || null : null,
    private_key: form.authMethod === "ssh_key" ? form.private_key || null : null,
    passphrase: form.authMethod === "ssh_key" ? form.passphrase || null : null,
    enable_password: form.enable_password || null,
    ports,
    port_channels,
    // Only meaningful to /api/devices/test - lets it fall back to the
    // existing stored secret when testing an edit without re-entering it.
    edit_id: editId || null,
  };
}

export default function DevicesPage({ devices, refreshDevices, pushFlash }) {
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState(null); // null = adding a new device
  const [loadingEdit, setLoadingEdit] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [filterText, setFilterText] = useState("");

  const filteredDevices = useMemo(() => {
    const q = filterText.toLowerCase();
    if (!q) return devices;
    return devices.filter((d) => d.name.toLowerCase().includes(q) || d.host.includes(q));
  }, [devices, filterText]);
  const { pageItems, paginationProps } = useClientPagination(filteredDevices, 10);

  function setField(name, value) {
    setForm((prev) => ({ ...prev, [name]: value }));
  }

  function openAddModal() {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setTestResult(null);
    setModalOpen(true);
  }

  async function openEditModal(device) {
    setEditingId(device.id);
    setTestResult(null);
    setModalOpen(true);
    setLoadingEdit(true);
    try {
      const data = await getDeviceForEdit(device.id);
      const portsJson =
        data.ports || data.port_channels
          ? JSON.stringify({ ports: data.ports ?? undefined, port_channels: data.port_channels ?? undefined }, null, 2)
          : "";
      setForm({
        ...EMPTY_FORM,
        name: data.name,
        host: data.host,
        make: data.make,
        model: data.model,
        platform: data.platform,
        username: data.username,
        authMethod: data.auth_method,
        // Secrets never come back from the server - left blank here, and
        // a blank submission means "keep the existing value" (see
        // app.py's api_update_device).
        portsJson,
      });
    } catch (e) {
      pushFlash("error", `Could not load device for editing: ${e.message}`);
      setModalOpen(false);
    } finally {
      setLoadingEdit(false);
    }
  }

  async function handleTest() {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testDevice(toRequestBody(form, editingId));
      setTestResult(res);
    } catch (e) {
      setTestResult({ ok: false, message: e instanceof SyntaxError ? `Interface whitelist: invalid JSON - ${e.message}` : e.message });
    } finally {
      setTesting(false);
    }
  }

  async function handleSave(e) {
    e.preventDefault();
    setSaving(true);
    try {
      if (editingId) {
        await updateDevice(editingId, toRequestBody(form));
      } else {
        await createDevice(toRequestBody(form));
      }
      setModalOpen(false);
      await refreshDevices();
      pushFlash("success", `Device "${form.name}" ${editingId ? "updated" : "added"}.`);
    } catch (e) {
      const message = e instanceof SyntaxError ? `Interface whitelist: invalid JSON - ${e.message}` : e.message;
      pushFlash("error", `Could not save device: ${message}`);
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    const id = deleteTarget.id;
    setDeleteTarget(null);
    try {
      await deleteDevice(id);
      await refreshDevices();
      pushFlash("success", `Device "${deleteTarget.name}" deleted.`);
    } catch (e) {
      pushFlash("error", `Could not delete: ${e.message}`);
    }
  }

  return (
    <>
      <Container
        header={
          <Header
            variant="h2"
            description="Switches and other network devices Switchboard can run commands against."
            actions={
              <Button variant="primary" onClick={openAddModal}>
                Add device
              </Button>
            }
          >
            Devices
          </Header>
        }
      >
        <Table
          variant="embedded"
          items={pageItems}
          filter={
            <TextFilter
              filteringText={filterText}
              onChange={({ detail }) => setFilterText(detail.filteringText)}
              filteringPlaceholder="Search devices..."
            />
          }
          pagination={<Pagination {...paginationProps} />}
          columnDefinitions={[
            { id: "name", header: "Name", cell: (d) => d.name },
            { id: "host", header: "Host", cell: (d) => <Box variant="code">{d.host}</Box> },
            { id: "makeModel", header: "Make / model", cell: (d) => [d.make, d.model].filter(Boolean).join(" / ") || "-" },
            { id: "platform", header: "Platform", cell: (d) => d.platform },
            { id: "auth", header: "Auth", cell: (d) => (d.auth_method === "ssh_key" ? "SSH key" : "Password") },
            {
              id: "source",
              header: "Source",
              cell: (d) => (
                <StatusIndicator type={d.source === "static" ? "info" : "success"}>
                  {d.source === "static" ? "Static (.env)" : "Added"}
                </StatusIndicator>
              ),
            },
            {
              id: "actions",
              header: "",
              cell: (d) =>
                d.source === "added" ? (
                  <SpaceBetween size="xs" direction="horizontal">
                    <Button variant="inline-link" onClick={() => openEditModal(d)}>
                      Edit
                    </Button>
                    <Button variant="inline-link" onClick={() => setDeleteTarget(d)}>
                      Delete
                    </Button>
                  </SpaceBetween>
                ) : null,
            },
          ]}
        />
      </Container>

      <Modal
        visible={modalOpen}
        onDismiss={() => setModalOpen(false)}
        header={editingId ? "Edit device" : "Add a device"}
        size="large"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={handleTest} loading={testing} disabled={loadingEdit}>
                Test connection
              </Button>
              <Button variant="link" onClick={() => setModalOpen(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleSave} loading={saving} disabled={loadingEdit} formAction="none">
                {editingId ? "Save changes" : "Save device"}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <form onSubmit={handleSave}>
          <Form>
            <SpaceBetween size="l">
              {testResult && (
                <Alert type={testResult.ok ? "success" : "error"}>{testResult.message}</Alert>
              )}
              <FormField label="Name">
                <Input value={form.name} onChange={({ detail }) => setField("name", detail.value)} placeholder="e.g. Core Switch - Rack 3" />
              </FormField>
              <FormField label="Host / IP">
                <Input value={form.host} onChange={({ detail }) => setField("host", detail.value)} placeholder="192.168.1.10" />
              </FormField>
              <SpaceBetween size="l" direction="horizontal">
                <FormField label="Make">
                  <Input value={form.make} onChange={({ detail }) => setField("make", detail.value)} placeholder="Dell, Cisco, Arista..." />
                </FormField>
                <FormField label="Model">
                  <Input value={form.model} onChange={({ detail }) => setField("model", detail.value)} placeholder="S4048-ON, Catalyst 9300..." />
                </FormField>
              </SpaceBetween>
              <FormField
                label="Operating system"
                description="Only Dell OS9's command set is wired up in the Console today - other platforms can be saved and reached over SSH, but the commands menu may not match its syntax."
              >
                <Select
                  selectedOption={PLATFORM_OPTIONS.find((o) => o.value === form.platform)}
                  onChange={({ detail }) => setField("platform", detail.selectedOption.value)}
                  options={PLATFORM_OPTIONS}
                />
              </FormField>
              <FormField label="Username">
                <Input value={form.username} onChange={({ detail }) => setField("username", detail.value)} placeholder="admin" />
              </FormField>

              <FormField label="Authentication">
                <SegmentedControl
                  selectedId={form.authMethod}
                  onChange={({ detail }) => setField("authMethod", detail.selectedId)}
                  options={[
                    { id: "password", text: "Password" },
                    { id: "ssh_key", text: "SSH key" },
                  ]}
                />
              </FormField>

              {form.authMethod === "password" ? (
                <FormField label="Password" description={editingId ? "Leave blank to keep the current password." : undefined}>
                  <Input type="password" value={form.password} onChange={({ detail }) => setField("password", detail.value)} />
                </FormField>
              ) : (
                <>
                  <FormField label="Private key (PEM)" description={editingId ? "Leave blank to keep the current key." : undefined}>
                    <Textarea
                      value={form.private_key}
                      onChange={({ detail }) => setField("private_key", detail.value)}
                      rows={6}
                      placeholder={"-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----"}
                    />
                  </FormField>
                  <FormField label="Key passphrase (if encrypted)" description={editingId ? "Leave blank to keep the current passphrase." : undefined}>
                    <Input type="password" value={form.passphrase} onChange={({ detail }) => setField("passphrase", detail.value)} />
                  </FormField>
                </>
              )}

              {form.platform !== "junos" && (
                <FormField
                  label="Enable password"
                  description={
                    editingId
                      ? "Leave blank to keep the current enable password."
                      : "Defaults to the login password if left blank. Not used for Junos - it has no enable/privileged-mode concept."
                  }
                >
                  <Input type="password" value={form.enable_password} onChange={({ detail }) => setField("enable_password", detail.value)} />
                </FormField>
              )}

              <FormField
                label="Interface whitelist (optional)"
                description={
                  'JSON: {"ports": [{"prefix": "Te", "range": [1, 48]}], "port_channels": {"range": [1, 8]}} - ' +
                  "generates the exact values parameterized commands (e.g. transceiver diagnostics) are allowed to " +
                  "run against; nothing outside this list is ever accepted. Add a \"template\" per ports entry for " +
                  'non-Dell naming, e.g. {"prefix": "ge", "range": [0, 47], "template": "{prefix}-0/0/{n}"} for Junos.'
                }
              >
                <Textarea
                  value={form.portsJson}
                  onChange={({ detail }) => setField("portsJson", detail.value)}
                  rows={4}
                  placeholder='{"ports": [{"prefix": "ge", "range": [0, 47], "template": "{prefix}-0/0/{n}"}], "port_channels": {"range": [1, 1], "template": "ae{n}"}}'
                />
              </FormField>
            </SpaceBetween>
          </Form>
        </form>
      </Modal>

      <Modal
        visible={!!deleteTarget}
        onDismiss={() => setDeleteTarget(null)}
        header="Delete device"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setDeleteTarget(null)}>Cancel</Button>
              <Button variant="primary" onClick={confirmDelete}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Delete device "{deleteTarget?.name}"? This only removes it from Switchboard, not the device itself.
      </Modal>
    </>
  );
}
