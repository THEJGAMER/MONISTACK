# Deploying the stack across 5 separate LXCs (one per service)

Maximum isolation: `webui`, `s4048-exporter`, `prometheus`, `alertmanager`,
and `grafana` each get their own LXC instead of sharing one via
`docker compose`. Each LXC still runs its service via Docker (a full repo
checkout + `docker compose up -d --build <service-name>`, just starting
one service instead of all five) - this reuses the existing, already-correct
Dockerfiles and volume definitions instead of hand-writing raw `docker run`
commands per service.

For the simpler single-LXC path, see
[deploy-lxc-docker.md](deploy-lxc-docker.md), or
[deploy-lxc-4lxcs.md](deploy-lxc-4lxcs.md) for a middle ground that avoids
this guide's one real gotcha (see below) by keeping `webui` and
`prometheus` together. For just the exporter with no
Docker at all, see [deploy-lxc-exporter.md](deploy-lxc-exporter.md).

## The real cost of splitting: no more compose service-name DNS

Docker Compose gives every service in `docker-compose.yml` a free DNS name
on its private bridge network - `alertmanager:9093`, `prometheus:9090`,
`s4048-exporter:9101`, `webui:8080` all resolve automatically *only when
they're containers in the same compose project on the same host*. Split
across 5 LXCs, none of that exists anymore - every one of those references
has to become a real, reachable IP (or hostname you've set up DNS for).

**Assign static IPs to all 5 LXCs before starting** - every config below
needs to reference the others, and editing five interdependent configs
after the fact (once each LXC's IP is known) is more error-prone than
having them upfront.

| LXC | Service | Port | Referenced by |
|---|---|---|---|
| lxc-webui | `webui` | 8080 | Prometheus (scrape), Alertmanager (webhook) |
| lxc-exporter | `s4048-exporter` | 9101 | Prometheus (scrape) |
| lxc-prometheus | `prometheus` | 9090 | webui (query/reload), Grafana (datasource), Alertmanager (n/a - Alertmanager doesn't call Prometheus) |
| lxc-alertmanager | `alertmanager` | 9093 | webui (query/silences), Prometheus (alerting target) |
| lxc-grafana | `grafana` | 3000 | (nothing calls into Grafana) |

Every LXC below: 1 vCPU / 1GB RAM is enough per-service (unlike the
single-LXC path, nothing here is sharing resources with 4 other
containers); `apt install -y docker.io docker-compose-plugin git` and a
full `git clone <repo> MONISTACK` - each LXC needs the whole repo checked
out even though it only runs one service, since `docker-compose.yml` and
the Dockerfiles for the shared `common/` module live at the repo root.

## The one real gap: the Rules tab's alerts.yml

Everything above is just "point env vars/config files at real IPs instead
of compose DNS names" - mechanical. There's one genuine architectural
problem this split creates, not just a config edit:

The webui's Rules tab (Alerts page) regenerates
`prometheus/alerts.yml` and calls Prometheus's `/-/reload` to pick it up
live. In the single-LXC compose setup this works because **both containers
bind-mount the same host file** - `webui`'s
`./prometheus/alerts.yml:/app/data/prometheus-alerts.yml` and
`prometheus`'s `./prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro` are
the same file on the same disk. Reload alone (an HTTP call) works fine
across LXCs - `PROMETHEUS_RELOAD_URL` just needs to point at the real
Prometheus LXC. Getting the *file content* there does not have an
HTTP-based solution here; nothing in this codebase pushes the file over
the network.

**Fix: NFS-share the `prometheus/` directory from lxc-prometheus to
lxc-webui**, so the *same* bind-mount lines already in `docker-compose.yml`
keep working unmodified - webui writes into what looks like a local
directory, which is actually the real file on lxc-prometheus.

```bash
# On lxc-prometheus:
apt install -y nfs-kernel-server
mkdir -p ~/MONISTACK/prometheus   # if not already there from git clone
echo "$HOME/MONISTACK/prometheus  <lxc-webui-ip>(rw,sync,no_subtree_check)" >> /etc/exports
exportfs -ra
systemctl restart nfs-kernel-server

# On lxc-webui:
apt install -y nfs-common
mv ~/MONISTACK/prometheus ~/MONISTACK/prometheus.orig   # keep prometheus.yml etc as backup, not needed here
mkdir -p ~/MONISTACK/prometheus
mount <lxc-prometheus-ip>:/root/MONISTACK/prometheus ~/MONISTACK/prometheus
# make it persistent:
echo "<lxc-prometheus-ip>:/root/MONISTACK/prometheus  /root/MONISTACK/prometheus  nfs  defaults  0  0" >> /etc/fstab
```

(Adjust paths for whatever user/home directory you actually cloned the repo
into - the above assumes root's home for brevity.)

If you don't want to run NFS at all, the alternative is not deploying
`webui` and `prometheus` on separate LXCs specifically - keep those two
together and only separate the others, since this specific coupling is the
one part of the stack that isn't a clean network boundary today. See
[deploy-lxc-4lxcs.md](deploy-lxc-4lxcs.md) for exactly that split, worked
through in full.

## Per-LXC setup

### lxc-exporter

```bash
cp .env.example .env
nano .env   # SWITCH_HOST/USER/PASS, DATABASE_URL if pulling in UI-added devices
docker compose up -d --build s4048-exporter
curl http://localhost:9101/metrics | head   # confirm real metrics
```

No cross-service config needed here - the exporter doesn't call anything
else, it's only ever scraped.

### lxc-webui

```bash
cp .env.example .env
nano .env
```

Set, beyond the usual `DATABASE_URL`/OIDC vars (see
[deploy-lxc-docker.md](deploy-lxc-docker.md) for what those mean):

```bash
ALERTMANAGER_URL=http://<lxc-alertmanager-ip>:9093
PROMETHEUS_URL=http://<lxc-prometheus-ip>:9090
PROMETHEUS_RELOAD_URL=http://<lxc-prometheus-ip>:9090/-/reload
```

Then set up the NFS mount from the section above before starting, since
`docker-compose.yml`'s bind mount needs `./prometheus/alerts.yml` to
already resolve to the shared file:

```bash
docker compose up -d --build webui
curl http://localhost:8080/healthz
```

### lxc-prometheus

```bash
cp .env.example .env   # only needed if you use any ${VAR} substitution in prometheus.yml - none by default
```

Edit `prometheus/prometheus.yml`, replacing the compose-DNS targets with
real IPs:

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["<lxc-alertmanager-ip>:9093"]

scrape_configs:
  - job_name: s4048
    scrape_interval: 10s
    static_configs:
      - targets: ["<lxc-exporter-ip>:9101"]
  - job_name: switchboard
    metrics_path: /metrics
    static_configs:
      - targets: ["<lxc-webui-ip>:8080"]
```

Set up the NFS export from the section above (this is the export side, do
it before starting webui). Then:

```bash
docker compose up -d --build prometheus
curl http://localhost:9090/-/healthy
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool   # confirm both scrape targets are "up", not "down"/unreachable
```

### lxc-alertmanager

Edit `alertmanager/alertmanager.yml`'s webhook receiver:

```yaml
webhook_configs:
  - url: "http://<lxc-webui-ip>:8080/api/alertmanager/webhook"
```

Keep `alertmanager/secrets/` (PagerDuty integration keys, etc.) populated
the same as the single-LXC path - nothing about secrets changes with this
split.

```bash
docker compose up -d --build alertmanager
curl http://localhost:9093/-/healthy
```

### lxc-grafana

Edit `grafana/provisioning/datasources/datasource.yml`:

```yaml
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://<lxc-prometheus-ip>:9090
    isDefault: true
```

(The Loki datasource in the same file already points at a real IP, not a
compose DNS name - untouched.)

```bash
cp .env.example .env
nano .env   # GRAFANA_ADMIN_PASSWORD
docker compose up -d --build grafana
```

Visit `http://<lxc-grafana-ip>:3000` - confirm the dashboard actually shows
data, not just that Grafana itself loaded (a wrong Prometheus URL still
lets Grafana boot, it just shows empty panels).

## Verifying the whole thing end to end

```bash
# From lxc-prometheus:
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=s4048_up'
# From a browser: http://<lxc-webui-ip>:8080 - log in, confirm devices/alarms load
# From a browser: http://<lxc-grafana-ip>:3000 - confirm the S4048 dashboard has real data
```

If Prometheus shows a scrape target as `down`, it's almost always one of:
a firewall between the two LXCs, a typo'd IP in `prometheus.yml`, or (for
the `webui`/`s4048-exporter` targets specifically) the target service not
actually running yet on the other LXC.

## Updating later

Same as the single-LXC path, but once per LXC:

```bash
cd MONISTACK
git pull
docker compose up -d --build <service-name>
```
