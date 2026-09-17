# S4048 SSH-polled monitoring stack

Monitors Dell EMC OS9 switches (started with one S4048-ON at
`192.168.4.106`, now any number of them registered through the webui's
Devices page) purely over SSH `show` commands instead of SNMP. Stack: a
custom Python exporter (Prometheus format) + Prometheus for metrics, and
Switchboard for everything a person looks at - including event-driven
monitoring from syslog (with SSH as the fallback), which replaced the
Prometheus/Alertmanager alerting on 2026-09-16.

There's a second, complementary piece: [syslog/](syslog/README.md) — the
switch also sends syslog to an LXC (`192.168.0.144`), where Vector parses it
into structured events (interface link-state changes, auth events, etc.).
Vector also POSTs each parsed line straight to Switchboard, which turns
it into an event within about 70 ms - see webui/README.md "Events".

A third piece, [webui/](webui/README.md) — **Switchboard** — is a web app,
built with the real Cloudscape design system (what AWS Console itself uses,
via React + `@cloudscape-design/components`), for searching a device and
running a pre-approved, read-only command from a menu (no free-text CLI
ever reaches the switch), plus a Devices page for registering new switches
by IP, make/model/OS, and a password or SSH key.

[loki/](loki/README.md) holds the deployed Loki config (the LXC at
`192.168.0.145`) - it wasn't version-controlled at all until 2026-08-23,
which meant the store keeping every log forever had its config on exactly
one machine and nowhere else.

`common/` holds the device registry and SSH-client code shared between
`webui/` and `exporter/` (device-adding UI and metrics polling both need
to agree on the same device list and know how to talk to the same
switches) - one copy, not two drifting duplicates. Docker builds for both
services use the repo root as their build context specifically so they can
each pull from `common/` (see `webui/Dockerfile`/`exporter/Dockerfile`).

## How it works

`exporter/exporter.py` polls **every OS9 and Junos device in the
registry** - not just one hardcoded switch. The registry is the same one
the webui manages:
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

1. **Directly on an LXC** (no Docker) — see [docs/deploy-lxc-exporter.md](docs/deploy-lxc-exporter.md) (or [Deploying on an LXC](#deploying-on-an-lxc) below for the other split options). Point the `prometheus/prometheus.yml` `targets` at the LXC's IP:9101, and still run Prometheus via Docker Compose (or however you already run it) elsewhere.
2. **Everything in Docker Compose** (exporter + Prometheus + webui bundled) — the all-in-one path, described right here.

### Docker Compose (all-in-one)

```
docker compose up -d --build
```

- Exporter metrics: http://localhost:9101/metrics
- Prometheus: http://localhost:9090
  - Dashboard "Dell S4048-ON (SSH polled)" is auto-provisioned.

Credentials live in `.env` (gitignored, not committed). `.env.example` shows
the shape.

## Metrics exposed

Every metric below carries a `device_id` label too (the same id shown in
the webui's Devices page) - omitted from the list for brevity. Metric
*names* still say `s4048_*` even though this can now poll more than one
switch - renaming them would break every existing dashboard and
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

## Console bastion

A real terminal on a device, in the browser, at **Bastion** in the side
navigation. It exists because eventually something goes wrong that no
allowlist anticipated, and the alternative - handing out the switch's
enable password - is worse than the thing it is meant to avoid.

It authenticates with the credentials Switchboard already holds, so
access to a switch is granted and revoked in Keycloak and nobody ever
learns the device's own password.

**Two modes, and your role decides which you may choose:**

| Role | May open | What that means |
|---|---|---|
| `admin` | full **or** read-only | full is a raw pipe: keystrokes, tab completion, configuration mode, everything the device allows |
| `operator` | read-only | free text, but only read-only commands are forwarded |
| `viewer` | nothing | free text to a live device is not a read-only-user capability |

**How read-only is enforced - twice, independently.** `webui/bastion_policy.py`
refuses anything whose first word is not a verb it recognises as
read-only for that platform, checks each pipe stage against its own
allowlist (`| save` on OS9 and `| tee`/`| append`/`| request` on Junos all
write), and treats a shell as a shell (OPNsense lands in real FreeBSD, so
whole-command patterns and no metacharacters, because `ifconfig; rm -rf /`
starts with `ifconfig`). *Separately*, a read-only session on Dell OS9
never sends `enable` - it sits in user EXEC, where the device itself
rejects configuration. One of those has to be right; both have to be
wrong to do damage.

**Text reaches the device only as whole, checked lines.** A read-only
session's keystrokes never leave the browser - the page echoes them
locally, submits a finished line, and the server sends it prefixed with
Ctrl-U so anything sitting in the device's input buffer is wiped first.
There is no way to assemble a command one character at a time past the
check. `?` still works: it sends the partial line and a question mark,
with no newline, and clears the buffer afterwards.

**Everything is recorded, and there is no flag to turn it off.** Every
submitted line and every byte the device printed lands in
`bastion_chunks` with timestamps, so a session replays afterwards at the
speed it happened (Session recordings tab). Recordings are kept 365 days,
the same as the audit log, which is what they are. An operator sees their
own; an admin sees everyone's. Commands and refusals also go to
`audit_log` as `bastion.command` / `bastion.refused`.

**Knobs:**

| Env var | Default | What it does |
|---|---|---|
| `BASTION_ENABLED` | `1` | `0` turns the whole thing off; the routes 404 and the page says so |
| `BASTION_MAX_SESSIONS_PER_DEVICE` | `2` | not a preference - Dell OS9 has a handful of vty slots and the status poller already holds one |
| `BASTION_MAX_SESSIONS` | `8` | fleet-wide |
| `BASTION_IDLE_TIMEOUT_SECONDS` | `900` | an abandoned tab must not hold an SSH slot |
| `BASTION_MAX_SESSION_SECONDS` | `14400` | hard ceiling regardless of activity |
| `BASTION_MAX_RECORD_BYTES` | `8000000` | past this the transcript says where it was cut rather than becoming unloadable |
| `BASTION_ALLOWED_ORIGINS` | *(empty)* | extra origins for the WebSocket handshake; by default only the host the app is served from |

The terminal is a WebSocket, which needs a WebSocket implementation
bolted onto uvicorn - `websockets` is in `webui/requirements.txt` for
exactly that. Without it uvicorn logs "Unsupported upgrade request" and
answers the handshake with a 404.

## Data retention

Every growing table is pruned by `webui/retention.py`, once at startup and
then daily. Windows are per-table and env-tunable; **0 disables that
table's prune** ("keep forever", said honestly rather than by setting an
absurd number).

| Table | Env var | Default | Why |
|---|---|---|---|
| `metric_samples` (iface_*) | `RETAIN_IFACE_SAMPLES_DAYS` | 30 | ~94% of all samples - ~105 ports x 4 series, most ports unused |
| `metric_samples` (other) | `RETAIN_METRIC_SAMPLES_DAYS` | 180 | optics/PSU: low volume, high value over long periods |
| `events` | `RETAIN_EVENTS_DAYS` | 180 | resolved only - an open event is live state whatever its age |
| `results` (auto-saved) | `RETAIN_AUTOSAVED_RESULTS_DAYS` | 90 | explicitly saved results are **never** pruned |
| `command_history` | `RETAIN_COMMAND_HISTORY_DAYS` | 90 | per-user working list; `audit_log` keeps the durable record |
| `bastion_sessions` | `RETAIN_BASTION_DAYS` | 365 | console recordings *are* audit; cascades to `bastion_chunks` |
| `audit_log` | `RETAIN_AUDIT_LOG_DAYS` | 365 | longest by design - an audit trail that deletes itself is worth little |

Two rules the policies are built around, both about not destroying things
a person deliberately created:

1. **Deliberate keeps outlive automatic ones.** A result you clicked Save
   on is not the same as the auto-saved copy of every command ever run.
2. **Resolved only.** An open event is live state, whatever its age; only
   resolved events age out.

`metric_samples` is split because one class of series dominates it -
measured at 1.91M of 2.03M rows on a real 3-device fleet. A single window
would force a bad trade: short enough to control the interface series
throws away optic history that costs almost nothing and is exactly the
trend you want months of.

## Notes

- The switch's `admin` account lands in unprivileged EXEC (`>`); the
  exporter escalates with `enable` using the same password
  (`SWITCH_ENABLE_PASS` env var can override if it's ever set differently).
- Nothing in this stack issues a config-mode command *on its own*: the
  pollers, the Console and the scheduler only ever run commands written
  down in `webui/commands.py`. The one exception is a person at the
  Console bastion in full-access mode, where the whole point is that they
  are typing it themselves - and every keystroke of that is recorded.
- The temp password used to set this up was shared in plaintext in chat;
  worth rotating it on the switch once you're done validating the stack.

## Deploying on an LXC

### The quick way: `packaging/install-stack.sh`

An installer that does the native (no-Docker) deployment for you, one
module at a time or as a bundle:

```
sudo ./packaging/install-stack.sh              # interactive - detects, then asks
sudo ./packaging/install-stack.sh --detect     # what's installed here (no root needed)
sudo ./packaging/install-stack.sh --bundle app # webui + Prometheus on this host
sudo ./packaging/install-stack.sh --update     # after a git pull
```

Bundles are convenient groupings, nothing more: `app` is the webui and
Prometheus on one host, `monitoring` is Prometheus and the exporter,
`collector` the sFlow collector, `ingest` sFlow and syslog receivers.

| Bundle | Modules | For |
|---|---|---|
| `app` | webui + prometheus | The pairing that avoids needing a shared filesystem |
| `monitoring` | prometheus + exporter | The metrics side, no webui |
| `all` | everything | One-host install |

Individual modules: `webui`, `prometheus`,
`exporter`. It detects what's already present, only updates what's out of
date, prints per-module next steps when it finishes, and has `--dry-run`.

**Updates never touch your config.** Every module keeps its configuration
under `/etc`, deliberately outside the `/opt/<module>` directory an upgrade
replaces wholesale:

| Module | Config kept at | On update |
|---|---|---|
| webui | `/etc/switchboard/webui.env` | untouched |
| prometheus | `/etc/prometheus/prometheus.yml` | untouched |
| exporter | `/etc/s4048-exporter/exporter.env` | untouched |

Only the binaries, the Python app files, the frontend bundle and the
dashboard JSON are replaced. If an earlier install left a `prometheus.yml`
inside `/opt/prometheus`, it is migrated to `/etc/prometheus` before that
directory is removed rather than lost.
The walkthroughs below remain the reference for what it does and why -
read them if you want to understand or customise any step.

### The manual walkthroughs

Five full walkthroughs, depending on what you need:

- **[docs/deploy-lxc-docker.md](docs/deploy-lxc-docker.md)** — the whole
  stack (webui + exporter + Prometheus) via
  `docker compose`, in one LXC.
- **[docs/deploy-lxc-split.md](docs/deploy-lxc-split.md)** — the same five
  services, but each on its own LXC for maximum isolation - covers what
  breaks (compose's built-in service-name DNS) and how each piece is
  reconnected across real IPs instead.
- **[docs/deploy-lxc-4lxcs.md](docs/deploy-lxc-4lxcs.md)** — a middle
  ground: `webui` and `prometheus` share one LXC, the rest each get their
  own.
- **[docs/deploy-lxc-4lxcs-native.md](docs/deploy-lxc-4lxcs-native.md)** —
  the same 4-LXC split, but with **no Docker anywhere**: every service as
  a real systemd unit, built/installed directly on the host. Every command
  in it was actually run to confirm the versions/paths/CLI flags are real,
  not guessed from documentation.
- **[docs/deploy-lxc-exporter.md](docs/deploy-lxc-exporter.md)** — just the
  exporter, native, no Docker, as a systemd service (`packaging/`) - for
  when Prometheus already exists elsewhere. Point it at
  `<lxc-ip>:9101`.

Both install paths for the exporter-only route were smoke-tested against
the live switch during development: the venv path via the Docker image
(same `exporter.py`), and the standalone binary by running it directly with
real credentials and confirming `show processes cpu` / transceiver metrics
came back correctly.
