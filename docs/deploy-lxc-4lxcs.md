# Deploying across 4 LXCs: webui + Prometheus together, the rest separate

A middle ground between [deploy-lxc-docker.md](deploy-lxc-docker.md) (one
LXC, everything) and [deploy-lxc-split.md](deploy-lxc-split.md) (five LXCs,
one per service) - `webui` and `prometheus` share one LXC, `s4048-exporter`,
`alertmanager`, and `grafana` each get their own.

**Why keep these two together specifically:** the webui's Rules tab
regenerates `prometheus/alerts.yml` and calls Prometheus's `/-/reload` to
pick it up live. That relies on both containers bind-mounting the *same
host file* (see `docker-compose.yml`'s `webui`/`prometheus` volume lines) -
which only works when they're on the same filesystem. The 5-LXC split
solves this with an NFS share; this guide sidesteps needing NFS at all by
just not splitting the one pair that's actually coupled at the filesystem
level. Everything else in the stack talks over plain HTTP and splits
cleanly.

**The other benefit:** since `webui` and `prometheus` are started from the
*same* `docker-compose.yml` on the *same* host, Docker Compose's built-in
service-name DNS (`webui:8080`, `prometheus:9090`) still works **between
those two** without any changes - only the connections crossing an actual
LXC boundary (to the exporter, Alertmanager, and Grafana LXCs) need real
IPs instead of compose DNS names. This guide is close to
[deploy-lxc-docker.md](deploy-lxc-docker.md) for the webui+prometheus LXC,
and close to [deploy-lxc-split.md](deploy-lxc-split.md) for the other
three.

| LXC | Services | Notes |
|---|---|---|
| lxc-app | `webui`, `prometheus` | Same compose project, same host - `webui:8080`/`prometheus:9090` DNS still works between them |
| lxc-exporter | `s4048-exporter` | Standalone |
| lxc-alertmanager | `alertmanager` | Standalone |
| lxc-grafana | `grafana` | Standalone |

As with the other split guides: assign static IPs to all 4 LXCs before
starting, since the configs below reference each other.

## lxc-app (webui + prometheus)

```bash
apt install -y docker.io docker-compose-plugin git
git clone <repo> MONISTACK && cd MONISTACK
cp .env.example .env
nano .env
```

Set:

```bash
# Real IP - alertmanager is NOT on this LXC
ALERTMANAGER_URL=http://<lxc-alertmanager-ip>:9093

# Left as compose DNS names - prometheus IS on this same LXC/compose project
PROMETHEUS_URL=http://prometheus:9090
PROMETHEUS_RELOAD_URL=http://prometheus:9090/-/reload
```

(`PROMETHEUS_URL`/`PROMETHEUS_RELOAD_URL` are already this value by default
in `webui/app.py` - only listed here so it's clear you should *leave them
alone*, not because they need editing.)

Edit `prometheus/prometheus.yml`:

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["<lxc-alertmanager-ip>:9093"]   # real IP - different LXC

scrape_configs:
  - job_name: s4048
    scrape_interval: 10s
    static_configs:
      - targets: ["<lxc-exporter-ip>:9101"]          # real IP - different LXC
  - job_name: switchboard
    metrics_path: /metrics
    static_configs:
      - targets: ["webui:8080"]                       # unchanged - same LXC
```

`rule_files: - /etc/prometheus/alerts.yml` and the `webui`/`prometheus`
bind mounts in `docker-compose.yml` need **no changes at all** - same
filesystem, same as the single-LXC setup.

```bash
docker compose up -d --build webui prometheus
curl http://localhost:8080/healthz
curl http://localhost:9090/-/healthy
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool   # both scrape targets "up"
```

## lxc-exporter

Identical to the 5-LXC split - see
[deploy-lxc-split.md's exporter section](deploy-lxc-split.md#lxc-exporter),
or the native no-Docker path in
[deploy-lxc-exporter.md](deploy-lxc-exporter.md).

```bash
cp .env.example .env
nano .env   # SWITCH_HOST/USER/PASS, DATABASE_URL if pulling in UI-added devices
docker compose up -d --build s4048-exporter
curl http://localhost:9101/metrics | head
```

## lxc-alertmanager

Edit `alertmanager/alertmanager.yml`'s webhook receiver - webui is on a
different LXC now, so this needs a real IP:

```yaml
webhook_configs:
  - url: "http://<lxc-app-ip>:8080/api/alertmanager/webhook"
```

Keep `alertmanager/secrets/` populated the same as any other path.

```bash
docker compose up -d --build alertmanager
curl http://localhost:9093/-/healthy
```

## lxc-grafana

Edit `grafana/provisioning/datasources/datasource.yml` - Prometheus is on
lxc-app, a different LXC:

```yaml
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://<lxc-app-ip>:9090
    isDefault: true
```

```bash
cp .env.example .env
nano .env   # GRAFANA_ADMIN_PASSWORD
docker compose up -d --build grafana
```

Visit `http://<lxc-grafana-ip>:3000` and confirm the dashboard actually has
data, not just that Grafana loaded.

## Verifying end to end

```bash
# On lxc-app:
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=s4048_up'
# Browser: http://<lxc-app-ip>:8080 - log in, confirm devices/alarms load,
# and specifically try the Alerts page's Rules tab - this is the one thing
# this split was designed to keep working without extra plumbing.
# Browser: http://<lxc-grafana-ip>:3000 - dashboard has real data.
```

## Updating later

Same as the other split guides, once per LXC - for lxc-app, rebuild both
services together:

```bash
cd MONISTACK
git pull
docker compose up -d --build webui prometheus   # on lxc-app
docker compose up -d --build <service-name>     # on each of the other three
```
