# Deploying just the exporter, native, on an LXC (no Docker)

For when you don't want a full Docker stack next to the switch - just the
metrics exporter, running as a systemd service, talking to a
Prometheus/Grafana that already exists elsewhere. This is what
`packaging/` is for; this doc is the walkthrough tying its scripts
together, not a replacement for them.

For the full monitoring stack (webui + exporter + Prometheus + Alertmanager
+ Grafana together, via Docker), see
[deploy-lxc-docker.md](deploy-lxc-docker.md) instead.

Assumes a Debian 12 or Ubuntu 22.04+ LXC template. Exercised against this
repo's actual layout after the 2026-08-02 restructure (shared `common/`
module, multi-device exporter, Junos support) - not written against an
older shape of the codebase.

## Two install methods

| | needs on the LXC | how it runs |
|---|---|---|
| `install.sh` (recommended) | Python 3 (already on Debian/Ubuntu templates) | venv under `/opt/s4048-exporter`, systemd service |
| `install-binary.sh` | nothing - single compiled executable | binary under `/opt/s4048-exporter`, systemd service |

Since the exporter now shares `db.py`/`store.py`/`devices.py`/
`ssh_client.py`/`metrics.py`/`junos_parsers.py`/`devices.yaml` with the
webui (`common/`), **both methods need a full repo checkout** - not just
the `exporter/` and `packaging/` directories on their own like before this
restructure.

Every command block below `cd`s to an absolute path (`~/MONISTACK/...`)
rather than a relative one - safe to copy-paste in sequence from one
persistent shell, or to run out of order, without needing to track which
directory the previous block left you in.

## Method A - venv + systemd (`install.sh`)

On the LXC:

```bash
cd ~ && git clone <your-fork-or-repo-url> MONISTACK
cd ~/MONISTACK/packaging
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

## Method B - standalone binary (`install-binary.sh`)

No Python on the target at all. Build once, on a machine matching the
target LXC's OS/architecture (building inside a throwaway copy of the same
LXC template is the safest way to guarantee compatibility - the binary
bundles native crypto libraries that don't travel well across distros):

```bash
cd ~/MONISTACK/packaging
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

## Either method: adding more devices later

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

## Junos devices work here too

Both install methods carry `junos_parsers.py` and the exporter's Junos
polling logic - a Juniper device in `devices.yaml` or added via the webui
(with `DATABASE_URL` set) gets polled the same as any OS9 device, no extra
setup. OPNsense devices are listed but not yet polled (no parser exists for
that platform).

## Removing it

```bash
sudo ~/MONISTACK/packaging/uninstall.sh            # leaves /etc/s4048-exporter/exporter.env in place
sudo ~/MONISTACK/packaging/uninstall.sh --purge    # also deletes the config dir (holds the switch password) and the service user
```
