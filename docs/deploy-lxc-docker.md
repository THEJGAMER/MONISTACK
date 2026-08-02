# Deploying the full stack via Docker in an LXC

The whole monitoring stack - webui + exporter + Prometheus + Alertmanager +
Grafana - via `docker compose`, running inside one Debian 12 / Ubuntu
22.04+ LXC with Docker installed (e.g. a Proxmox nested-Docker LXC).

For a lighter option (just the exporter, no Docker, talking to a
Prometheus/Grafana you already have elsewhere), see
[deploy-lxc-exporter.md](deploy-lxc-exporter.md) instead. For maximum
isolation (each service on its own LXC instead of sharing one), see
[deploy-lxc-split.md](deploy-lxc-split.md), or
[deploy-lxc-4lxcs.md](deploy-lxc-4lxcs.md) for a middle ground (`webui` and
`prometheus` share one LXC, the rest each get their own).

Exercised against this repo's actual layout after the 2026-08-02
restructure (shared `common/` module, per-user OIDC login, multi-device
exporter) - not written against an older shape of the codebase.

## Prerequisites

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

## Steps

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
  see any device added through the UI.
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

## Verifying it's actually running

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

## Updating later

```bash
cd MONISTACK
git pull
docker compose up -d --build
```

Rebuilding is required (not just `up -d`) whenever code changes - these are
built images, not bind-mounted source.
