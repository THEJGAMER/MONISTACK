# S4048 SSH-polled monitoring stack

Monitors the Dell EMC S4048-ON (`192.168.4.106`) purely over SSH `show`
commands instead of SNMP. Stack: a custom Python exporter (Prometheus format)
+ Prometheus + Grafana, all run via Docker Compose.

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

## How it works

`exporter/ssh_client.py` opens one interactive SSH shell to the switch,
logs in, runs `enable` (same password), and disables paging. It then reuses
that session to run `show` commands on a loop, reconnecting automatically if
the session drops.

`exporter/exporter.py` polls two groups on that session:

- **Fast (every 30s, `FAST_POLL_INTERVAL`)**: `show processes cpu`,
  `show memory`, `show environment`, `show interfaces status`.
- **Slow (every 300s, `TRANSCEIVER_POLL_INTERVAL`)**: per-port
  `show interfaces <port> transceiver` optical diagnostics (temp, voltage,
  bias current, Tx/Rx power, alarm flags) for all 54 ports. This is
  sequential over one SSH session, so it's deliberately not run every cycle.

Metrics are served on `:9101/metrics` for Prometheus to scrape.

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

- `s4048_up` — 1 if the last SSH poll cycle succeeded
- `s4048_cpu_utilization_percent{core,window}`
- `s4048_memory_bytes{type}`
- `s4048_fan_status{unit,bay,fan}` / `s4048_fan_speed_rpm{...}`
- `s4048_psu_status{unit,bay}` / `s4048_psu_power_watts{unit,bay,kind}`
- `s4048_unit_temperature_celsius{unit}` / `s4048_sensor_temperature_celsius{sensor}`
- `s4048_interface_up{port,description}` / `s4048_interface_speed_mbps{port}`
- `s4048_transceiver_present{port}`, `_temperature_celsius`, `_voltage_volts`,
  `_tx_bias_ma`, `_tx_power_dbm`, `_rx_power_dbm`, `_alarm{port,flag}`

## Notes

- The switch's `admin` account lands in unprivileged EXEC (`>`); the
  exporter escalates with `enable` using the same password
  (`SWITCH_ENABLE_PASS` env var can override if it's ever set differently).
- The account only has read (`show`) commands run against it — nothing in
  this stack issues config-mode commands.
- The temp password used to set this up was shared in plaintext in chat;
  worth rotating it on the switch once you're done validating the stack.

## Installing on an LXC (no Docker)

`packaging/` has everything needed to run just the exporter natively as a
systemd service inside an LXC container — no Docker required. Point your
Prometheus (running wherever) at `<lxc-ip>:9101`.

Two install methods, pick one:

| | needs on the LXC | how it runs |
|---|---|---|
| `install.sh` (recommended) | Python 3 (already on Debian/Ubuntu LXC templates) | venv under `/opt/s4048-exporter`, systemd service |
| `install-binary.sh` | nothing — single compiled executable | binary under `/opt/s4048-exporter`, systemd service |

Both install to the same layout:
- App/binary: `/opt/s4048-exporter/`
- Config (credentials): `/etc/s4048-exporter/exporter.env` (mode 600, owned by the service user)
- systemd unit: `/etc/systemd/system/s4048-exporter.service`, running as a dedicated unprivileged `s4048-exporter` user

### Method A — venv + systemd (`install.sh`)

Copy this whole repo (or at least `exporter/` and `packaging/`) onto the LXC, then:

```
cd packaging
sudo ./install.sh
sudo nano /etc/s4048-exporter/exporter.env   # fill in SWITCH_HOST / SWITCH_USER / SWITCH_PASS
sudo systemctl restart s4048-exporter
```

### Method B — standalone binary (`install-binary.sh`)

No Python needed on the target at all. Build once (on a machine matching the
LXC's OS/arch — building it inside a throwaway copy of the same LXC template
is the safest way to guarantee compatibility, since the binary bundles
native crypto libraries):

```
cd packaging
./build_binary.sh          # produces ./s4048-exporter
```

Then copy `s4048-exporter`, `install-binary.sh`, and `exporter.env.example`
onto the target LXC and run:

```
sudo ./install-binary.sh
sudo nano /etc/s4048-exporter/exporter.env
sudo systemctl restart s4048-exporter
```

### Either way

```
journalctl -u s4048-exporter -f      # watch it poll
curl http://localhost:9101/metrics   # confirm metrics
```

To remove: `sudo packaging/uninstall.sh` (add `--purge` to also delete the
config dir — which holds the switch password — and the service user).

Both install paths were smoke-tested against the live switch during
development: the venv path via the Docker image (same `exporter.py`), and
the standalone binary by running it directly with real credentials and
confirming `show processes cpu` / transceiver metrics came back correctly.
