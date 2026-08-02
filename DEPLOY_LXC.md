# Deploying on an LXC

Two different paths, depending on what you actually need:

| Path | When to use | What you get |
|---|---|---|
| [Full stack via Docker](#part-1-full-stack-via-docker-in-an-lxc) | You want the whole monitoring stack - Switchboard, alerting, dashboards | webui + exporter + Prometheus + Alertmanager + Grafana, all in one LXC |
| [Exporter-only, native](#part-2-exporter-only-native-no-docker) | You already have Prometheus/Grafana elsewhere, or want something minimal next to the switch | Just the exporter, as a systemd service, no Docker |

Both assume a Debian 12 or Ubuntu 22.04+ LXC template. Everything here was
exercised against this repo's actual layout after the 2026-08-02 restructure
(shared `common/` module, per-user OIDC login, multi-device exporter) - not
written against an older shape of the codebase.

---

## Part 1: full stack via Docker in an LXC

### Prerequisites

- **Nesting.** Docker needs kernel features an unprivileged LXC doesn't
  expose by default. On Proxmox: `pct set <vmid> --features nesting=1,keyctl=1`
  (or Datacenter → *that container* → Options → Features → check
  **Nesting**), then reboot the container. If Docker still fails to start
  after that (some kernel/storage-driver combinations need more), the
  fallback is making the container privileged
  (`pct set <vmid> --unprivileged 0`) - simpler, less isolated, only do
  this if nesting alone doesn't work.
- **Resources.** 2 vCPU / 2GB RAM / 20GB disk minimum. Prometheus's TSDB and
  Grafana's own storage grow over time - the apps themselves are light, but
  metrics retention isn't.
- **Network reachability**, outbound from the LXC to: the switches being
  monitored (SSH, port 22), your Postgres host (port 5432 - Postgres itself
  is not bundled in this compose file, it's assumed to already exist
  somewhere reachable), Loki if you're using the Syslog page, and Keycloak
  if you're setting up login (see the OIDC note below).

### Steps

```bash
# On the LXC:
apt update && apt install -y docker.io docker-compose-plugin git
git clone <your-fork-or-repo-url> MONISTACK
cd MONISTACK
cp .env.example .env
nano .env   # fill in real values - see below
docker compose up -d --build
```

**What to actually put in `.env`:**

- `SWITCH_HOST`/`SWITCH_USER`/`SWITCH_PASS` - the static device
  `common/devices.yaml` already defines (the original S4048). Devices added
  later through the webui's Devices page don't need env vars at all - they
  live in Postgres.
- `DATABASE_URL` - a real Postgres DSN
  (`postgresql://user:pass@host:5432/switchboard`). Required for the webui
  to do anything beyond the first-run setup wizard, and for the exporter to
  see any device added through the UI (see `exporter.env.example`'s comment
  on this same variable for the native path).
- `GRAFANA_ADMIN_PASSWORD` - anything other than the default.
- `OIDC_ISSUER_URL`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`/`OIDC_REDIRECT_URI`
  - only if you want per-user login working (you do, for anything beyond a
    quick local test - there is no other way in once configured, no
    break-glass account). Needs an already-running Keycloak instance; see
    `webui/README.md`'s **"Login: OIDC against Keycloak"** section for the
    exact client/role setup this depends on. `OIDC_REDIRECT_URI` must be
    `http://<this LXC's real reachable address>:8080/api/auth/callback` -
    not `localhost`, since that has to resolve from *your browser*, not
    from inside the LXC.

**One thing specific to running this in an LXC rather than on your own
machine:** if you want Keycloak's **Back-Channel Logout** to work (a
session ending in Keycloak - an admin revoke, a logout somewhere else -
actually ending the session here too, not just on your own click), Keycloak's
*server* needs to be able to reach back into this LXC over the network.
That means the LXC needs a real routable hostname or IP registered as the
client's Backchannel logout URL in Keycloak, same requirement as
`OIDC_REDIRECT_URI` above but one direction further - see
`webui/README.md`'s "Back-Channel Logout" section for the full setup and
why this is a genuine network requirement, not a bug, if it doesn't fire.

### Verifying it's actually running

```bash
docker compose ps                              # all 5 services "Up"
curl http://localhost:8080/healthz              # webui
curl http://localhost:9101/metrics | head       # exporter - real metrics, not empty
curl http://localhost:9090/-/healthy             # Prometheus
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=s4048_up'
```

From a browser on the same network: `http://<lxc-ip>:8080` (Switchboard),
`:9090` (Prometheus), `:3000` (Grafana), `:9101/metrics` (raw exporter
output).

### Updating later

```bash
cd MONISTACK
git pull
docker compose up -d --build
```

Rebuilding is required (not just `up -d`) whenever code changes - these are
built images, not bind-mounted source.

---

## Part 2: exporter-only, native (no Docker)

For when you don't want a full Docker stack next to the switch - just the
metrics exporter, running as a systemd service, talking to a
Prometheus/Grafana that already exists elsewhere. This is what
`packaging/` is for; this section is the walkthrough tying its scripts
together, not a replacement for them.

Two install methods:

| | needs on the LXC | how it runs |
|---|---|---|
| `install.sh` (recommended) | Python 3 (already on Debian/Ubuntu templates) | venv under `/opt/s4048-exporter`, systemd service |
| `install-binary.sh` | nothing - single compiled executable | binary under `/opt/s4048-exporter`, systemd service |

Since the exporter now shares `db.py`/`store.py`/`devices.py`/
`ssh_client.py`/`metrics.py`/`junos_parsers.py`/`devices.yaml` with the
webui (`common/`), **both methods need a full repo checkout** - not just
the `exporter/` and `packaging/` directories on their own like before this
restructure.

### Method A - venv + systemd (`install.sh`)

On the LXC:

```bash
git clone <your-fork-or-repo-url> MONISTACK
cd MONISTACK/packaging
sudo ./install.sh
```

This installs to `/opt/s4048-exporter` (app + venv), writes a default
config to `/etc/s4048-exporter/exporter.env` (mode 600, owned by the
`s4048-exporter` service user - the script creates that user for you), and
registers the systemd unit, but does **not** start it yet - it needs real
credentials first:

```bash
sudo nano /etc/s4048-exporter/exporter.env
```

At minimum, set `SWITCH_HOST`/`SWITCH_USER`/`SWITCH_PASS` for the static
device already defined in `devices.yaml` (copied alongside the app by
`install.sh`). Optionally also set `DATABASE_URL` to the same Postgres the
webui uses, if you want this exporter to also poll devices added through
the webui's Devices page - see the comments already in
`exporter.env.example` for what each variable does, they were written for
exactly this file.

```bash
sudo systemctl restart s4048-exporter
journalctl -u s4048-exporter -f      # watch it connect and poll
curl http://localhost:9101/metrics   # confirm real metrics, not just "up"
```

Point your existing Prometheus at `<lxc-ip>:9101`.

### Method B - standalone binary (`install-binary.sh`)

No Python on the target at all. Build once, on a machine matching the
target LXC's OS/architecture (building inside a throwaway copy of the same
LXC template is the safest way to guarantee compatibility - the binary
bundles native crypto libraries that don't travel well across distros):

```bash
cd packaging
./build_binary.sh
```

This produces `./s4048-exporter` and, if this checkout also has
`common/devices.yaml`, copies it alongside the binary too (needed at
runtime, not bundled into the binary itself - PyInstaller resolves
`__file__` to a temp extraction directory inside a frozen binary, not
where the binary actually lives on disk, which `exporter.py` accounts for
explicitly so it looks next to the real binary instead).

Copy `s4048-exporter`, `install-binary.sh`, `exporter.env.example`, and
(if present) `devices.yaml` onto the target LXC, then:

```bash
sudo ./install-binary.sh
sudo nano /etc/s4048-exporter/exporter.env   # same as Method A
sudo systemctl restart s4048-exporter
```

### Either method: adding more devices later

- **Devices added through the webui** (if `DATABASE_URL` is set): picked
  up automatically within `REGISTRY_REFRESH_INTERVAL` (default 60s), no
  restart needed.
- **A second static device**: edit `devices.yaml` (either
  `/opt/s4048-exporter/app/devices.yaml` for Method A, or
  `/opt/s4048-exporter/devices.yaml` for Method B), add an entry following
  the existing S4048 one's shape, add the matching env vars to
  `exporter.env`, then `sudo systemctl restart s4048-exporter` (unlike the
  registry-refresh case above, editing this file needs a restart to be
  picked up - it's read once at process start, not on the refresh
  interval).

### Junos devices work here too

Both install methods carry `junos_parsers.py` and the exporter's Junos
polling logic - a Juniper device in `devices.yaml` or added via the webui
(with `DATABASE_URL` set) gets polled the same as any OS9 device, no extra
setup. OPNsense devices are listed but not yet polled (no parser exists for
that platform).

### Removing it

```bash
sudo packaging/uninstall.sh            # leaves /etc/s4048-exporter/exporter.env in place
sudo packaging/uninstall.sh --purge    # also deletes the config dir (holds the switch password) and the service user
```
