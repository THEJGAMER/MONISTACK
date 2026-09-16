#!/usr/bin/env bash
# Switchboard stack installer - native systemd, no Docker anywhere.
#
# Installs any combination of the five services (webui, prometheus,
# exporter, sflow, syslog) onto this host, either
# individually or as a pre-set bundle. Detects what is already installed, and can update
# in place rather than reinstalling.
#
# The bundles are just convenient groupings: `app` (webui + Prometheus on
# one host), `monitoring` (Prometheus + exporter), `collector` (sFlow),
# `ingest` (sFlow + syslog), `all`. See docs/deploy-lxc-4lxcs-native.md.
#
# Every version, URL and path here comes from that guide, where they were
# actually downloaded and executed rather than taken from documentation.
set -euo pipefail

VERSION="1.0.0"

# Pinned to match docker-compose.yml, so a native install and a Docker one
# are the same software. Bump both together.
PROMETHEUS_VERSION="2.55.1"
NODE_MAJOR="20"
# The version the syslog VRL was written and verified against.
VECTOR_VERSION="0.57.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SB_HOME=/opt/switchboard
SB_DATA="$SB_HOME/data"
SB_CONF=/etc/switchboard
SFLOW_CONF=/etc/pmacct/sfacctd.conf
SYSLOG_CONF=/etc/vector/vector.yaml
SYSLOG_CANDIDATE=/etc/vector/vector.candidate.yaml
SYSLOG_MARKER=/etc/vector/.switchboard-commit
SFLOW_MARKER=/etc/pmacct/.switchboard-commit

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

have_tty() {
  # Whether prompts are possible at all. Probed once and cached, because
  # the probe is the thing that would otherwise be noisy: /dev/tty exists
  # as a world-readable device node even with no controlling terminal, so
  # a permission test says yes and only the open actually fails. Putting
  # 2>/dev/null on the read itself does not help either - redirections
  # are set up left to right, so </dev/tty has already failed and printed
  # by then, and moving it earlier would swallow read -p's prompt, which
  # also goes to stderr.
  if [[ -z "${HAVE_TTY:-}" ]]; then
    if { : </dev/tty; } 2>/dev/null; then HAVE_TTY=1; else HAVE_TTY=0; fi
  fi
  [[ "$HAVE_TTY" == "1" ]]
}

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  # No terminal to ask at: decline. Every confirm here guards something
  # that changes state, so silence must not read as consent.
  have_tty || return 1
  local reply
  read -r -p "$1 [y/N] " reply </dev/tty || reply=n
  [[ "$reply" =~ ^[Yy] ]]
}

ask() {
  # ask <prompt> <default> -> echoes the answer
  local prompt="$1" default="${2:-}" reply
  if [[ $ASSUME_YES -eq 1 ]] || ! have_tty; then printf '%s' "$default"; return; fi
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " reply </dev/tty || reply=""
  else
    read -r -p "$prompt: " reply </dev/tty || reply=""
  fi
  printf '%s' "${reply:-$default}"
}

ask_secret() {
  # Like ask, but does not echo. The database password ends up in a 0600
  # file; printing it into the scrollback of whatever terminal ran the
  # installer gives most of that back.
  local prompt="$1" default="${2:-}" reply
  if [[ $ASSUME_YES -eq 1 ]] || ! have_tty; then printf '%s' "$default"; return; fi
  if [[ -n "$default" ]]; then
    read -rs -p "$prompt [keep existing]: " reply </dev/tty || reply=""
  else
    read -rs -p "$prompt: " reply </dev/tty || reply=""
  fi
  printf '\n' >&2
  printf '%s' "${reply:-$default}"
}

# ---------------------------------------------------------------- modules

ALL_MODULES=(webui prometheus exporter sflow syslog)

module_desc() {
  case "$1" in
    webui)        echo "Switchboard web UI (FastAPI + systemd, port 8080)";;
    prometheus)   echo "Prometheus v$PROMETHEUS_VERSION (port 9090)";;
    exporter)     echo "SSH-polling metrics exporter (port 9101)";;
    sflow)        echo "sFlow collector - sfacctd into Postgres (UDP 6343)";;
    syslog)       echo "Syslog receiver - Vector v$VECTOR_VERSION into Loki (UDP/TCP 514)";;
  esac
}

module_unit() {
  case "$1" in
    webui)        echo "switchboard-webui";;
    prometheus)   echo "prometheus";;
    exporter)     echo "s4048-exporter";;
    sflow)        echo "sfacctd";;
    syslog)       echo "vector";;
  esac
}

bundle_modules() {
  case "$1" in
    app)        echo "webui prometheus";;
    monitoring) echo "prometheus exporter";;
    # Its own bundle rather than part of `app`: the collector is usually
    # given its own host so switch sampling traffic lands somewhere it
    # cannot compete with the webui, and it is the only module whose
    # install is incomplete until the switches are reconfigured.
    collector)  echo "sflow";;
    # Both receivers on one box: neither needs anything the other has, and
    # keeping ingest off the webui host means a flood of flows or log
    # lines cannot starve the thing people are looking at.
    ingest)     echo "sflow syslog";;
    all)        echo "webui prometheus exporter sflow syslog";;
    *)          echo "";;
  esac
}

bundle_desc() {
  case "$1" in
    app)        echo "webui + Prometheus together on one host";;
    monitoring) echo "Prometheus + the SSH-polling exporter - the metrics side, no webui";;
    collector)  echo "sFlow collector only - for a dedicated LXC/VM the switches sample into";;
    ingest)     echo "Both receivers - sFlow and syslog - on one dedicated host";;
    all)        echo "Everything on this one host";;
  esac
}

# ---------------------------------------------------------------- detect

write_commit_marker() {
  # write_commit_marker <path> - records which checkout produced this
  # install, so --update can tell "already current" from "out of date".
  #
  # Captured first, written second. `git rev-parse > file || true` opens
  # the file before git runs, so a checkout without commits (or no git at
  # all) leaves a zero-byte marker behind - which read_commit_marker then
  # returns as "", and the detect table renders as "not installed" for
  # something it installed thirty seconds earlier. Confirmed live.
  local path="$1" rev
  rev="$( (cd "$REPO_DIR" && git rev-parse --short HEAD) 2>/dev/null )" || rev=""
  if [[ -n "$rev" ]]; then
    printf '%s\n' "$rev" > "$path"
  else
    # No commit to record. Leave any previous marker alone rather than
    # replacing a real answer with a worse one.
    [[ -f "$path" ]] || printf 'unknown\n' > "$path"
  fi
}

read_commit_marker() {
  # An empty marker means the same thing as a missing one.
  local path="$1" val=""
  [[ -f "$path" ]] && val="$(cat "$path" 2>/dev/null)"
  printf '%s' "${val:-unknown}"
}

installed_version() {
  # Echoes a version string for an installed module, or "" if absent.
  case "$1" in
    webui)
      [[ -f "$SB_HOME/app/app.py" ]] || return 0
      read_commit_marker "$SB_HOME/.installed-commit";;
    prometheus)
      [[ -x /opt/prometheus/prometheus ]] || return 0
      /opt/prometheus/prometheus --version 2>&1 | head -1 | sed -E 's/.*version ([0-9.]+).*/\1/';;
    exporter)
      [[ -d /opt/s4048-exporter ]] || return 0
      echo "installed";;
    sflow)
      [[ -f "$SFLOW_CONF" ]] || return 0
      read_commit_marker "$SFLOW_MARKER";;
    syslog)
      [[ -f "$SYSLOG_CONF" ]] || return 0
      read_commit_marker "$SYSLOG_MARKER";;
  esac
}

target_version() {
  case "$1" in
    prometheus)   echo "$PROMETHEUS_VERSION";;
    webui)        (cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null) || echo "unknown";;
    exporter)     (cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null) || echo "unknown";;
    sflow)        (cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null) || echo "unknown";;
    syslog)       (cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null) || echo "unknown";;
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
  # `enable --now` is enable + *start*, and start is a no-op on a service
  # that's already running - so on an update it would copy the new code
  # and leave the old process serving it, with everything looking
  # successful. That exact failure hit production: new files on disk, an
  # 11-hour-old process, and a UI calling an endpoint its backend didn't
  # have yet. `restart` is correct for both cases, since restarting a
  # stopped unit just starts it.
  local unit="$1"
  run systemctl daemon-reload
  run systemctl enable "$unit"
  step "Restarting $unit to load the new code"
  run systemctl restart "$unit"
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

  run mkdir -p "$SB_DATA"
  run chown switchboard:switchboard "$SB_DATA"

  write_webui_env
  run chown -R switchboard:switchboard "$SB_HOME/app" "$SB_HOME/venv"
  [[ $DRY_RUN -eq 0 ]] && write_commit_marker "$SB_HOME/.installed-commit"

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
  local db loki secret
  say "  The webui needs a Postgres DSN. Everything else can be edited later."
  db="$(ask '  DATABASE_URL' 'postgresql://switchboard:changeme@127.0.0.1:5432/switchboard')"
  loki="$(ask '  LOKI_URL (blank to skip)' '')"
  secret="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  local ingest_token
  ingest_token="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '  %s[dry-run]%s write %s/webui.env\n' "$C_DIM" "$C_RESET" "$SB_CONF"; return
  fi
  cat > "$SB_CONF/webui.env" <<EOF
DATABASE_URL=$db
LOKI_URL=$loki
PROMETHEUS_URL=http://localhost:9090
EXPORTER_URL=http://localhost:9101

SESSION_SECRET_KEY=$secret
SESSION_COOKIE_SECURE=false
SESSION_TTL_HOURS=12

# Web Push (the in-house pager). The VAPID subject is derived from
# OIDC_REDIRECT_URI (https origin, else mailto: on its hostname); set this
# only to override. Push needs HTTPS in the browser regardless.
PUSH_VAPID_SUBJECT=

# Syslog fast path: Vector's switchboard_fast sink POSTs each syslog event
# here as it arrives (sub-second paging). Give this token to
# `install-stack.sh --install syslog` on the syslog host. SYSLOG_RECEIVER is
# where the devices send syslog (host:port) - used by the Alerts page's
# fast-path self-test only; also editable in Settings.
SYSLOG_INGEST_TOKEN=$ingest_token
SYSLOG_RECEIVER=

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

  # Config lives in /etc, never inside /opt/prometheus - an update
  # replaces that whole directory, so a prometheus.yml kept in there is
  # destroyed on every upgrade. Same convention the exporter uses.
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
  --web.console.templates=/opt/prometheus/consoles
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
  local exp_target
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

# ------------------------------------------------------------ exporter

install_exporter() {
  step "Installing the exporter (delegating to packaging/install.sh)"
  [[ -x "$SCRIPT_DIR/install.sh" ]] || die "packaging/install.sh not found or not executable"
  run "$SCRIPT_DIR/install.sh"
}

# ------------------------------------------------------------ sflow
#
# The collector is the one module that is useless on its own: it needs a
# reachable Postgres to write into and switches configured to send to it,
# and both of those are outside this script. So rather than install and
# hope, every prerequisite is *tested* here - the database before a line
# of config is written, and the UDP path afterwards with a synthetic
# datagram that proves collector -> Postgres end to end without waiting
# on any switch.

sflow_conf_value() {
  # sflow_conf_value <key> [file] - the value of a pmacct config key, or
  # "" when absent. Existing answers become the defaults on a re-run, so
  # updating a working collector never silently resets its credentials.
  local key="$1" file="${2:-$SFLOW_CONF}"
  [[ -f "$file" ]] || return 0
  sed -nE "s/^[[:space:]]*${key}[[:space:]]*:[[:space:]]*(.*[^[:space:]])[[:space:]]*$/\1/p" "$file" | head -1
}

sflow_port_holder() {
  # Who currently owns this UDP port, if anyone.
  ss -ulnpH "sport = :$1" 2>/dev/null | sed -nE 's/.*users:\(\("([^"]+)".*/\1/p' | head -1
}

sflow_psql() {
  # sflow_psql <host> <port> <db> <user> <pass> <sql>
  PGPASSWORD="$5" psql -h "$1" -p "$2" -U "$4" -d "$3" \
    -v ON_ERROR_STOP=1 -qtAc "$6" 2>&1
}

sflow_pg_test() {
  # sflow_pg_test <host> <port> <db> <user> <pass>
  #
  # Proves the four things sfacctd needs, in the order they fail. The
  # last one is the reason this exists: a read-only user lets sfacctd
  # start, connect, and report itself healthy, then discard every 60s
  # flush with an error into a log nobody is tailing. That failure is
  # invisible from the outside for as long as you care to leave it.
  local host="$1" pgport="$2" db="$3" user="$4" pass="$5" out
  if ! command -v psql >/dev/null 2>&1; then
    warn "psql not installed - cannot test the database before installing"
    return 2
  fi

  step "Testing the Postgres connection ($user@$host:$pgport/$db)"

  if ! out="$(sflow_psql "$host" "$pgport" "$db" "$user" "$pass" 'SELECT 1')"; then
    warn "cannot connect:"
    printf '      %s\n' "$out" | head -4
    return 1
  fi
  ok "connected"

  out="$(sflow_psql "$host" "$pgport" "$db" "$user" "$pass" \
        "SELECT to_regclass('public.sflow_flows')")" || true
  if [[ -z "$out" ]]; then
    warn "the sflow_flows table does not exist in $db"
    say "  ${C_DIM}The webui creates it at startup (common/db.py), along with every"
    say "  other table. On a collector-only host it may simply not have run yet.${C_RESET}"
    if [[ -f "$REPO_DIR/sflow/sflow_flows.schema" ]] && confirm "  Create it now from sflow/sflow_flows.schema?"; then
      if out="$(PGPASSWORD="$pass" psql -h "$host" -p "$pgport" -U "$user" -d "$db" \
                -v ON_ERROR_STOP=1 -q -f "$REPO_DIR/sflow/sflow_flows.schema" 2>&1)"; then
        ok "created sflow_flows"
      else
        warn "could not create it:"; printf '      %s\n' "$out" | head -4
        return 1
      fi
    else
      return 1
    fi
  else
    ok "sflow_flows table present"
  fi

  # Rolled back, so a passing test leaves nothing behind to explain later.
  if ! out="$(sflow_psql "$host" "$pgport" "$db" "$user" "$pass" \
        "BEGIN; INSERT INTO sflow_flows (peer_ip_src, packets, bytes) VALUES ('0.0.0.0', 0, 0); ROLLBACK;")"; then
    warn "$user cannot INSERT into sflow_flows - sfacctd would connect and then drop every flush:"
    printf '      %s\n' "$out" | head -4
    return 1
  fi
  ok "$user can write to sflow_flows"
  return 0
}

sflow_receive_test() {
  # sflow_receive_test <port> <seconds>
  #
  # Binds the port and reports what actually turns up. Reports the
  # agent-id separately from the sending IP because those two differ more
  # often than anyone expects - the EX3300 here announced itself as
  # 192.168.5.10 while being managed on 192.168.4.1, and the symptom was
  # an unattributable switch in the UI rather than anything that looked
  # like an addressing problem.
  local port="$1" secs="${2:-30}" holder
  holder="$(sflow_port_holder "$port")"
  if [[ -n "$holder" ]]; then
    warn "UDP/$port is already held by '$holder' - nothing else can bind it to listen"
    if [[ "$holder" == "sfacctd" ]]; then
      say "  ${C_DIM}That is the collector itself, which is the good case. To run this"
      say "  test anyway:  systemctl stop sfacctd && $0 --test-sflow $port${C_RESET}"
    fi
    return 2
  fi
  step "Listening on UDP/$port for ${secs}s to see what arrives"
  say "  ${C_DIM}Read-only: this binds a socket and waits. It sends nothing and"
  say "  touches no switch.${C_RESET}"
  python3 - "$port" "$secs" <<'PY'
import socket, struct, sys, time
port, secs = int(sys.argv[1]), int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", port))
except OSError as e:
    print("      could not bind UDP/%d: %s" % (port, e))
    raise SystemExit(2)
s.settimeout(1.0)
seen, notsflow = {}, 0
end = time.time() + secs
while time.time() < end:
    try:
        data, addr = s.recvfrom(65535)
    except socket.timeout:
        continue
    # Too short to carry a v5 header is not "an sFlow packet with an
    # unknown agent" - it is something else entirely arriving on this
    # port, and counting it as a sender would report a switch that is not
    # there.
    if len(data) < 12:
        notsflow += 1
        continue
    ver, atype = struct.unpack("!II", data[:8])
    if ver != 5:
        notsflow += 1
        continue
    # Address type 1 is IPv4. Anything else is valid sFlow this listener
    # simply does not decode, so say so rather than printing a bare "?".
    agent = socket.inet_ntoa(data[8:12]) if atype == 1 else "(non-IPv4 agent)"
    key = (addr[0], agent)
    seen[key] = seen.get(key, 0) + 1
if not seen:
    print("      nothing arrived in %ds" % secs)
    if notsflow:
        print("      (%d datagrams arrived but were not sFlow v5)" % notsflow)
    raise SystemExit(1)
print("      %-17s %-17s %s" % ("FROM", "AGENT-ID", "DATAGRAMS"))
for (src, agent), n in sorted(seen.items(), key=lambda kv: -kv[1]):
    # Only meaningful when the agent-id actually decoded to an address.
    note = "   <- agent-id differs from sender" if agent[0].isdigit() and src != agent else ""
    print("      %-17s %-17s %-9d%s" % (src, agent, n, note))
if notsflow:
    print("      (%d further datagrams were not sFlow v5)" % notsflow)
PY
}

sflow_endtoend_test() {
  # sflow_endtoend_test <port> <host> <pgport> <db> <user> <pass>
  #
  # Sends one synthetic-but-valid sFlow v5 datagram at the freshly
  # started collector and waits for it to surface in Postgres. This is
  # the only check that covers the whole path in one go, and it does not
  # depend on any switch being configured yet - so a failure here is
  # unambiguously the collector or the database, never "maybe the switch
  # isn't sending".
  local port="$1" host="$2" pgport="$3" db="$4" user="$5" pass="$6"
  local probe="$REPO_DIR/sflow/tests/send_test_datagram.py"
  local agent="203.0.113.253"   # RFC 5737 TEST-NET-3: never a real switch
  [[ $DRY_RUN -eq 1 ]] && return 0
  if [[ ! -f "$probe" ]]; then
    warn "sflow/tests/send_test_datagram.py missing - skipping the end-to-end test"
    return 2
  fi
  command -v psql >/dev/null 2>&1 || return 2

  step "End-to-end test: synthetic datagram -> sfacctd -> Postgres"
  python3 "$probe" 127.0.0.1 "$port" "$agent" >/dev/null 2>&1 \
    || { warn "could not send the test datagram"; return 1; }

  local refresh n i
  refresh="$(sflow_conf_value sql_refresh_time)"; refresh="${refresh:-60}"
  say "  ${C_DIM}sent; sfacctd flushes every ${refresh}s, so this waits up to $((refresh + 40))s${C_RESET}"
  for ((i=0; i<$((refresh + 40)); i+=5)); do
    sleep 5
    n="$(sflow_psql "$host" "$pgport" "$db" "$user" "$pass" \
         "SELECT count(*) FROM sflow_flows WHERE peer_ip_src = '$agent'")" || n=0
    [[ "$n" =~ ^[0-9]+$ ]] && [[ "$n" -gt 0 ]] && break
  done

  if [[ "${n:-0}" =~ ^[0-9]+$ ]] && [[ "${n:-0}" -gt 0 ]]; then
    ok "the test flow reached Postgres - collector, plugin and database all work"
    # Clean up: a synthetic agent left in the table becomes a phantom
    # switch on the Traffic page for as long as retention keeps it.
    sflow_psql "$host" "$pgport" "$db" "$user" "$pass" \
      "DELETE FROM sflow_flows WHERE peer_ip_src = '$agent'" >/dev/null 2>&1 \
      && ok "removed the synthetic test rows"
    return 0
  fi

  warn "the test datagram never reached Postgres"
  say "      journalctl -u sfacctd -n 40 --no-pager"
  say "      ${C_DIM}Most common cause: the pgsql plugin failing its INSERT - which"
  say "      appears only in that log, never in the service state.${C_RESET}"
  return 1
}

install_sflow() {
  step "Installing the sFlow collector (sfacctd)"
  ensure_apt pmacct postgresql-client iproute2 python3

  # Debian builds pmacct with --enable-pgsql, but a build from anywhere
  # else may not, and without it the failure is "plugin not found" at
  # startup rather than anything visible now.
  sfacctd -V 2>&1 | grep -qi postgresql \
    || die "this sfacctd was built without PostgreSQL support - the pgsql plugin cannot load"
  ok "sfacctd $(sfacctd -V 2>&1 | sed -nE 's/.*sfacctd ([0-9][0-9.]*).*/\1/p' | head -1) has PostgreSQL support"

  [[ -f "$REPO_DIR/sflow/sfacctd.conf" ]] \
    || die "no sflow/sfacctd.conf - run this from a full repo checkout"

  local d_port d_host d_dbport d_name d_user d_pass
  d_port="$(sflow_conf_value sfacctd_port)";  d_port="${d_port:-6343}"
  d_host="$(sflow_conf_value sql_host)";      d_host="${d_host:-127.0.0.1}"
  d_name="$(sflow_conf_value sql_db)";        d_name="${d_name:-switchboard}"
  d_user="$(sflow_conf_value sql_user)";      d_user="${d_user:-switchboard}"
  d_pass="$(sflow_conf_value sql_passwd)"
  d_dbport="$(sed -nE 's/^Environment=PGPORT=([0-9]+)$/\1/p' /etc/systemd/system/sfacctd.service 2>/dev/null | head -1)"
  d_dbport="${d_dbport:-5432}"

  if [[ -f "$SFLOW_CONF" ]]; then
    ok "$SFLOW_CONF exists - its current values are the defaults below"
  fi

  local port
  say ""
  say "  ${C_BOLD}Listen port.${C_RESET} 6343 is the sFlow default and what both Dell OS9"
  say "  and Junos use unless told otherwise. A collector on the wrong port"
  say "  looks exactly like a switch that isn't sending."
  port="$(ask '  sFlow listen port (UDP)' "$d_port")"
  [[ "$port" =~ ^[0-9]+$ ]] && (( port > 0 && port < 65536 )) || die "not a valid port: $port"

  local holder; holder="$(sflow_port_holder "$port")"
  if [[ -n "$holder" && "$holder" != "sfacctd" ]]; then
    die "UDP/$port is already in use by '$holder' - pick another port or stop it first"
  fi

  # Before anything is written: is anything actually sending here? A
  # silent result is not a failure - the switches may not be configured
  # yet, and the guide at the end says how - but knowing now beats
  # discovering it after a clean install that looks perfect.
  if [[ $DRY_RUN -eq 0 && -z "$holder" ]]; then
    say ""
    if confirm "  Listen on UDP/$port for 20s first, to see if anything is already sending?"; then
      sflow_receive_test "$port" 20 || \
        warn "nothing is sending yet - the switch commands are in the guide at the end"
    fi
  fi

  say ""
  say "  ${C_BOLD}Database.${C_RESET} sfacctd writes flows straight into the same Postgres"
  say "  the webui reads - there is no intermediate store and no API between"
  say "  them. These credentials need INSERT on sflow_flows."
  local db_host db_port db_name db_user db_pass
  while :; do
    db_host="$(ask '  Postgres host' "$d_host")"
    db_port="$(ask '  Postgres port' "$d_dbport")"
    db_name="$(ask '  Postgres database' "$d_name")"
    db_user="$(ask '  Postgres user' "$d_user")"
    db_pass="$(ask_secret '  Postgres password' "$d_pass")"

    sflow_pg_test "$db_host" "$db_port" "$db_name" "$db_user" "$db_pass" && break

    # -y means unattended, where re-prompting would loop forever.
    if [[ $ASSUME_YES -eq 1 ]]; then
      warn "continuing anyway (-y) - the collector will start but may not store anything"
      break
    fi
    say ""
    if confirm "  Try different database details?"; then
      d_host="$db_host"; d_dbport="$db_port"; d_name="$db_name"
      d_user="$db_user"; d_pass="$db_pass"
      continue
    fi
    confirm "  Install anyway with details that failed the test?" || die "aborted"
    break
  done

  step "Writing $SFLOW_CONF"
  say "  ${C_DIM}Substituted from the repo's versioned sflow/sfacctd.conf, not"
  say "  written fresh here: that file's comments record why several of those"
  say "  settings are not optional, and a second copy would drift from it.${C_RESET}"
  run mkdir -p /etc/pmacct
  if [[ $DRY_RUN -eq 0 ]]; then
    SB_PORT="$port" SB_HOST="$db_host" SB_DB="$db_name" SB_USER="$db_user" \
    SB_PASS="$db_pass" \
    python3 - "$REPO_DIR/sflow/sfacctd.conf" "$SFLOW_CONF" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
subs = {"sfacctd_port": os.environ["SB_PORT"], "sql_host": os.environ["SB_HOST"],
        "sql_db": os.environ["SB_DB"], "sql_user": os.environ["SB_USER"],
        "sql_passwd": os.environ["SB_PASS"]}
out, seen = [], set()
for line in open(src):
    m = re.match(r"^(\s*)([a-z_]+)(\s*:\s*)(.*)$", line)
    if m and m.group(2) in subs:
        seen.add(m.group(2))
        out.append("%s%s: %s\n" % (m.group(1), m.group(2), subs[m.group(2)]))
    else:
        out.append(line)
# Loud rather than silent: if a key is renamed upstream, the template
# would otherwise be copied with its hardcoded value still in place and
# the installer's answers quietly ignored.
missing = sorted(set(subs) - seen)
if missing:
    raise SystemExit("sflow/sfacctd.conf has no %s line(s) to substitute" % ", ".join(missing))
open(dst, "w").write("".join(out))
PY
    chmod 600 "$SFLOW_CONF"
    ok "wrote $SFLOW_CONF (0600 - it holds the database password)"
    write_commit_marker "$SFLOW_MARKER"
  fi

  # Type=simple with `daemonize: false` in the config, not Type=forking
  # with a PIDFile. The forking form is racy both ways: systemd can read
  # the pid file before pmacct writes it, and a stale one makes it adopt
  # a process it does not own ("Supervising process N which is not our
  # child") - after which the unit reports success while nothing is
  # collecting. Hit live on the NetFlow daemon. This also puts pmacct's
  # own logging in the journal.
  #
  # PGPORT because pmacct's pgsql plugin has no sql_port setting - it
  # hands libpq a NULL port and takes the default. On a non-5432 Postgres
  # that is a connection to nowhere, with the port you carefully entered
  # nowhere in the config.
  write_unit sfacctd <<EOF
[Unit]
Description=sfacctd - sFlow collector for Switchboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PGPORT=$db_port
ExecStart=/usr/sbin/sfacctd -f $SFLOW_CONF
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

  enable_now sfacctd

  if [[ $DRY_RUN -eq 0 ]]; then
    sleep 2
    if [[ "$(sflow_port_holder "$port")" == "sfacctd" ]]; then
      ok "sfacctd is listening on UDP/$port"
    else
      warn "sfacctd is not listening on UDP/$port - check: journalctl -u sfacctd -n 40 --no-pager"
    fi
    sflow_endtoend_test "$port" "$db_host" "$db_port" "$db_name" "$db_user" "$db_pass" || true
  fi
}

# ------------------------------------------------------------ syslog
#
# Vector, receiving syslog from the switches on 514 and shipping parsed
# events to Loki. syslog/vector.yaml in the repo is the source of truth -
# it carries the VRL that turns each vendor's message shape into
# structured fields, and every line of it was derived from real captured
# traffic. This installs Vector and deploys that file; it never writes
# config of its own.

sysl_conf_value() {
  # sysl_conf_value <regex-with-one-group> - pulls a value back out of the
  # deployed config so a re-run offers what is already there.
  [[ -f "$SYSLOG_CONF" ]] || return 0
  sed -nE "s|$1|\1|p" "$SYSLOG_CONF" | head -1
}

syslog_loki_test() {
  # syslog_loki_test <endpoint>
  #
  # Checked before anything is deployed. Vector's loki sink is configured
  # with healthcheck disabled (it has to be - Vector refuses to start if
  # Loki is briefly down, and losing the syslog receiver because the log
  # store blinked is the wrong trade), which means a wrong endpoint here
  # produces a running, healthy-looking Vector that quietly drops every
  # event. That is precisely the failure that went unnoticed for seven
  # days once already.
  local endpoint="${1%/}" out
  step "Testing the Loki endpoint ($endpoint)"
  if ! out="$(curl -fsS --max-time 10 "$endpoint/ready" 2>&1)"; then
    warn "Loki did not answer /ready:"
    printf '      %s\n' "$out" | head -3
    return 1
  fi
  ok "Loki is ready"
  # Push access is the part that matters and /ready does not prove it.
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
          -H 'Content-Type: application/json' -X POST \
          --data '{"streams":[]}' "$endpoint/loki/api/v1/push" 2>/dev/null || echo 000)"
  case "$code" in
    2*|400) ok "the push endpoint accepts writes (HTTP $code)";;
    000)    warn "could not reach $endpoint/loki/api/v1/push"; return 1;;
    *)      warn "push endpoint answered HTTP $code - Vector's events may be rejected"; return 1;;
  esac
  return 0
}

syslog_ingest_test() {
  # syslog_ingest_test <url> <token>
  #
  # POSTs one event to Switchboard's /api/ingest/syslog exactly as Vector's
  # http sink will (a JSON array, the bearer token) and reads the answer.
  # A wrong URL or token here means a healthy-looking sink that Switchboard
  # rejects on every request - the fast path silently not existing.
  local url="$1" token="$2" code body
  step "Testing the Switchboard fast-path endpoint ($url)"
  body="$(mktemp)"
  code="$(curl -s -o "$body" -w '%{http_code}' --max-time 10 \
          -H 'Content-Type: application/json' -H "Authorization: Bearer $token" -X POST \
          --data '[{"message":"install-stack connection test","host":"install-test","timestamp":"1970-01-01T00:00:00Z"}]' \
          "$url" 2>/dev/null || echo 000)"
  case "$code" in
    2*)  ok "Switchboard accepted a test event (HTTP $code)"; rm -f "$body"; return 0;;
    000) warn "could not reach $url"; rm -f "$body"; return 1;;
    401) warn "Switchboard rejected the token (HTTP 401) - it must equal SYSLOG_INGEST_TOKEN in webui.env there";;
    503) warn "Switchboard says the fast path is not configured (HTTP 503) - set SYSLOG_INGEST_TOKEN in its webui.env and restart";;
    *)   warn "endpoint answered HTTP $code:"; head -c 300 "$body" | sed 's/^/      /'; echo;;
  esac
  rm -f "$body"
  return 1
}

syslog_endtoend_test() {
  # syslog_endtoend_test <endpoint>
  #
  # Sends one syslog line at the freshly started Vector and looks for it
  # coming out of Loki. Covers receiver, VRL and sink in one go without
  # waiting on a switch, so a failure is unambiguously ours.
  local endpoint="${1%/}" marker="switchboard-install-$(date +%s)" i found
  [[ $DRY_RUN -eq 1 ]] && return 0
  step "End-to-end test: syslog line -> Vector -> Loki"
  command -v logger >/dev/null 2>&1 || { warn "no logger(1) - skipping"; return 2; }

  # A Dell OS9-shaped line, so the VRL's parsing runs rather than just its
  # passthrough path.
  logger -n 127.0.0.1 -P 514 -d -t "switchboard" \
    "%IFMGR-5-ASTATE_UP: Changed interface Admin state to up: Te 1/37 $marker" 2>/dev/null \
    || { warn "could not send the test line"; return 1; }

  for ((i=0; i<30; i+=3)); do
    sleep 3
    if curl -fsS --max-time 10 --get "$endpoint/loki/api/v1/query_range" \
         --data-urlencode "query={job=\"syslog\"}" \
         --data-urlencode "limit=200" 2>/dev/null | grep -q "$marker"; then
      found=1; break
    fi
  done
  if [[ -n "${found:-}" ]]; then
    ok "the test event reached Loki - receiver, parser and sink all work"
    return 0
  fi
  warn "the test event never reached Loki"
  say "      journalctl -u vector -n 40 --no-pager"
  say "      ${C_DIM}Vector logs each event to the console sink too, so if it appears"
  say "      there but not in Loki, the problem is the sink, not the parsing.${C_RESET}"
  return 1
}

install_syslog() {
  step "Installing the syslog receiver (Vector v$VECTOR_VERSION)"
  ensure_apt curl ca-certificates

  [[ -f "$REPO_DIR/syslog/vector.yaml" ]] \
    || die "no syslog/vector.yaml - run this from a full repo checkout"

  # `|| true` is load-bearing: with `set -o pipefail` a missing vector
  # makes the whole pipeline fail, and a bare assignment failing under
  # `set -e` aborts the installer before it can install the thing whose
  # absence it was checking for.
  local have=""
  have="$(vector --version 2>/dev/null | awk '{print $2}' || true)"
  if [[ "$have" == "$VECTOR_VERSION" ]]; then
    ok "vector $have already installed"
  else
    local tmp=/tmp/vector.deb
    fetch_tarball "https://packages.timber.io/vector/${VECTOR_VERSION}/vector_${VECTOR_VERSION}-1_amd64.deb" "$tmp"
    step "Installing the package"
    # apt, not dpkg -i: the .deb has dependencies and apt resolves them
    # rather than leaving a half-configured package behind.
    run apt-get install -y -qq "$tmp"
    run rm -f "$tmp"
  fi

  local d_loki d_tz loki tz
  d_loki="$(sysl_conf_value '^[[:space:]]*endpoint:[[:space:]]*(http.*[^[:space:]])[[:space:]]*$')"
  d_loki="${d_loki:-http://127.0.0.1:3100}"
  d_tz="$(sysl_conf_value '^.*timezone: "([^"]+)".*$')"
  d_tz="${d_tz:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
  # Overridable from the environment so `-y` can do a real unattended
  # install rather than only accepting whatever the defaults happen to be.
  # Without this, automating a rebuild means either answering prompts over
  # a pty or installing with a Loki address that points nowhere.
  d_loki="${SB_LOKI_ENDPOINT:-$d_loki}"
  d_tz="${SB_DEVICE_TZ:-$d_tz}"
  local d_ingest d_token ingest token
  d_ingest="$(sysl_conf_value '^[[:space:]]*uri:[[:space:]]*(http.*[^[:space:]])[[:space:]]*$')"
  d_ingest="${d_ingest:-http://127.0.0.1:8080/api/ingest/syslog}"
  d_token="$(sysl_conf_value '^[[:space:]]*Authorization:[[:space:]]*"Bearer ([^"]+)".*$')"
  [[ "$d_token" == CHANGE-ME* ]] && d_token=""
  d_ingest="${SB_INGEST_URL:-$d_ingest}"
  d_token="${SB_INGEST_TOKEN:-$d_token}"

  say ""
  say "  ${C_BOLD}Loki.${C_RESET} Where parsed events are shipped. Vector's healthcheck for"
  say "  this sink is deliberately off - it must not refuse to start just"
  say "  because the log store blinked - so a wrong address here gives you a"
  say "  healthy-looking Vector that silently drops everything."
  while :; do
    loki="$(ask '  Loki endpoint' "$d_loki")"
    syslog_loki_test "$loki" && break
    if [[ $ASSUME_YES -eq 1 ]]; then
      warn "continuing anyway (-y) - events may go nowhere"
      break
    fi
    say ""
    confirm "  Try a different endpoint?" && { d_loki="$loki"; continue; }
    confirm "  Install anyway with an endpoint that failed the test?" || die "aborted"
    break
  done

  say ""
  say "  ${C_BOLD}Switchboard fast path.${C_RESET} Besides the archive in Loki, Vector POSTs"
  say "  every parsed event straight to Switchboard as it arrives, so an"
  say "  interface down or a fan fault becomes an event within a second instead of"
  say "  waiting on Loki polling. The token is SYSLOG_INGEST_TOKEN"
  say "  from Switchboard's /etc/switchboard/webui.env. Leave the token blank"
  say "  to skip the fast path (Loki polling still works, just slower)."
  while :; do
    ingest="$(ask '  Switchboard ingest URL' "$d_ingest")"
    token="$(ask '  SYSLOG_INGEST_TOKEN (blank = no fast path)' "$d_token")"
    if [[ -z "$token" ]]; then
      warn "no token - the switchboard_fast sink will be left disabled"
      break
    fi
    syslog_ingest_test "$ingest" "$token" && break
    if [[ $ASSUME_YES -eq 1 ]]; then
      warn "continuing anyway (-y) - the fast path may not work"
      break
    fi
    say ""
    confirm "  Try a different URL or token?" && { d_ingest="$ingest"; d_token="$token"; continue; }
    confirm "  Install anyway with a fast path that failed the test?" || die "aborted"
    break
  done

  say ""
  say "  ${C_BOLD}Device timezone.${C_RESET} Switches send BSD-syslog timestamps with no UTC"
  say "  offset, and Vector's syslog source has no setting for the sender's"
  say "  zone, so it assumes they are already UTC. Getting this wrong shifts"
  say "  every device_timestamp by the offset - silently, and permanently for"
  say "  anything already stored. An IANA name, so DST is handled."
  tz="$(ask '  Timezone the devices report in' "$d_tz")"
  if [[ $DRY_RUN -eq 0 ]] && ! python3 -c "import zoneinfo,sys; zoneinfo.ZoneInfo(sys.argv[1])" "$tz" 2>/dev/null; then
    warn "'$tz' is not a zone this host recognises - using it anyway, but check it"
  fi

  step "Deploying $SYSLOG_CONF from syslog/vector.yaml"
  run mkdir -p /etc/vector
  if [[ $DRY_RUN -eq 0 ]]; then
    SB_LOKI="$loki" SB_TZ="$tz" SB_INGEST="$ingest" SB_TOKEN="$token" python3 - "$REPO_DIR/syslog/vector.yaml" "$SYSLOG_CANDIDATE" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
subs = 0
text, n = re.subn(r'(^\s*endpoint:\s*)http\S+', lambda m: m.group(1) + os.environ["SB_LOKI"],
                  text, count=1, flags=re.M)
subs += n
text, n = re.subn(r'(timezone:\s*")[^"]+(")', lambda m: m.group(1) + os.environ["SB_TZ"] + m.group(2), text)
subs += n
# Loud rather than silent: if these keys are renamed upstream the file
# would otherwise deploy with the previous site's address and timezone
# still baked in, and nothing would say so.
if n == 0 or subs < 2:
    raise SystemExit("syslog/vector.yaml has no endpoint:/timezone: line to substitute")
# The fast path: point the switchboard_fast sink at Switchboard with the
# token, or - no token - remove the sink block so Vector does not retry
# an endpoint forever.
token = os.environ.get("SB_TOKEN", "")
if token:
    text, n1 = re.subn(r'(^\s*uri:\s*)http\S+', lambda m: m.group(1) + os.environ["SB_INGEST"], text, count=1, flags=re.M)
    text, n2 = re.subn(r'(Authorization:\s*"Bearer )[^"]+(")', lambda m: m.group(1) + token + m.group(2), text, count=1)
    if n1 == 0 or n2 == 0:
        raise SystemExit("syslog/vector.yaml has no switchboard_fast uri:/Authorization: line to substitute")
else:
    start = text.find("  switchboard_fast:")
    end = text.find("  loki_sink:")
    if start == -1 or end == -1 or end < start:
        raise SystemExit("syslog/vector.yaml: could not find the switchboard_fast sink block to remove")
    text = text[:start] + text[end:]
open(dst, "w").write(text)
PY
    ok "wrote $SYSLOG_CANDIDATE"
  fi

  # The validate gates everything after it. A past deploy moved the
  # candidate into place and restarted *before* checking the exit code,
  # and a VRL error (E651) took the whole receiver down for a minute.
  step "Validating the config before it goes anywhere near the running service"
  if [[ $DRY_RUN -eq 0 ]]; then
    if ! vector validate "$SYSLOG_CANDIDATE"; then
      rm -f "$SYSLOG_CANDIDATE"
      die "config failed validation - nothing was changed"
    fi
    ok "config is valid"
    if [[ -f "$SYSLOG_CONF" ]]; then
      run cp "$SYSLOG_CONF" "$SYSLOG_CONF.bak-$(date +%Y%m%d%H%M%S)"
    fi
    run mv "$SYSLOG_CANDIDATE" "$SYSLOG_CONF"
  fi

  # 514 is privileged and Vector's package runs it as the `vector` user,
  # so without this the service starts and immediately fails to bind -
  # visible only in the journal.
  run mkdir -p /etc/systemd/system/vector.service.d
  if [[ $DRY_RUN -eq 0 ]]; then
    cat > /etc/systemd/system/vector.service.d/10-switchboard.conf <<'UNIT'
[Service]
# Vector binds 514 (syslog), which is privileged, while the package runs
# it as the unprivileged `vector` user. Granting just this one capability
# is the narrow fix; running the whole daemon as root is not.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
# The config lives in /etc and is read at start; nothing here writes it.
Restart=on-failure
RestartSec=5
UNIT
    ok "wrote the systemd drop-in (CAP_NET_BIND_SERVICE for port 514)"
    write_commit_marker "$SYSLOG_MARKER"
  fi

  enable_now vector

  if [[ $DRY_RUN -eq 0 ]]; then
    sleep 3
    if ss -ulnp 2>/dev/null | grep -q ':514 ' && ss -tlnp 2>/dev/null | grep -q ':514 '; then
      ok "vector is listening on UDP and TCP 514"
    else
      warn "vector is not listening on both 514 sockets:"
      ss -lnp 2>/dev/null | grep ':514 ' | sed 's/^/      /' || true
      warn "check: journalctl -u vector -n 40 --no-pager"
    fi
    syslog_endtoend_test "$loki" || true
  fi
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
    exporter)     install_exporter;;
    sflow)        install_sflow;;
    syslog)       install_syslog;;
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
        say "  one Postgres will double every SSH poll and every event."
        say "";;
      prometheus)
        say "${C_BOLD}prometheus${C_RESET}  http://$ip:9090"
        say "  - Targets: http://$ip:9090/targets (both jobs should read 'up')"
        say "  - Edit /opt/prometheus/prometheus.yml to correct any host:port,"
        say "    then: systemctl reload-or-restart prometheus"
        say "";;
      exporter)
        say "${C_BOLD}exporter${C_RESET}  http://$ip:9101/metrics"
        say "  - Set real device credentials in /etc/s4048-exporter/exporter.env"
        say "  - Point Prometheus's s4048 job at $ip:9101"
        say "";;
      sflow)
        local sport; sport="$(sflow_conf_value sfacctd_port)"; sport="${sport:-6343}"
        say "${C_BOLD}sflow${C_RESET}  sfacctd listening on UDP/$sport at $ip"
        say ""
        say "  ${C_BOLD}1. Point the switches at this collector.${C_RESET}"
        say "  ${C_YELLOW}Run these yourself on the switches - this installer never"
        say "  touches network device configuration.${C_RESET}"
        say ""
        say "  ${C_DIM}Dell OS9${C_RESET}"
        say "    sflow collector $ip agent-addr <switch-ip>"
        say "    sflow enable"
        say "    interface TenGigabitEthernet 1/1"
        say "     sflow enable"
        say ""
        say "  ${C_DIM}Junos${C_RESET}"
        say "    set protocols sflow collector $ip udp-port $sport"
        say "    set protocols sflow agent-id <switch-ip>"
        say "    set protocols sflow sample-rate ingress 1024"
        say "    set protocols sflow interfaces ge-0/0/0"
        say "    commit"
        say ""
        say "  ${C_YELLOW}Both need per-interface enabling.${C_RESET} A global enable alone samples"
        say "  nothing, and reads as fully configured while sending zero packets -"
        say "  which is exactly how this looked when it was first set up here."
        say "  On Junos the config must be ${C_BOLD}committed${C_RESET}: 'show protocols sflow' under"
        say "  [edit] shows the candidate, while operational 'show sflow' still"
        say "  says 'sFlow is not Configured'."
        say ""
        say "  ${C_BOLD}2. Confirm the switches are actually sending.${C_RESET}"
        say "    systemctl stop sfacctd && sudo $0 --test-sflow $sport && systemctl start sfacctd"
        say "  ${C_DIM}Reports the sender IP and the agent-id separately. If they differ,"
        say "  set agent-id to the management IP - otherwise the switch shows up on"
        say "  the Traffic page as an unrecognised agent with no port names.${C_RESET}"
        say ""
        say "  ${C_BOLD}3. Confirm flows are landing in Postgres.${C_RESET}"
        say "  ${C_DIM}Allow one flush interval - rows appear in 60s batches, not instantly.${C_RESET}"
        say "    psql -h <db-host> -U <user> -d <db> \\"
        say "      -c \"SELECT peer_ip_src, count(*), max(stamp_inserted) FROM sflow_flows GROUP BY 1\""
        say ""
        say "  ${C_BOLD}4. Tell the webui where this collector is.${C_RESET}"
        say "  Settings -> Services -> ${C_BOLD}sFlow collector${C_RESET} = $ip:$sport"
        say "  ${C_DIM}Not a connection string - the webui reads flows from Postgres and"
        say "  never contacts sfacctd. It is the address the health panel names when"
        say "  sFlow goes quiet, so 'no flows' comes with somewhere to look. The"
        say "  'sFlow flow' row there reports the age of the newest flow, which is"
        say "  the check that catches a switch silently ceasing to sample.${C_RESET}"
        say ""
        say "  ${C_BOLD}Storage:${C_RESET} roughly 0.08 GB/day at 1:1024 across two switches."
        say "  ${C_DIM}There is no retention policy on sflow_flows by choice - size the"
        say "  disk for how much history you want to keep.${C_RESET}"
        say ""
        say "  ${C_DIM}Config: $SFLOW_CONF (0600)   Logs: journalctl -u sfacctd${C_RESET}"
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
  for b in app monitoring collector ingest all; do
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
  for b in app monitoring collector ingest all; do
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
  --test-sflow [port]   listen on a UDP port and report what sFlow arrives,
                        with each sender's agent-id (default port 6343)
  --dry-run             print what would happen, change nothing
  -y, --yes             accept defaults, no prompts (for automation)
  -h, --help            this

Modules:  ${ALL_MODULES[*]}
Bundles:  app, monitoring, collector, ingest, all

Examples:
  sudo $0                                  # interactive
  sudo $0 --bundle app                     # webui + prometheus on one host
  sudo $0 --bundle collector               # sFlow collector on its own host
  sudo $0 --install syslog                 # Vector syslog receiver -> Loki
  sudo $0 --test-sflow                     # is anything sampling to us?
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
      --test-sflow)
        mode="test-sflow"
        if [[ "${2:-}" =~ ^[0-9]+$ ]]; then SFLOW_TEST_PORT="$2"; shift 2; else shift; fi;;
      --dry-run) DRY_RUN=1; shift;;
      -y|--yes)  ASSUME_YES=1; shift;;
      -h|--help) usage; exit 0;;
      *) die "unknown option: $1 (try --help)";;
    esac
  done

  # Diagnostic, not an install: binds a socket and reads. Root is only
  # needed for the process name in the "port already held by" message.
  if [[ "$mode" == "test-sflow" ]]; then
    command -v python3 >/dev/null 2>&1 || die "python3 is needed for --test-sflow"
    sflow_receive_test "${SFLOW_TEST_PORT:-6343}" "${SFLOW_TEST_SECONDS:-30}"
    exit $?
  fi

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
  confirm "Continue?" || die "aborted"

  local m
  for m in "${SELECTED[@]}"; do install_module "$m"; done

  say ""
  do_detect
  next_steps
}

main "$@"
