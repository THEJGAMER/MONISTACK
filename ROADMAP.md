# MONISTACK Roadmap

Path from "working single-switch monitoring stack" to something a DC team
relies on daily.

**Current state (honest):** a genuinely functional stack — SSH-polling
Prometheus exporter, Vector→Loki syslog pipeline with a Dell OS9
interpreter, Grafana dashboards, and Switchboard (the read-only command
console: 40 allowlisted `show` commands, live status polling, front-panel
visual, per-device syslog, alarm history, auto-saved results in SQLite).
Everything in it has been tested against real hardware.

**But:** it's built around *one* switch, trusts *one* shared password, has
*zero* automated tests, and *zero* git commits. The bones need work before
the fleet features are worth building.

Ordering below reflects that: foundations → identity → scale → features.

---

## Phase 0 — Foundations ("bones")

Nothing here is user-visible. All of it is what makes the rest safe to
build on.

### 0.1 Version control — **do this first**
- [ ] The repo has **zero commits**. Everything so far exists only as
      working-tree files. One `rm -rf` from oblivion.
- [ ] Initial commit + sensible history going forward.
- [ ] `.gitignore` is already correct (`.env`, `webui/data/`,
      `node_modules/`, `dist/` excluded) — verify nothing secret is staged
      before the first push.
- [ ] Decide on a remote (or deliberately keep it local-only and back up
      the bare repo).

### 0.2 Test suite + CI
- [ ] **There are no tests.** Note that `webui/README.md` currently claims
      the read-only allowlist is "enforced by a test, not just convention"
      — that claim is **false today** and should be made true (or removed).
- [ ] **Allowlist safety test** (highest value, ~10 lines): assert every
      `cmd` in `commands.py` starts with `show ` and that no entry contains
      config-mode verbs. This is the single test that protects the core
      security property of the whole tool.
- [ ] **Parser tests** against the real captured outputs already used
      during development: `parse_environment`, `parse_interfaces_status`,
      `parse_interfaces_description`, `parse_transceiver` (optical vs
      AOC vs DAC), `parse_interfaces_rates`, `parse_alarms`, `parse_cpu`,
      `parse_memory`. Freeze real device output as fixtures so a parser
      regression is caught without needing the switch.
- [ ] **Param-injection test**: assert `/api/run` rejects a `params` value
      outside the server-generated whitelist (this was verified by hand
      during development; it should be a test).
- [ ] **VRL transform tests** for `syslog/vector.yaml` — `vector vrl` can
      run the transform against fixture events in CI (this is how the
      alarm-severity logic was validated by hand; automate it).
- [ ] CI (GitHub Actions or equivalent): run tests + `vector validate` +
      a frontend build on every push.

### 0.3 Data durability and growth
- [ ] **Results grow unbounded.** Every `/api/run` auto-saves a row; the
      only deletion path is a manual click. At fleet scale with scheduled
      polling this is a slow-motion disk-fill. Add retention (age- and/or
      count-based), a prune job, and a documented policy.
- [ ] **No DB backup.** `switchboard.db` lives on a single Docker volume.
      Host dies → device inventory and all saved results are gone. Add a
      scheduled dump (`sqlite3 .backup`) to somewhere off-host.
- [ ] Add DB schema migrations (currently `CREATE TABLE IF NOT EXISTS`
      only — fine today, painful the first time a column changes).

### 0.4 Self-observability
- [ ] `/healthz` and `/readyz` endpoints so an orchestrator can actually
      tell if the app is alive vs. wedged.
- [ ] Export the webui's *own* metrics: poll success/failure counts per
      device, SSH reconnect count, poll duration, Loki query latency,
      command run count/latency. It's a monitoring tool that currently
      can't be monitored.
- [ ] Structured (JSON) logging with a request/correlation ID, so a
      command run can be traced end to end.

### 0.5 Resilience and graceful degradation
- [ ] Audit behavior when Loki is unreachable, the switch is unreachable,
      or the DB is locked — some paths already handle this well
      (`summarize()` can never raise; `status_poller` catches per-device),
      others are less proven.
- [ ] Global SSH concurrency guard. Dell OS9 has very few vty slots — this
      already bit us once (see webui README "Session model"). Today's
      per-device lock is right for one device; a fleet needs a global
      semaphore and backpressure.
- [ ] Timeout/retry policy review across SSH, Loki, and the frontend.

### 0.6 Frontend hygiene
- [ ] Bundle is ~1.03 MB JS / ~1.14 MB CSS (Vite warns; grew from ~996 KB
      after adding `@cloudscape-design/board-components` for the Console's
      dynamic board). Code-split by route/panel so first paint isn't the
      whole app.
- [ ] Pagination is client-side (`useClientPagination`) — the API returns
      everything and the browser slices it. Fine at 200 rows, wrong at
      50k. Move to server-side pagination for results and syslog.
- [ ] Add a favicon (currently a 404 on every page load — cosmetic, but
      it's the only console error in the app and it masks real ones).

### 0.7 Container and supply chain
- [ ] Containers run as root; use a non-root user + read-only rootfs.
- [ ] Pin dependency versions (currently ranged) and generate an SBOM.
- [ ] Image vulnerability scanning in CI.

---

## Phase 1 — Identity, secrets, and audit

The gate on anyone other than you using this.

- [ ] **Per-user identity** — OIDC/SAML/LDAP, or at minimum real local
      accounts. Today it's one shared basic-auth credential.
- [ ] **RBAC** — who may run which command category against which device
      (or device group). Read-only-viewer vs operator vs admin.
- [ ] **Real audit trail** — every command attributed to a real human,
      append-only, exportable. Today's `log.info("user=admin ...")` logs
      the shared account, which is not an audit trail.
- [ ] **TLS** — the webui is plain HTTP today.
- [ ] **Secrets management** — switch credentials currently sit in
      plaintext in `.env` and in the SQLite device store, protected by
      filesystem permissions only. Move to Vault / cloud secret manager /
      SOPS, or per-device SSH keys in a real keystore.
- [ ] **Credential rotation** — including rotating the credentials used
      throughout this project's development (`.env` still holds the
      placeholder `admin` / `changeme-webui`, and the switch password has
      been shared in plaintext).
- [ ] Session management, CSRF protection, rate limiting on auth.

---

## Phase 2 — Scale to a fleet

- [ ] **Poller architecture.** Today: one OS thread + one persistent SSH
      session per device, all inside one uvicorn process. Fine at 5
      devices; it will not hold at 200+. Move to a worker pool / job queue
      (arq, RQ, Celery) with bounded concurrency.
- [ ] **Multi-device exporter.** `exporter/exporter.py` is hardcoded to a
      single switch via `SWITCH_HOST`/`SWITCH_USER`/`SWITCH_PASS` env
      vars. It should read the same device registry the webui uses.
- [x] **Postgres instead of SQLite** — done 2026-07-30. `webui/db.py` now
      connects to Postgres via `DATABASE_URL`; `store.py`/`results_store.py`
      unchanged in shape (same public methods, `?` → `%s` placeholders).
      Single-connection-behind-a-lock design kept as-is (still correct at
      this request volume) with one addition: a retry-once-after-reconnect
      wrapper, since a network DB can drop a connection in ways a local
      SQLite file never could. Existing data (54 saved results, 0 UI-added
      devices) migrated automatically on first startup via
      `_migrate_legacy_sqlite()` in `app.py`, mirroring the existing
      `_migrate_legacy_json_devices()` idiom. See `webui/README.md`
      "Storage" section for the encoding gotcha this surfaced (the Postgres
      cluster was `SQL_ASCII` by default, not UTF8 - crashed on the first
      migrated row until the database was recreated with
      `ENCODING 'UTF8' TEMPLATE template0`).
- [ ] **Streaming telemetry** — gNMI/gRPC or SNMP traps where hardware
      supports it, instead of polling `show` commands over SSH. SSH
      polling was the right call for *this* switch; it doesn't scale as
      the primary mechanism for a fleet.
- [ ] **Device grouping** — sites, racks, roles, tags — plus filtering and
      per-group views throughout the UI.
- [ ] Loki/Prometheus retention and cardinality review before fleet-scale
      ingest.

---

## Phase 3 — Network-ops features that earn daily use

### 3.1 Config backup, versioning, drift detection — *highest value*
- [ ] Scheduled config pulls into a git-backed store, with per-device
      history and diffs.
- [ ] Drift detection and alerting against last-known-good.
- [ ] Requires solving the secrets problem that `show running-config` was
      deliberately excluded for (SNMP communities, local user hashes) —
      scrub or encrypt at rest rather than continuing to avoid it.

### 3.2 Alerting
- [ ] Wire alarms, link flaps, PSU/fan faults, and optic degradation to
      Alertmanager (already in the stack) → Slack/PagerDuty/email.
- [ ] Maintenance windows / alert suppression.
- [ ] Alarm acknowledgement and assignment.

### 3.3 Multi-vendor
- [x] **Per-platform command trees — done 2026-07-30, for Junos.** Added
      a real Juniper EX3300-48P (root SSH, verified live), with its own
      command tree (`commands.py`'s `JUNOS_COMMAND_TREE`), login flow
      (`ssh_client.py` - Junos lands in a FreeBSD shell, needs `cli`, no
      enable concept, its own `---(more)---` pagination), parsers
      (`junos_parsers.py`, built from real captured output), status
      polling (`status_poller.py`'s `_poll_once_junos`), summaries, and an
      accurate Front Panel illustration (`chassisProfiles.js`'s
      `ex3300-48p`, traced against real reference photos). Devices with
      any other "Operating System" selection (Cisco/Arista/NX-OS/Other)
      are still "(experimental)" - saved and reachable over SSH, but with
      no command tree wired up, so the Console shows an empty Commands
      panel rather than guessing at syntax.
- [ ] Replace hand-rolled paramiko + regex with Netmiko/Scrapli +
      ntc-templates/TextFSM for parsing across vendors.
- [ ] Normalize parsed output into a vendor-neutral shape so the UI and
      alerting don't care what's underneath.

### 3.4 Predictive / trending
- [ ] **Optic degradation trending** — Rx/Tx power and temperature are
      *already collected* per port. Trending them and alerting on gradual
      Rx-power decline predicts failing optics before links drop. Mostly
      built; needs the trend + threshold logic.
- [ ] Port utilization and error-counter trending; capacity forecasting.
- [ ] PSU power draw trending.

### 3.5 Topology
- [ ] LLDP neighbors are already collected — build a live topology graph
      across the fleet.
- [ ] Overlay link state / utilization on the topology.

### 3.6 Operational workflow
- [ ] **Bulk operations** — run a command across N devices, with a
      collated diff-style result view.
- [ ] **Scheduled/recurring runs** with saved output (feeds config backup
      and compliance).
- [ ] Compliance checks — assert fleet-wide invariants (NTP configured,
      expected VLANs present, no unexpected admin-down uplinks).
- [ ] Export results as CSV/JSON, not just Markdown.

---

## Phase 4 — Nice to have

- [ ] Saved/favorite commands and per-user command history.
- [ ] Full-text search across saved results.
- [ ] API tokens + a documented public API (OpenAPI is auto-generated by
      FastAPI but unversioned and undocumented).
- [ ] Webhooks for external integration.
- [ ] Mobile/responsive pass (the front panel especially).
- [ ] Per-device notes/runbook links.
- [ ] Dark mode is done; consider a high-contrast/accessibility pass.

---

## Known issues / tech debt

- **The Juniper EX3300 has no Syslog/Alarm History data** - deliberate
  scope decision (2026-07-30), not a bug: enabling it needs a config
  write on the device itself (`set system syslog host ...`) that wasn't
  authorized. Switch Status still shows live alarms via `show chassis
  alarms`/`show system alarms` polling, just no historical log.
- **The Juniper EX3300's own clock is drifted** (`show system uptime`
  showed April 2026 while it was really July 2026, found live during
  Junos support work). Not fixed here - out of scope for a monitoring
  tool to change production device config - but worth knowing before
  trusting any Junos-side timestamp (device_timestamp-equivalent) at
  face value, unlike the Dell switch's NTP-synced clock.
- **Junos transceiver diagnostics aren't polled automatically.**
  `status_poller.py`'s slow-cadence transceiver poll only has a Dell OS9
  implementation; the Junos command (`show interfaces diagnostics optics
  <port>`) is available manually from the Console but not wired into the
  background poll or the Front Panel hover data yet.
- **Junos memory/CPU numbers in Switch Status are derived, not exact.**
  `show chassis routing-engine` reports `DRAM 1024 MB` + a single
  `Memory utilization N percent`, not exact used/free byte counts like
  Dell's `show memory` - `status_poller.py` computes used/free from those
  two real numbers rather than fabricating them, and CPU only has one
  current data point (no rolling 5sec/1min/5min average like Dell), so
  the Switch Status tab's three-tier CPU display shows the same number
  three times for Junos devices.
- **`webui/README.md:80` claims the read-only allowlist is "enforced by a
  test, not just convention."** There are no tests. Fix the claim or write
  the test (see 0.2).
- **`parse_alarms` populated-row format is inferred, not verified.** The
  switch had no active alarms, and inducing one on production hardware
  wasn't appropriate. The "no alarms" path is verified against real
  output; the populated path is inferred from the header column layout.
  Validate against a real alarm when one occurs.
- **Alarm history classification moved into the webui backend
  (`_classify_alarm` in `app.py`), not just Vector.** Originally the
  `/api/devices/{id}/alarm-history` endpoint only trusted
  `alarm_severity`/`alarm_component` fields stamped by Vector at
  ingestion - which meant the Vector alarm-normalization fix having
  silently never reached the LXC (written and tested 2026-07-30 but the
  redeploy step was skipped) left the tab permanently empty with no
  error anywhere, and events ingested before that fix would never be
  recoverable at all. Fixed by reclassifying from each event's raw
  `facility`/`detail` fields in Python instead (mirroring the same VRL
  logic), so the feature no longer depends on Vector having tagged
  anything correctly, and 33 real historical fault/recovery events were
  recovered immediately. See `syslog/README.md` changelog.
- **Recovery messages containing "alarm cleared" were briefly
  misclassified as new active critical alarms** (the `contains(...,
  "major alarm")` check also matched "Major alarm cleared: ..."). Fixed
  same day by checking for "cleared" first, in both the VRL transform
  and its Python mirror. `alarm_component` extraction for cleared
  messages is a best-effort text strip, not correlated back to the
  original fault's exact component string - a resolved alarm shows as
  its own (inactive) row rather than closing out the original row.
- **`device_timestamp` was 10 hours fast for every syslog/alarm event**
  until 2026-07-30 (~08:01 UTC). The S4048's clock runs in local time
  (Sydney) and sends BSD-syslog timestamps with no UTC offset on the
  wire; Vector's `syslog` source has no way to be told the sender's zone
  and assumed UTC, so displayed times were the switch's local wall-clock
  digits mislabeled as UTC (then converted *again* to browser-local on
  top, compounding the error). Fixed in `process_syslog` by
  reinterpreting the parsed digits as `Australia/Sydney` via real
  IANA tzdata (not a hardcoded offset, so it survives DST). **Only fixes
  events ingested after the fix** - Loki log lines are immutable, so
  anything already stored (including everything currently in Alarm
  History) permanently keeps its old, 10-hours-fast timestamp. See
  `syslog/README.md` changelog.
- **Removed-bay detection is per-process.** `status_poller` remembers seen
  (unit, bay) pairs in memory; a webui restart forgets that a bay was ever
  populated, so a still-removed PSU reverts to simply not being listed
  until it reappears. Persist known bays to survive restarts.
- **`.env` still holds placeholder credentials** (`admin` /
  `changeme-webui`), real switch credentials, and now `DATABASE_URL` with
  temporary Postgres credentials (`claude`/`claude`) - all in plaintext.
- **Vector's `loki_sink` runs with
  `dangerously_allow_unconfined_template_resolution: true`** — Vector
  itself warns on every start that a log producer controlling a templated
  field could write to arbitrary label keys. Pre-existing, worth revisiting.
- **Frontend has no tests** and no linting configured.
- **Loki's storage was configured under `/tmp/loki`** on its host
  (192.168.0.145) — `/tmp` there is `tmpfs` (RAM-backed), so every reboot
  silently wiped all log/alarm history with no error anywhere. This is how
  the 33 historical hardware alarm events recovered on 2026-07-30 (see
  Alarm History fix above) were then permanently lost a few hours later
  when that LXC restarted for an unrelated reason. Fixed same day: moved
  `path_prefix`/`chunks_directory`/`rules_directory` in
  `/etc/loki/loki-config.yaml` to `/var/lib/loki` (the host's real
  ZFS-backed root, 7.5G free) and verified data survives a restart.
  Dated backup of the old config left at
  `/etc/loki/loki-config.yaml.bak-20260730154409` on that host. Same class
  of risk applies to Prometheus/Grafana if their storage is ever pointed
  at `/tmp` on any host - worth auditing.
