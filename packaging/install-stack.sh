#!/usr/bin/env bash
# Switchboard stack installer - native systemd, no Docker anywhere.
#
# Installs any combination of the five services (webui, prometheus,
# alertmanager, grafana, exporter) onto this host, either individually or
# as a pre-set bundle. Detects what is already installed, and can update
# in place rather than reinstalling.
#
# The bundles exist for a specific practical reason, not just convenience.
# The webui's Rules tab writes Prometheus's alert-rules file and then calls
# Prometheus's /-/reload, so those two processes must see the *same file*.
# Split across machines that needs a network filesystem (NFS/CIFS/SMB) with
# all the failure modes that brings; co-located on one host it is just a
# real path on disk shared through a Unix group. The `app` bundle is
# exactly that pairing. See docs/deploy-lxc-4lxcs-native.md.
#
# Every version, URL and path here comes from that guide, where they were
# actually downloaded and executed rather than taken from documentation.
set -euo pipefail

VERSION="1.0.0"

# Pinned to match docker-compose.yml, so a native install and a Docker one
# are the same software. Bump both together.
PROMETHEUS_VERSION="2.55.1"
ALERTMANAGER_VERSION="0.27.0"
GRAFANA_VERSION="11.3.1"
NODE_MAJOR="20"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SHARED_GROUP="switchboard-shared"
SB_HOME=/opt/switchboard
SB_DATA="$SB_HOME/data"
SB_CONF=/etc/switchboard
ALERT_RULES_FILE="$SB_DATA/prometheus-alerts.yml"

ASSUME_YES=0
DRY_RUN=0
declare -a SELECTED=()

# ---------------------------------------------------------------- output

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[36m'
else
  C_RESET=; C_BOLD=; C_DIM=; C_RED=; C_GREEN=; C_YELLOW=; C_BLUE=
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$C_BLUE$C_BOLD" "$C_RESET" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
die()  { printf '%sERROR:%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; exit 1; }

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  %s[dry-run]%s %s\n' "$C_DIM" "$C_RESET" "$*"
  else
    "$@"
  fi
}

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local reply
  read -r -p "$1 [y/N] " reply </dev/tty || reply=n
  [[ "$reply" =~ ^[Yy] ]]
}

ask() {
  # ask <prompt> <default> -> echoes the answer
  local prompt="$1" default="${2:-}" reply
  if [[ $ASSUME_YES -eq 1 ]]; then printf '%s' "$default"; return; fi
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " reply </dev/tty || reply=""
  else
    read -r -p "$prompt: " reply </dev/tty || reply=""
  fi
  printf '%s' "${reply:-$default}"
}

# ---------------------------------------------------------------- modules

ALL_MODULES=(webui prometheus alertmanager grafana exporter)

module_desc() {
  case "$1" in
    webui)        echo "Switchboard web UI (FastAPI + systemd, port 8080)";;
    prometheus)   echo "Prometheus v$PROMETHEUS_VERSION (port 9090)";;
    alertmanager) echo "Alertmanager v$ALERTMANAGER_VERSION (port 9093)";;
    grafana)      echo "Grafana v$GRAFANA_VERSION (port 3000)";;
    exporter)     echo "SSH-polling metrics exporter (port 9101)";;
  esac
}

module_unit() {
  case "$1" in
    webui)        echo "switchboard-webui";;
    prometheus)   echo "prometheus";;
    alertmanager) echo "alertmanager";;
    grafana)      echo "grafana";;
    exporter)     echo "s4048-exporter";;
  esac
}

bundle_modules() {
  case "$1" in
    # The no-shared-filesystem pairing - see the header.
    app)        echo "webui prometheus";;
    monitoring) echo "prometheus alertmanager grafana";;
    all)        echo "webui prometheus alertmanager grafana exporter";;
    *)          echo "";;
  esac
}

bundle_desc() {
  case "$1" in
    app)        echo "webui + Prometheus together, sharing the alert-rules file on local disk (no NFS/CIFS/SMB needed)";;
    monitoring) echo "Prometheus + Alertmanager + Grafana - the metrics/alerting side, no webui";;
    all)        echo "Everything on this one host";;
  esac
}

# ---------------------------------------------------------------- detect

installed_version() {
  # Echoes a version string for an installed module, or "" if absent.
  case "$1" in
    webui)
      [[ -f "$SB_HOME/app/app.py" ]] || return 0
      if [[ -f "$SB_HOME/.installed-commit" ]]; then
        cat "$SB_HOME/.installed-commit"
      else
        echo "unknown"
      fi;;
    prometheus)
      [[ -x /opt/prometheus/prometheus ]] || return 0
      /opt/prometheus/prometheus --version 2>&1 | head -1 | sed -E 's/.*version ([0-9.]+).*/\1/';;
    alertmanager)
      [[ -x /opt/alertmanager/alertmanager ]] || return 0
      /opt/alertmanager/alertmanager --version 2>&1 | head -1 | sed -E 's/.*version ([0-9.]+).*/\1/';;
    grafana)
      [[ -x /opt/grafana/bin/grafana ]] || return 0
      /opt/grafana/bin/grafana --version 2>&1 | head -1 | sed -E 's/.*version ([0-9.]+).*/\1/';;
    exporter)
      [[ -d /opt/s4048-exporter ]] || return 0
      echo "installed";;
  esac
}

target_version() {
  case "$1" in
    prometheus)   echo "$PROMETHEUS_VERSION";;
    alertmanager) echo "$ALERTMANAGER_VERSION";;
    grafana)      echo "$GRAFANA_VERSION";;
    webui)        (cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null) || echo "unknown";;
    exporter)     (cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null) || echo "unknown";;
  esac
}

unit_state() {
  local unit; unit="$(module_unit "$1")"
  if ! systemctl list-unit-files "$unit.service" >/dev/null 2>&1 \
     || ! systemctl cat "$unit.service" >/dev/null 2>&1; then
    echo "-"; return
  fi
  # stdout redirected as well as stderr: this function's output is
  # captured by the caller, so anything systemctl prints would be spliced
  # into the status column. --quiet is supposed to be silent, but relying
  # on that makes the whole table hostage to one chatty implementation.
  if systemctl is-active --quiet "$unit" >/dev/null 2>&1; then echo "running"
  elif systemctl is-enabled --quiet "$unit" >/dev/null 2>&1; then echo "enabled/stopped"
  else echo "installed/stopped"; fi
}

do_detect() {
  step "Installed on $(hostname) ($(hostname -I 2>/dev/null | awk '{print $1}'))"
  printf '  %-14s %-12s %-16s %-12s %s\n' MODULE INSTALLED AVAILABLE SERVICE STATUS
  printf '  %s\n' "$(printf '%.0s-' {1..72})"
  local m have want state note
  for m in "${ALL_MODULES[@]}"; do
    have="$(installed_version "$m" || true)"
    want="$(target_version "$m" || true)"
    state="$(unit_state "$m")"
    if [[ -z "$have" ]]; then
      note="not installed"
    elif [[ "$have" == "$want" ]]; then
      note="up to date"
    else
      note="${C_YELLOW}update available${C_RESET}"
    fi
    printf '  %-14s %-12s %-16s %-12s %b\n' \
      "$m" "${have:--}" "${want:--}" "$state" "$note"
  done
  say ""
}

# ------------------------------------------------------------ prereqs

ensure_apt() {
  step "Installing OS prerequisites: $*"
  run apt-get update -qq
  run apt-get install -y -qq "$@"
}

ensure_python() {
  # `import venv` succeeding is NOT proof venv creation works: on
  # Debian/Ubuntu the module is stdlib and imports fine without the
  # python3-venv package, but ensurepip then has nothing to bootstrap
  # from and `python3 -m venv` silently produces a venv with no pip.
  # Confirmed live in a clean debian:12-slim container. apt is a near
  # no-op when they're already present, so this is cheap to always run.
  ensure_apt python3 python3-venv python3-pip curl
}

ensure_user() {
  local user="$1"
  if id "$user" >/dev/null 2>&1; then
    ok "user $user exists"
  else
    run useradd --system --no-create-home --shell /usr/sbin/nologin "$user"
    [[ $DRY_RUN -eq 0 ]] && ok "created user $user"
  fi
}

ensure_shared_group() {
  # Only meaningful when webui and prometheus are on the same host - which
  # is the entire point of the `app` bundle.
  if getent group "$SHARED_GROUP" >/dev/null; then
    ok "group $SHARED_GROUP exists"
  else
    run groupadd "$SHARED_GROUP"
    [[ $DRY_RUN -eq 0 ]] && ok "created group $SHARED_GROUP"
  fi
}

fetch_tarball() {
  # fetch_tarball <url> <tmpfile> - fails loudly rather than leaving a
  # truncated archive to fail confusingly at tar/exec time.
  local url="$1" dest="$2"
  run curl -fsSL --retry 3 -o "$dest" "$url" \
    || die "download failed: $url"
  [[ $DRY_RUN -eq 1 ]] && return 0
  [[ -s "$dest" ]] || die "downloaded file is empty: $url"
}

write_unit() {
  # write_unit <name> <<'EOF' ... EOF  - reads the unit body on stdin
  local name="$1"
  if [[ $DRY_RUN -eq 1 ]]; then
    cat >/dev/null
    printf '  %s[dry-run]%s write /etc/systemd/system/%s.service\n' "$C_DIM" "$C_RESET" "$name"
    return
  fi
  cat > "/etc/systemd/system/$name.service"
  ok "wrote /etc/systemd/system/$name.service"
}

enable_now() {
  local unit="$1"
  run systemctl daemon-reload
  run systemctl enable --now "$unit"
}

wait_healthy() {
  # wait_healthy <label> <url> [tries] - a service that "started" but
  # immediately crash-loops is the failure this catches; systemd's
  # Restart=on-failure will happily retry a permissions bug forever.
  local label="$1" url="$2" tries="${3:-20}" i
  [[ $DRY_RUN -eq 1 ]] && return 0
  for ((i=1; i<=tries; i++)); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      ok "$label is responding at $url"
      return 0
    fi
    sleep 1
  done
  warn "$label did not respond at $url after ${tries}s"
  warn "check: journalctl -u $(module_unit "$label") -n 50 --no-pager"
  return 1
}

# ------------------------------------------------------------ webui

install_webui() {
  step "Installing webui"
  ensure_python
  ensure_user switchboard
  ensure_shared_group
  run usermod -aG "$SHARED_GROUP" switchboard

  [[ -f "$REPO_DIR/webui/app.py" ]] || die "no webui/app.py - run this from a full repo checkout"

  if ! command -v node >/dev/null 2>&1 || [[ "$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/')" -lt "$NODE_MAJOR" ]]; then
    step "Installing Node $NODE_MAJOR (Debian/Ubuntu's own is usually too old for this frontend)"
    run bash -c "curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash -"
    ensure_apt nodejs
  else
    ok "node $(node -v) already present"
  fi

  step "Building the frontend"
  run bash -c "cd '$REPO_DIR/webui/frontend' && npm install --no-fund --no-audit && npm run build"

  step "Assembling $SB_HOME/app"
  run mkdir -p "$SB_HOME/app/frontend" "$SB_DATA"
  # Globs, never a hand-written module list: naming files individually is
  # what shipped a ModuleNotFoundError restart loop to production when a
  # new module was added and missed. Every .py in these two directories is
  # meant to ship (webui tests live in webui/tests/, not the top level).
  run bash -c "cp '$REPO_DIR'/common/*.py '$REPO_DIR'/common/devices.yaml '$REPO_DIR'/webui/*.py '$REPO_DIR'/webui/requirements.txt '$SB_HOME/app/'"
  run rm -rf "$SB_HOME/app/frontend/dist"
  run cp -r "$REPO_DIR/webui/frontend/dist" "$SB_HOME/app/frontend/dist"

  step "Creating the Python venv"
  if [[ ! -x "$SB_HOME/venv/bin/pip" ]]; then
    run python3 -m venv "$SB_HOME/venv"
  fi
  [[ $DRY_RUN -eq 0 && ! -x "$SB_HOME/venv/bin/pip" ]] && \
    die "venv has no pip - install python3-venv and re-run"
  run "$SB_HOME/venv/bin/pip" install --quiet --upgrade pip
  run "$SB_HOME/venv/bin/pip" install --quiet -r "$SB_HOME/app/requirements.txt"

  # The shared alert-rules file. 664 + the shared group is what lets the
  # webui write it and Prometheus read it without a network filesystem.
  run mkdir -p "$SB_DATA"
  run touch "$ALERT_RULES_FILE"
  run chown switchboard:"$SHARED_GROUP" "$SB_DATA" "$ALERT_RULES_FILE"
  run chmod 775 "$SB_DATA"
  run chmod 664 "$ALERT_RULES_FILE"

  write_webui_env
  run chown -R switchboard:switchboard "$SB_HOME/app" "$SB_HOME/venv"
  [[ $DRY_RUN -eq 0 ]] && (cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null > "$SB_HOME/.installed-commit" || true)

  write_unit switchboard-webui <<'EOF'
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
# Deliberately NOT ProtectSystem=strict (unlike the exporter's unit): this
# process writes into /opt/switchboard/data, which strict mode blocks even
# for its own supplementary group.

[Install]
WantedBy=multi-user.target
EOF

  enable_now switchboard-webui
  wait_healthy webui http://localhost:8080/healthz || true
}

write_webui_env() {
  run mkdir -p "$SB_CONF"
  if [[ -f "$SB_CONF/webui.env" ]]; then
    ok "$SB_CONF/webui.env exists - left untouched"
    return
  fi
  step "Creating $SB_CONF/webui.env"
  local db loki am secret
  say "  The webui needs a Postgres DSN. Everything else can be edited later."
  db="$(ask '  DATABASE_URL' 'postgresql://switchboard:changeme@127.0.0.1:5432/switchboard')"
  loki="$(ask '  LOKI_URL (blank to skip)' '')"
  if is_selected prometheus; then
    am="$(ask '  ALERTMANAGER_URL' 'http://127.0.0.1:9093')"
  else
    am="$(ask '  ALERTMANAGER_URL' 'http://127.0.0.1:9093')"
  fi
  secret="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  %s[dry-run]%s write %s/webui.env\n' "$C_DIM" "$C_RESET" "$SB_CONF"; return
  fi
  cat > "$SB_CONF/webui.env" <<EOF
DATABASE_URL=$db
LOKI_URL=$loki

ALERTMANAGER_URL=$am
PROMETHEUS_URL=http://localhost:9090
PROMETHEUS_RELOAD_URL=http://localhost:9090/-/reload

# Shared with Prometheus on this host - prometheus.yml's rule_files entry
# must point at this exact path.
ALERT_RULES_FILE=$ALERT_RULES_FILE

SESSION_SECRET_KEY=$secret
SESSION_COOKIE_SECURE=false
SESSION_TTL_HOURS=12

# OIDC - login will not work until these are real.
# See webui/README.md "Login: OIDC against Keycloak".
OIDC_ISSUER_URL=
OIDC_CLIENT_ID=switchboard
OIDC_CLIENT_SECRET=
OIDC_REDIRECT_URI=http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080/api/auth/callback
EOF
  chmod 600 "$SB_CONF/webui.env"
  chown switchboard:switchboard "$SB_CONF/webui.env"
  ok "wrote $SB_CONF/webui.env (0600)"
}

# ------------------------------------------------------------ prometheus

install_prometheus() {
  step "Installing Prometheus v$PROMETHEUS_VERSION"
  ensure_apt curl
  ensure_user prometheus
  ensure_shared_group
  run usermod -aG "$SHARED_GROUP" prometheus

  # Config lives in /etc, never inside /opt/prometheus - an update
  # replaces that whole directory, so a prometheus.yml kept in there is
  # destroyed on every upgrade. Same convention the exporter and
  # alertmanager already use.
  run mkdir -p /etc/prometheus
  # Migrate a config left in the old location by an earlier version of
  # this script (or a hand-rolled install following the guide) *before*
  # the directory is removed, rather than silently losing it.
  if [[ -f /opt/prometheus/prometheus.yml && ! -f /etc/prometheus/prometheus.yml ]]; then
    run cp /opt/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
    ok "migrated existing prometheus.yml to /etc/prometheus/"
  fi

  local tmp=/tmp/prometheus.tar.gz
  fetch_tarball "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION}.linux-amd64.tar.gz" "$tmp"
  run tar xzf "$tmp" -C /tmp
  run rm -rf /opt/prometheus
  run mv "/tmp/prometheus-${PROMETHEUS_VERSION}.linux-amd64" /opt/prometheus
  run chown -R prometheus:prometheus /opt/prometheus

  # mkdir and chown stay together, right here. Splitting them apart is a
  # confirmed-live failure: Prometheus mmaps queries.active inside this
  # directory at startup, and "permission denied" on it makes systemd
  # restart-loop forever (Restart=on-failure never fixes a permissions
  # problem, it just retries the same one).
  run mkdir -p /var/lib/prometheus
  run chown -R prometheus:prometheus /var/lib/prometheus

  write_prometheus_yml

  run mkdir -p "$SB_DATA"
  run touch "$ALERT_RULES_FILE"
  if getent passwd switchboard >/dev/null; then
    run chown switchboard:"$SHARED_GROUP" "$ALERT_RULES_FILE"
  else
    run chown prometheus:"$SHARED_GROUP" "$ALERT_RULES_FILE"
  fi
  run chmod 664 "$ALERT_RULES_FILE"

  write_unit prometheus <<'EOF'
[Unit]
Description=Prometheus
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=prometheus
Group=prometheus
ExecStart=/opt/prometheus/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --web.console.libraries=/opt/prometheus/console_libraries \
  --web.console.templates=/opt/prometheus/consoles \
  --web.enable-lifecycle
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  enable_now prometheus
  wait_healthy prometheus http://localhost:9090/-/healthy || true
}

write_prometheus_yml() {
  # Any existing config is the operator's, not ours - an update must never
  # overwrite it (or re-prompt for values already answered).
  if [[ -f /etc/prometheus/prometheus.yml ]]; then
    ok "/etc/prometheus/prometheus.yml exists - left untouched"
    return
  fi
  step "Writing /etc/prometheus/prometheus.yml"
  local am_target exp_target
  am_target="$(ask '  Alertmanager host:port' '127.0.0.1:9093')"
  exp_target="$(ask '  Exporter host:port' '127.0.0.1:9101')"

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  %s[dry-run]%s write /etc/prometheus/prometheus.yml\n' "$C_DIM" "$C_RESET"; return
  fi
  cat > /etc/prometheus/prometheus.yml <<EOF
global:
  scrape_interval: 30s
  # Explicit, not defaulted. Prometheus's own default is 1 minute, which
  # stacked with the scrape and poll cycles turned a real PSU failure into
  # ~90s before it even reached "pending" (confirmed live).
  evaluation_interval: 15s

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["$am_target"]

rule_files:
  - $ALERT_RULES_FILE

scrape_configs:
  - job_name: s4048
    scrape_interval: 10s
    static_configs:
      - targets: ["$exp_target"]
  - job_name: switchboard
    metrics_path: /metrics
    static_configs:
      - targets: ["localhost:8080"]
EOF
  chown -R prometheus:prometheus /etc/prometheus
  ok "wrote /etc/prometheus/prometheus.yml"
}

# ------------------------------------------------------------ alertmanager

install_alertmanager() {
  step "Installing Alertmanager v$ALERTMANAGER_VERSION"
  ensure_apt curl
  ensure_user alertmanager

  local tmp=/tmp/alertmanager.tar.gz
  fetch_tarball "https://github.com/prometheus/alertmanager/releases/download/v${ALERTMANAGER_VERSION}/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64.tar.gz" "$tmp"
  run tar xzf "$tmp" -C /tmp
  run rm -rf /opt/alertmanager
  run mv "/tmp/alertmanager-${ALERTMANAGER_VERSION}.linux-amd64" /opt/alertmanager
  run mkdir -p /var/lib/alertmanager /etc/alertmanager/secrets

  if [[ -f /etc/alertmanager/alertmanager.yml ]]; then
    # Config *and* the secrets beside it are the operator's - an update
    # replaces only the binary in /opt. Real Pushover/PagerDuty keys live
    # in /etc/alertmanager/secrets and must never be overwritten by the
    # repo's placeholder copies.
    ok "/etc/alertmanager/alertmanager.yml exists - left untouched"
    ok "/etc/alertmanager/secrets left untouched"
  else
    local webui_url
    webui_url="$(ask '  webui host:port (for the alert webhook)' "$(hostname -I 2>/dev/null | awk '{print $1}'):8080")"
    if [[ $DRY_RUN -eq 0 ]]; then
      if [[ -f "$REPO_DIR/alertmanager/alertmanager.yml" ]]; then
        sed -E "s#http://[^\"']*/api/alertmanager/webhook#http://$webui_url/api/alertmanager/webhook#g" \
          "$REPO_DIR/alertmanager/alertmanager.yml" > /etc/alertmanager/alertmanager.yml
        ok "installed alertmanager.yml from the repo, webhook -> $webui_url"
      else
        cat > /etc/alertmanager/alertmanager.yml <<EOF
route:
  receiver: switchboard
  group_wait: 0s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: switchboard
    webhook_configs:
      - url: "http://$webui_url/api/alertmanager/webhook"
EOF
        ok "wrote a minimal alertmanager.yml (webhook -> $webui_url)"
      fi
    fi
    [[ -d "$REPO_DIR/alertmanager/secrets" ]] && \
      run bash -c "cp -r '$REPO_DIR'/alertmanager/secrets/* /etc/alertmanager/secrets/ 2>/dev/null || true"
  fi
  run chown -R alertmanager:alertmanager /opt/alertmanager /var/lib/alertmanager /etc/alertmanager

  write_unit alertmanager <<'EOF'
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

  enable_now alertmanager
  wait_healthy alertmanager http://localhost:9093/-/healthy || true
}

# ------------------------------------------------------------ grafana

install_grafana() {
  step "Installing Grafana v$GRAFANA_VERSION"
  ensure_apt curl
  ensure_user grafana

  # Provisioning lives in /etc, not /opt/grafana/conf - an update replaces
  # that whole directory, which would silently wipe the datasource and
  # dashboard provisioning every upgrade. Grafana's own
  # `paths.provisioning` setting exists exactly so this can live elsewhere.
  run mkdir -p /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards
  if [[ -d /opt/grafana/conf/provisioning/datasources ]] && \
     [[ ! -f /etc/grafana/provisioning/datasources/switchboard.yml ]]; then
    run bash -c "cp -r /opt/grafana/conf/provisioning/datasources/*.yml /etc/grafana/provisioning/datasources/ 2>/dev/null || true"
  fi

  local tmp=/tmp/grafana.tar.gz
  fetch_tarball "https://dl.grafana.com/oss/release/grafana-${GRAFANA_VERSION}.linux-amd64.tar.gz" "$tmp"
  run tar xzf "$tmp" -C /tmp
  run rm -rf /opt/grafana
  run mv "/tmp/grafana-v${GRAFANA_VERSION}" /opt/grafana
  run mkdir -p /var/lib/grafana/dashboards /var/log/grafana

  # Dashboards are content, not config: refreshed from the repo each time.
  # The provisioning *config* pointing at them is what must survive.
  if [[ -d "$REPO_DIR/grafana/dashboards" ]]; then
    run bash -c "cp '$REPO_DIR'/grafana/dashboards/*.json /var/lib/grafana/dashboards/ 2>/dev/null || true"
    run bash -c "cp -r '$REPO_DIR'/grafana/provisioning/dashboards/* /etc/grafana/provisioning/dashboards/ 2>/dev/null || true"
  fi

  if [[ ! -f /etc/grafana/provisioning/datasources/switchboard.yml ]]; then
    local prom_url loki_url
    prom_url="$(ask '  Prometheus URL' 'http://127.0.0.1:9090')"
    loki_url="$(ask '  Loki URL (blank to skip)' '')"
    if [[ $DRY_RUN -eq 0 ]]; then
      mkdir -p /etc/grafana/provisioning/datasources
      {
        echo "apiVersion: 1"
        echo "datasources:"
        echo "  - name: Prometheus"
        echo "    type: prometheus"
        echo "    access: proxy"
        echo "    url: $prom_url"
        echo "    isDefault: true"
        if [[ -n "$loki_url" ]]; then
          echo "  - name: Loki"
          echo "    type: loki"
          echo "    uid: loki"
          echo "    access: proxy"
          echo "    url: $loki_url"
          echo "    jsonData:"
          echo "      maxLines: 1000"
        fi
      } > /etc/grafana/provisioning/datasources/switchboard.yml
      ok "wrote Grafana datasource provisioning (/etc/grafana)"
    fi
  fi

  # Only ask for a password on a first install - on an update the unit
  # already carries one, and re-prompting would silently reset it.
  local gpass=""
  if [[ ! -f /etc/systemd/system/grafana.service ]]; then
    gpass="$(ask '  Grafana admin password' 'changeme')"
  else
    ok "grafana.service exists - keeping its existing admin password"
  fi
  run chown -R grafana:grafana /opt/grafana /var/lib/grafana /var/log/grafana /etc/grafana

  if [[ $DRY_RUN -eq 0 && -n "$gpass" ]]; then
    cat > /etc/systemd/system/grafana.service <<EOF
[Unit]
Description=Grafana
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=grafana
Group=grafana
Environment=GF_SECURITY_ADMIN_PASSWORD=$gpass
Environment=GF_USERS_ALLOW_SIGN_UP=false
ExecStart=/opt/grafana/bin/grafana server \\
  --homepath=/opt/grafana \\
  --configOverrides="cfg:default.paths.data=/var/lib/grafana cfg:default.paths.logs=/var/log/grafana cfg:default.paths.provisioning=/etc/grafana/provisioning"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    ok "wrote /etc/systemd/system/grafana.service"
  fi

  enable_now grafana
  wait_healthy grafana http://localhost:3000/api/health 30 || true
}

# ------------------------------------------------------------ exporter

install_exporter() {
  step "Installing the exporter (delegating to packaging/install.sh)"
  [[ -x "$SCRIPT_DIR/install.sh" ]] || die "packaging/install.sh not found or not executable"
  run "$SCRIPT_DIR/install.sh"
}

# ------------------------------------------------------------ dispatch

is_selected() {
  local m
  for m in "${SELECTED[@]:-}"; do [[ "$m" == "$1" ]] && return 0; done
  return 1
}

install_module() {
  case "$1" in
    webui)        install_webui;;
    prometheus)   install_prometheus;;
    alertmanager) install_alertmanager;;
    grafana)      install_grafana;;
    exporter)     install_exporter;;
    *)            die "unknown module: $1";;
  esac
}

# ------------------------------------------------------------ next steps

next_steps() {
  local ip m
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  say ""
  printf '%s%s%s\n' "$C_BOLD" "================ Next steps ================" "$C_RESET"
  say ""

  for m in "${SELECTED[@]}"; do
    case "$m" in
      webui)
        say "${C_BOLD}webui${C_RESET}  http://$ip:8080"
        say "  1. Edit ${C_BOLD}$SB_CONF/webui.env${C_RESET} and set the OIDC_* values."
        say "     Login will not work until they are real - the app has no local"
        say "     accounts by design (see webui/README.md, 'Login: OIDC against"
        say "     Keycloak'). In Keycloak you need a confidential client with"
        say "     redirect URI http://$ip:8080/api/auth/callback and the three"
        say "     client roles viewer/operator/admin assigned to your users."
        say "  2. Confirm DATABASE_URL points at a reachable Postgres."
        say "     ${C_DIM}Schema is created/migrated automatically at startup.${C_RESET}"
        say "  3. systemctl restart switchboard-webui && curl -s localhost:8080/readyz"
        say ""
        say "  ${C_YELLOW}One database per deployment.${C_RESET} Two Switchboard instances sharing"
        say "  one Postgres will fight over alarm state - each reconciles against"
        say "  its own Alertmanager and closes what the other opened. Confirmed"
        say "  live: that produced ~19,800 junk rows before it was noticed."
        say "";;
      prometheus)
        say "${C_BOLD}prometheus${C_RESET}  http://$ip:9090"
        say "  - Targets: http://$ip:9090/targets (both jobs should read 'up')"
        say "  - Edit /opt/prometheus/prometheus.yml to correct any host:port,"
        say "    then: systemctl reload-or-restart prometheus"
        if is_selected webui; then
          say "  - Sharing $ALERT_RULES_FILE with the webui via the"
          say "    $SHARED_GROUP group, so the Rules tab works with no NFS/CIFS."
        else
          say "  ${C_YELLOW}- webui is NOT on this host.${C_RESET} The Rules tab writes"
          say "    $ALERT_RULES_FILE and calls /-/reload, so it needs that file"
          say "    shared with wherever the webui runs - which means NFS/CIFS, or"
          say "    moving them onto one host (the 'app' bundle) instead."
        fi
        say "";;
      alertmanager)
        say "${C_BOLD}alertmanager${C_RESET}  http://$ip:9093"
        say "  - Check the webhook URL in /etc/alertmanager/alertmanager.yml"
        say "    points at the webui host, or alarms will never reach it."
        say "  - Put real Pushover/PagerDuty credentials in"
        say "    /etc/alertmanager/secrets/ (chmod 600, owned by alertmanager)."
        say "  - amtool check-config /etc/alertmanager/alertmanager.yml"
        say "";;
      grafana)
        say "${C_BOLD}grafana${C_RESET}  http://$ip:3000"
        say "  - Log in as admin with the password you set, then change it."
        say "  - Dashboards are provisioned from /var/lib/grafana/dashboards."
        say "";;
      exporter)
        say "${C_BOLD}exporter${C_RESET}  http://$ip:9101/metrics"
        say "  - Set real device credentials in /etc/s4048-exporter/exporter.env"
        say "  - Point Prometheus's s4048 job at $ip:9101"
        say "";;
    esac
  done

  say "${C_BOLD}Health check everything installed here:${C_RESET}"
  for m in "${SELECTED[@]}"; do
    printf '  systemctl status %s --no-pager | head -3\n' "$(module_unit "$m")"
  done
  say ""
  say "${C_BOLD}Update later:${C_RESET}  cd $REPO_DIR && git pull && sudo $0 --update"
  say "${C_BOLD}Re-check state:${C_RESET} sudo $0 --detect"
  say ""
}

# ------------------------------------------------------------ interactive

interactive_menu() {
  do_detect
  say "${C_BOLD}Bundles${C_RESET} (recommended - avoids needing a shared filesystem)"
  local b
  for b in app monitoring all; do
    printf '  %-12s %s\n' "$b" "$(bundle_desc "$b")"
    printf '  %-12s %s%s%s\n' "" "$C_DIM" "-> $(bundle_modules "$b")" "$C_RESET"
  done
  say ""
  say "${C_BOLD}Individual modules${C_RESET}"
  local m
  for m in "${ALL_MODULES[@]}"; do
    printf '  %-12s %s\n' "$m" "$(module_desc "$m")"
  done
  say ""
  say "Enter a bundle name, or space/comma-separated module names."
  local answer
  answer="$(ask 'Install' 'app')"
  parse_selection "$answer"
}

parse_selection() {
  local raw="${1//,/ }" token expanded
  SELECTED=()
  for token in $raw; do
    expanded="$(bundle_modules "$token")"
    if [[ -n "$expanded" ]]; then
      for m in $expanded; do is_selected "$m" || SELECTED+=("$m"); done
    else
      local valid=0 known
      for known in "${ALL_MODULES[@]}"; do [[ "$known" == "$token" ]] && valid=1; done
      [[ $valid -eq 1 ]] || die "unknown module or bundle: $token (try --list)"
      is_selected "$token" || SELECTED+=("$token")
    fi
  done
  [[ ${#SELECTED[@]} -gt 0 ]] || die "nothing selected"
}

do_list() {
  say "${C_BOLD}Bundles${C_RESET}"
  local b m
  for b in app monitoring all; do
    printf '  %-12s %s\n' "$b" "$(bundle_desc "$b")"
    printf '  %-12s %s%s%s\n' "" "$C_DIM" "-> $(bundle_modules "$b")" "$C_RESET"
  done
  say ""
  say "${C_BOLD}Modules${C_RESET}"
  for m in "${ALL_MODULES[@]}"; do
    printf '  %-12s %s\n' "$m" "$(module_desc "$m")"
  done
}

do_update() {
  local m have want to_update=()
  for m in "${ALL_MODULES[@]}"; do
    have="$(installed_version "$m" || true)"
    [[ -z "$have" ]] && continue
    want="$(target_version "$m" || true)"
    if [[ "$have" != "$want" ]]; then
      to_update+=("$m")
    else
      ok "$m already at $want"
    fi
  done
  if [[ ${#to_update[@]} -eq 0 ]]; then
    say ""
    ok "Everything installed here is already up to date."
    say "  ${C_DIM}(webui/exporter track the repo checkout - git pull first if you expected a change)${C_RESET}"
    return
  fi
  say ""
  say "Will update: ${to_update[*]}"
  confirm "Proceed?" || die "aborted"
  SELECTED=("${to_update[@]}")
  for m in "${SELECTED[@]}"; do install_module "$m"; done
  next_steps
}

usage() {
  cat <<EOF
Switchboard stack installer v$VERSION - native systemd, no Docker.

Usage: sudo $0 [options]

  (no options)          interactive: detect, then choose what to install
  --install <list>      modules or a bundle, comma/space separated
  --bundle <name>       same as --install with a bundle name
  --detect              show what is installed here and what is available
  --list                show all modules and bundles
  --update              re-install any installed module that is out of date
  --dry-run             print what would happen, change nothing
  -y, --yes             accept defaults, no prompts (for automation)
  -h, --help            this

Modules:  ${ALL_MODULES[*]}
Bundles:  app, monitoring, all

Examples:
  sudo $0                                  # interactive
  sudo $0 --bundle app                     # webui + prometheus, no shared FS needed
  sudo $0 --install alertmanager,grafana
  sudo $0 --update                         # after a git pull
  sudo $0 --detect
EOF
}

# ------------------------------------------------------------ main

main() {
  local mode="interactive" selection=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install|--bundle) selection="$2"; mode="install"; shift 2;;
      --detect)  mode="detect"; shift;;
      --list)    mode="list"; shift;;
      --update)  mode="update"; shift;;
      --dry-run) DRY_RUN=1; shift;;
      -y|--yes)  ASSUME_YES=1; shift;;
      -h|--help) usage; exit 0;;
      *) die "unknown option: $1 (try --help)";;
    esac
  done

  # list/detect only read - no reason to demand root for them, and
  # needing sudo just to ask "what's installed here?" is the kind of
  # friction that stops people checking before they change something.
  [[ "$mode" == "list" ]] && { do_list; exit 0; }
  [[ "$mode" == "detect" ]] && { do_detect; exit 0; }

  if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
    die "Run as root (sudo $0 ...)"
  fi

  case "$mode" in
    update) do_detect; do_update; exit 0;;
    install) parse_selection "$selection";;
    interactive) interactive_menu;;
  esac

  say ""
  say "Selected: ${C_BOLD}${SELECTED[*]}${C_RESET}"
  if is_selected prometheus && ! is_selected webui && [[ ! -d "$SB_HOME/app" ]]; then
    warn "Prometheus without the webui on this host: the Rules tab needs the"
    warn "alert-rules file shared between them (NFS/CIFS, or use --bundle app)."
  fi
  confirm "Continue?" || die "aborted"

  local m
  for m in "${SELECTED[@]}"; do install_module "$m"; done

  say ""
  do_detect
  next_steps
}

main "$@"
