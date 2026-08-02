# S4048 SSH-polled monitoring stack

Monitors Dell EMC OS9 switches (started with one S4048-ON at
`192.168.4.106`, now any number of them registered through the webui's
Devices page) purely over SSH `show` commands instead of SNMP. Stack: a
custom Python exporter (Prometheus format) + Prometheus + Grafana, all run
via Docker Compose.

There's a second, complementary piece: [syslog/](syslog/README.md) — the
switch also sends syslog to an LXC (`192.168.0.144`), where Vector parses it
into structured events (interface link-state changes, auth events, etc.).
That's what makes "interface goes down" show up immediately instead of
waiting on the next 30s poll.

A third piece, [webui/](webui/README.md) — **Switchboard** — is a web app,
built with the real Cloudscape design system (what AWS Console itself uses,
via React + `@cloudscape-design/components`), for searching a device and
running a pre-approved, read-only command from a menu (no free-text CLI
ever reaches the switch), plus a Devices page for registering new switches
by IP, make/model/OS, and a password or SSH key.

`common/` holds the device registry and SSH-client code shared between
`webui/` and `exporter/` (device-adding UI and metrics polling both need
to agree on the same device list and know how to talk to the same
switches) - one copy, not two drifting duplicates. Docker builds for both
services use the repo root as their build context specifically so they can
each pull from `common/` (see `webui/Dockerfile`/`exporter/Dockerfile`).

## How it works

`exporter/exporter.py` polls **every OS9 device in the registry** - not
just one hardcoded switch. The registry is the same one the webui manages:
`common/devices.yaml` (static entries, credentials from env vars) plus
whatever's been added through the Devices page (Postgres, if `DATABASE_URL`
is set - see `.env.example`). One background thread per device, each
opening its own persistent SSH session via `common/ssh_client.py` (shared
with the webui - logs in, runs `enable`, disables paging, reconnects
automatically if the session drops) and polling on a loop. The registry
itself is re-read every `REGISTRY_REFRESH_INTERVAL` (default 60s), so
adding, editing, or removing a device in the webui takes effect here
without restarting the exporter - no manual reconfiguration needed.
Junos devices are polled too (`common/junos_parsers.py`, shared with the
webui's Console - same live-verified parsers, not a second guess at the
output format), using Junos-appropriate commands for the same metrics:
`show chassis routing-engine` (CPU/memory/temp), `show chassis
environment` (fans/PSUs/sensors), `show interfaces terse` + `show
interfaces descriptions` (link status). Some fields don't map 1:1 - Junos
reports one instantaneous CPU snapshot rather than OS9's per-core/
5sec/1min/5min breakdown, fan health is qualitative ("Spinning at normal
speed") with no RPM number, and this hardware's PSU rows report no
wattage - see `exporter.py`'s `poll_fast_junos`/`poll_slow_junos`
docstrings for exactly what's derived vs. real per field. Per-port
negotiated speed and optical diagnostics run on the slow cycle instead of
the fast one: `show interfaces extensive` (needed for real speed - the
`Speed:` field on the fast-cyclable commands just reports the port's
configured mode, "Auto") took ~19s against a real 48-port EX3300, far too
slow for a 10-30s fast cycle. OPNsense devices are still listed and
skipped (logged once, not silently ignored) - no parser exists for that
platform yet.

Each device's thread polls two groups on its own session:

- **Fast (every 30s, `FAST_POLL_INTERVAL`)**: `show processes cpu`,
  `show memory`, `show environment`, `show interfaces status`.
- **Slow (every 300s, `TRANSCEIVER_POLL_INTERVAL`)**: per-port
  `show interfaces <port> transceiver` optical diagnostics (temp, voltage,
  bias current, Tx/Rx power, alarm flags) for all 54 ports. This is
  sequential over one SSH session, so it's deliberately not run every cycle.

Metrics are served on `:9101/metrics` for Prometheus to scrape, one
`device_id` label distinguishing devices on every metric (see "Metrics
exposed" below).

## Running it

This repo supports two ways to run the exporter itself:

1. **Directly on an LXC** (no Docker) — see [Installing on an LXC](#installing-on-an-lxc-no-docker) below. Point the `prometheus/prometheus.yml` `targets` at the LXC's IP:9101, and still run Prometheus/Grafana via Docker Compose (or however you already run them) elsewhere.
2. **Everything in Docker Compose** (exporter + Prometheus + Grafana bundled) — the original all-in-one path, described right here.

### Docker Compose (all-in-one)

```
docker compose up -d --build
```

- Exporter metrics: http://localhost:9101/metrics
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin / value of `GRAFANA_ADMIN_PASSWORD` in `.env`, default `admin`)
  - Dashboard "Dell S4048-ON (SSH polled)" is auto-provisioned.

Credentials live in `.env` (gitignored, not committed). `.env.example` shows
the shape.

## Metrics exposed

Every metric below carries a `device_id` label too (the same id shown in
the webui's Devices page) - omitted from the list for brevity. Metric
*names* still say `s4048_*` even though this can now poll more than one
switch - renaming them would break every existing Grafana dashboard and
Prometheus alert rule, which is a separate, bigger change than adding a
label was.

- `s4048_up{device_id}` — 1 if the last SSH poll cycle succeeded
- `s4048_cpu_utilization_percent{device_id,core,window}`
- `s4048_memory_bytes{device_id,type}`
- `s4048_fan_status{device_id,unit,bay,fan}` / `s4048_fan_speed_rpm{...}`
- `s4048_psu_status{device_id,unit,bay}` / `s4048_psu_power_watts{device_id,unit,bay,kind}`
- `s4048_unit_temperature_celsius{device_id,unit}` / `s4048_sensor_temperature_celsius{device_id,sensor}`
- `s4048_interface_up{device_id,port,description}` / `s4048_interface_speed_mbps{device_id,port}`
- `s4048_transceiver_present{device_id,port}`, `_temperature_celsius`, `_voltage_volts`,
  `_tx_bias_ma`, `_tx_power_dbm`, `_rx_power_dbm`, `_alarm{device_id,port,flag}`

## Notes

- The switch's `admin` account lands in unprivileged EXEC (`>`); the
  exporter escalates with `enable` using the same password
  (`SWITCH_ENABLE_PASS` env var can override if it's ever set differently).
- The account only has read (`show`) commands run against it — nothing in
  this stack issues config-mode commands.
- The temp password used to set this up was shared in plaintext in chat;
  worth rotating it on the switch once you're done validating the stack.

## Deploying on an LXC

Three full walkthroughs, depending on what you need:

- **[docs/deploy-lxc-docker.md](docs/deploy-lxc-docker.md)** — the whole
  stack (webui + exporter + Prometheus + Alertmanager + Grafana) via
  `docker compose`, in one LXC.
- **[docs/deploy-lxc-split.md](docs/deploy-lxc-split.md)** — the same five
  services, but each on its own LXC for maximum isolation - covers what
  breaks (compose's built-in service-name DNS) and how each piece is
  reconnected across real IPs instead.
- **[docs/deploy-lxc-exporter.md](docs/deploy-lxc-exporter.md)** — just the
  exporter, native, no Docker, as a systemd service (`packaging/`) - for
  when Prometheus/Grafana already exist elsewhere. Point them at
  `<lxc-ip>:9101`.

Both install paths for the exporter-only route were smoke-tested against
the live switch during development: the venv path via the Docker image
(same `exporter.py`), and the standalone binary by running it directly with
real credentials and confirming `show processes cpu` / transceiver metrics
came back correctly.
