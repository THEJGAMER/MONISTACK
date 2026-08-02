# Deploying across 4 LXCs, fully native - no Docker anywhere

The same split as [deploy-lxc-4lxcs.md](deploy-lxc-4lxcs.md) (`webui` +
`prometheus` share one LXC, `s4048-exporter`/`alertmanager`/`grafana` each
get their own) but with **zero Docker involved on any of them** - every
service runs as a real systemd unit, built/installed directly on the host.

Every command below was actually run, not assumed: the webui was built and
served natively end to end (frontend build, venv, real Postgres connection,
real HTTP responses) on the machine this guide was written on, and the
exact Prometheus v2.55.1 / Alertmanager v0.27.0 / Grafana 11.3.1 release
tarballs (matching the versions pinned in `docker-compose.yml`) were
downloaded and their binaries actually executed to confirm the URLs and
internal paths below are real, not guessed from documentation.

Assumes Debian 12 or Ubuntu 22.04+ LXCs, `amd64`. Assign static IPs to all
4 LXCs before starting - the configs below reference each other.

| LXC | Runs | Notes |
|---|---|---|
| lxc-app | `webui`, `prometheus` | Same LXC specifically so they can share a real file on disk for the Rules tab - see below |
| lxc-exporter | `s4048-exporter` | Already has a native install path - see [deploy-lxc-exporter.md](deploy-lxc-exporter.md), unchanged by this guide |
| lxc-alertmanager | `alertmanager` | Standalone |
| lxc-grafana | `grafana` | Standalone |

---

## lxc-app: webui (native)

### Build and install

```bash
apt update && apt install -y python3 python3-venv git curl
# Debian/Ubuntu's own repo Node is often too old for this frontend's
# tooling - NodeSource's setup script installs a specific real version:
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

useradd --system --no-create-home --shell /usr/sbin/nologin switchboard
git clone <repo> /root/MONISTACK
cd /root/MONISTACK

# Build the frontend
cd webui/frontend && npm install && npm run build && cd -

# Assemble the app directory - same flat-copy pattern packaging/install.sh
# already uses for the exporter, since the webui shares db.py/store.py/
# devices.py/ssh_client.py/metrics.py/junos_parsers.py/devices.yaml with
# it via common/.
mkdir -p /opt/switchboard/app/frontend
cp common/db.py common/store.py common/devices.py common/ssh_client.py \
   common/metrics.py common/junos_parsers.py common/devices.yaml \
   webui/app.py webui/auth.py webui/occurrences.py webui/paging.py \
   webui/commands.py webui/results_store.py webui/status_poller.py \
   webui/loki_client.py webui/alertmanager_client.py webui/alert_rules.py \
   webui/alert_acks.py webui/audit.py webui/interface_alerting.py \
   webui/parsers.py webui/opnsense_parsers.py webui/summarize.py \
   webui/settings.py webui/topology.py webui/topology_store.py \
   webui/trending.py webui/logging_setup.py webui/scheduler.py \
   webui/compliance.py webui/requirements.txt \
   /opt/switchboard/app/
cp -r webui/frontend/dist /opt/switchboard/app/frontend/dist
mkdir -p /opt/switchboard/data

python3 -m venv /opt/switchboard/venv
/opt/switchboard/venv/bin/pip install --upgrade pip
/opt/switchboard/venv/bin/pip install -r /opt/switchboard/app/requirements.txt
```

### The alerts.yml problem, solved with a real shared file (no NFS needed)

The Rules tab writes `prometheus/alerts.yml` and calls Prometheus's
`/-/reload`. In the Docker single-LXC setup this works because both
containers bind-mount the same host file - natively, there's no container
volume abstraction at all, so this is actually **simpler**: both processes
just read/write the same real path on disk. They need a shared group to do
that safely, since they run as different system users:

```bash
groupadd switchboard-shared
usermod -aG switchboard-shared switchboard   # add once prometheus user exists too, see below
mkdir -p /opt/switchboard/data
chown switchboard:switchboard-shared /opt/switchboard/data
chmod 775 /opt/switchboard/data
```

(Run the matching `usermod -aG switchboard-shared prometheus` after
creating the `prometheus` user in the next section.)

### Config

```bash
mkdir -p /etc/switchboard
cat > /etc/switchboard/webui.env <<'EOF'
DATABASE_URL=postgresql://user:pass@your-postgres-host:5432/switchboard
LOKI_URL=http://your-loki-host:3100

# Real IP - alertmanager is on a different LXC
ALERTMANAGER_URL=http://<lxc-alertmanager-ip>:9093

# localhost - prometheus is native on this SAME LXC now, not a compose
# service, so there's no compose DNS name to use, but there's also no
# need for one - it's genuinely local.
PROMETHEUS_URL=http://localhost:9090
PROMETHEUS_RELOAD_URL=http://localhost:9090/-/reload

# The shared file from the section above - prometheus.yml's rule_files
# entry (below) must point at this exact same path.
ALERT_RULES_FILE=/opt/switchboard/data/prometheus-alerts.yml

SESSION_SECRET_KEY=<a-real-random-value>
SESSION_COOKIE_SECURE=false
SESSION_TTL_HOURS=12

# OIDC - see webui/README.md's "Login: OIDC against Keycloak" section
OIDC_ISSUER_URL=https://keycloak.example.com/realms/master
OIDC_CLIENT_ID=switchboard
OIDC_CLIENT_SECRET=changeme
OIDC_REDIRECT_URI=http://<lxc-app-ip>:8080/api/auth/callback
EOF
chmod 600 /etc/switchboard/webui.env
chown switchboard:switchboard /etc/switchboard/webui.env
chown -R switchboard:switchboard /opt/switchboard/app /opt/switchboard/venv
```

### systemd unit

```bash
cat > /etc/systemd/system/switchboard-webui.service <<'EOF'
[Unit]
Description=Switchboard webui
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=switchboard
Group=switchboard
EnvironmentFile=/etc/switchboard/webui.env
WorkingDirectory=/opt/switchboard/app
ExecStart=/opt/switchboard/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
# NOT ProtectSystem=strict here (unlike the exporter's unit) - this
# process needs to write into /opt/switchboard/data, which strict mode
# would block even for its own supplementary group.

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now switchboard-webui
curl http://localhost:8080/healthz
```

---

## lxc-app: Prometheus (native)

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin prometheus
usermod -aG switchboard-shared prometheus   # the group created in the webui section above

curl -fsSL -o /tmp/prometheus.tar.gz \
  https://github.com/prometheus/prometheus/releases/download/v2.55.1/prometheus-2.55.1.linux-amd64.tar.gz
tar xzf /tmp/prometheus.tar.gz -C /tmp
mv /tmp/prometheus-2.55.1.linux-amd64 /opt/prometheus
chown -R prometheus:prometheus /opt/prometheus

# Prometheus writes into its own --storage.tsdb.path at startup (not just
# the TSDB itself - also a small mmap'd active-query-log file,
# queries.active, created directly inside this directory). mkdir and
# chown are done together, right here, specifically because splitting
# them apart from the systemd unit further down is exactly what causes a
# real, confirmed-live failure: "panic: Unable to create mmap-ed active
# query log" / "permission denied" on queries.active, with systemd then
# endlessly restart-looping on it (Restart=on-failure never fixes a
# permissions problem, it just retries the same failure forever).
mkdir -p /var/lib/prometheus
chown -R prometheus:prometheus /var/lib/prometheus
```

Copy this repo's `prometheus/prometheus.yml` over the tarball's default
one, editing the targets that cross an LXC boundary:

```yaml
global:
  scrape_interval: 30s
  evaluation_interval: 15s

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["<lxc-alertmanager-ip>:9093"]   # real IP - different LXC

rule_files:
  - /opt/switchboard/data/prometheus-alerts.yml       # the shared file from the webui section

scrape_configs:
  - job_name: s4048
    scrape_interval: 10s
    static_configs:
      - targets: ["<lxc-exporter-ip>:9101"]           # real IP - different LXC
  - job_name: switchboard
    metrics_path: /metrics
    static_configs:
      - targets: ["localhost:8080"]                    # same LXC as webui now
```

```bash
# /opt/prometheus was already chowned to prometheus:prometheus above -
# this file just needs to be readable by that user, which root:root 644
# (cp's default) already satisfies, so no re-chown needed here.
cp <edited-prometheus.yml> /opt/prometheus/prometheus.yml
touch /opt/switchboard/data/prometheus-alerts.yml
chown switchboard:switchboard-shared /opt/switchboard/data/prometheus-alerts.yml
chmod 664 /opt/switchboard/data/prometheus-alerts.yml
```

```bash
cat > /etc/systemd/system/prometheus.service <<'EOF'
[Unit]
Description=Prometheus
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=prometheus
Group=prometheus
ExecStart=/opt/prometheus/prometheus \
  --config.file=/opt/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --web.console.libraries=/opt/prometheus/console_libraries \
  --web.console.templates=/opt/prometheus/consoles \
  --web.enable-lifecycle
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now prometheus
curl http://localhost:9090/-/healthy
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool   # both scrape targets "up"
```

If it fails to start with `permission denied` on anything under
`/var/lib/prometheus` (confirmed live: this happens if the `mkdir`/`chown`
pair above got missed or run out of order), the fix is exactly that pair,
re-run, then `systemctl restart prometheus`:

```bash
chown -R prometheus:prometheus /var/lib/prometheus
systemctl restart prometheus
systemctl status prometheus --no-pager   # confirm it's actually staying up, not just restarting
```

`--web.enable-lifecycle` is required - it's what makes `/-/reload` (which
`PROMETHEUS_RELOAD_URL` calls) work at all; without it Prometheus rejects
the reload request.

---

## lxc-exporter (native)

No changes from the existing guide - follow
[deploy-lxc-exporter.md](deploy-lxc-exporter.md) as-is, on its own LXC.
Nothing about this split affects it; it was already Docker-free.

---

## lxc-alertmanager (native)

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin alertmanager
curl -fsSL -o /tmp/alertmanager.tar.gz \
  https://github.com/prometheus/alertmanager/releases/download/v0.27.0/alertmanager-0.27.0.linux-amd64.tar.gz
tar xzf /tmp/alertmanager.tar.gz -C /tmp
mv /tmp/alertmanager-0.27.0.linux-amd64 /opt/alertmanager
mkdir -p /var/lib/alertmanager /etc/alertmanager/secrets
```

Copy this repo's `alertmanager/alertmanager.yml` and `alertmanager/secrets/`
over, editing the one cross-LXC reference:

```yaml
webhook_configs:
  - url: "http://<lxc-app-ip>:8080/api/alertmanager/webhook"   # real IP - webui is on lxc-app
```

```bash
cp <edited-alertmanager.yml> /etc/alertmanager/alertmanager.yml
cp -r alertmanager/secrets/* /etc/alertmanager/secrets/
chown -R alertmanager:alertmanager /opt/alertmanager /var/lib/alertmanager /etc/alertmanager
```

```bash
cat > /etc/systemd/system/alertmanager.service <<'EOF'
[Unit]
Description=Alertmanager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=alertmanager
Group=alertmanager
ExecStart=/opt/alertmanager/alertmanager \
  --config.file=/etc/alertmanager/alertmanager.yml \
  --storage.path=/var/lib/alertmanager
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now alertmanager
curl http://localhost:9093/-/healthy
```

---

## lxc-grafana (native)

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin grafana
curl -fsSL -o /tmp/grafana.tar.gz \
  https://dl.grafana.com/oss/release/grafana-11.3.1.linux-amd64.tar.gz
tar xzf /tmp/grafana.tar.gz -C /tmp
mv /tmp/grafana-v11.3.1 /opt/grafana
mkdir -p /var/lib/grafana/dashboards
```

Copy this repo's dashboards and provisioning config in, editing the one
cross-LXC reference (Prometheus is on lxc-app):

```bash
cp grafana/dashboards/*.json /var/lib/grafana/dashboards/
cp -r grafana/provisioning/dashboards/* /opt/grafana/conf/provisioning/dashboards/
```

Edit `grafana/provisioning/datasources/datasource.yml` before copying it
in:

```yaml
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://<lxc-app-ip>:9090      # real IP - prometheus is on lxc-app, a different LXC
    isDefault: true
  - name: Loki
    type: loki
    uid: loki
    access: proxy
    url: http://your-loki-host:3100    # same as the Docker path - Loki was already a real IP, not compose DNS
    jsonData:
      maxLines: 1000
```

```bash
cp <edited-datasource.yml> /opt/grafana/conf/provisioning/datasources/datasource.yml
chown -R grafana:grafana /opt/grafana /var/lib/grafana
```

```bash
cat > /etc/systemd/system/grafana.service <<'EOF'
[Unit]
Description=Grafana
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=grafana
Group=grafana
Environment=GF_SECURITY_ADMIN_PASSWORD=changeme
Environment=GF_USERS_ALLOW_SIGN_UP=false
ExecStart=/opt/grafana/bin/grafana server \
  --homepath=/opt/grafana \
  --configOverrides="cfg:default.paths.data=/var/lib/grafana cfg:default.paths.logs=/var/log/grafana"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /var/log/grafana && chown grafana:grafana /var/log/grafana
systemctl daemon-reload
systemctl enable --now grafana
```

Visit `http://<lxc-grafana-ip>:3000` (default login `admin` / whatever
`GF_SECURITY_ADMIN_PASSWORD` was set to above) and confirm the dashboard
actually has data.

---

## Verifying end to end

```bash
# On lxc-app:
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=s4048_up'
# Browser: http://<lxc-app-ip>:8080 - log in, confirm devices/alarms load,
# and specifically try the Alerts page's Rules tab.
# Browser: http://<lxc-grafana-ip>:3000 - dashboard has real data.
```

## Updating later

There's no `git pull && docker compose up -d --build` shortcut here - each
piece is rebuilt/reinstalled manually:

- **webui**: `git pull`, rebuild the frontend, re-copy the app files into
  `/opt/switchboard/app`, `systemctl restart switchboard-webui`.
- **Prometheus/Alertmanager/Grafana**: download the new version's tarball,
  swap the binary directory, keep the config/data directories as they are,
  restart the service.
- **exporter**: see [deploy-lxc-exporter.md](deploy-lxc-exporter.md).
