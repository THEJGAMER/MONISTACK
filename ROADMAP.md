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
- [x] **There are no tests** (2026-08-01: now 28 of them). The
      `webui/README.md` claim is fixed to describe what's actually
      enforced, not aspirational text - see its "Why this shape" section.
- [x] **Allowlist safety test** (2026-08-01,
      `tests/test_commands_allowlist.py`) — every command on every
      platform (Dell OS9, Junos, OPNsense) is asserted read-only: `show `
      prefix for the two `show`-grammar CLIs, a known-read-only-tool
      allowlist for OPNsense's shell commands (with `pfctl` specifically
      required to be a `-s` subcommand), and a fixed list of config-mode/
      state-changing verbs asserted absent everywhere. Also asserts
      `show running-config` stays excluded and that `COMMAND_TREES` (what
      `/api/run` actually dispatches through) matches the three trees
      tested.
- [x] **Parser tests** (2026-08-01, `tests/test_parsers.py`) — all 8
      listed here, against real output frozen in `tests/fixtures/`,
      captured live from the fleet's actual S4048 for this session (not
      reused from memory/old captures) - including all three real
      transceiver cases (Te 1/37 = genuine 10GBASE-LR optic with DOM, Te
      1/39 = AOC, Te 1/47 = copper DAC, found live by checking every
      Up port's transceiver type rather than guessing which ports would
      have which). One wrong assumption caught immediately by running
      against the real fixture: down ports still have a "Rate info"
      block, just zeros - not absent, as the first draft of that test
      assumed. **Coverage gap**: the topology/trending/OPNsense/Junos
      parsers added elsewhere in this session (ROADMAP 3.4/3.5, OPNsense
      support) have no fixture tests yet - not in this item's original
      scope, but the same treatment would apply.
- [x] **Param-injection test** (2026-08-01, `tests/test_api_run_params.py`)
      — asserts `/api/run` rejects an out-of-whitelist `params` value with
      400 *before* it can reach `_get_session`/SSH (proven with a session
      stub that raises `AssertionError` if ever called on a rejected
      request, not just checking the HTTP status), plus a companion test
      that the identical request shape with a real, server-generated
      value succeeds - confirming the rejection is really about the
      value, not something else failing first. Runs against `app.py`
      directly (dependency-overridden auth, monkeypatched device/session
      globals) - no live Postgres or device needed.
- [x] **VRL transform tests** (2026-08-01, `syslog/tests/test_vrl.py`) —
      installed the real `vector` binary to verify this rather than guess
      the CLI syntax (`vector vrl -p <program> -i <events.jsonl> -q -o`,
      confirmed live: one JSON result per line on stdout, vector's own
      log noise confined to stderr). Extracts the actual
      `interpret_switch_event` VRL source straight out of `vector.yaml`
      and runs it against real captured syslog messages - covers the
      major-alarm, alarm-recovery, interface-link-down, and the
      "cleared" vs "major alarm" text-ordering regression cases
      documented in `vector.yaml`'s own comments. Skips cleanly if
      `vector` isn't installed locally.
- [x] CI (2026-08-01, `.github/workflows/ci.yml`) — three jobs on every
      push/PR: `webui-tests` (pytest), `syslog-vrl` (installs Vector,
      runs `vector validate --no-environment` + the VRL tests -
      `--no-environment` skips the CI-irrelevant missing-`/var/lib/vector`
      check while still catching real config/schema errors, confirmed
      live with a deliberate typo before relying on it), and
      `frontend-build` (`npm ci && npm run build`).

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
- [x] `/healthz` and `/readyz` endpoints (2026-08-01) — unauthenticated
      (an orchestrator's probe/a Prometheus scrape doesn't carry basic-auth
      creds, same as `/api/setup/status` already was). `/healthz` is
      liveness only (process can answer at all); `/readyz` actually checks
      Postgres with `SELECT 1` rather than trusting whatever `DB`/`STORE`
      were at startup. Wired into `webui/Dockerfile` as a `HEALTHCHECK`
      (Python's own `urllib`, no new package). Loki/switch reachability
      deliberately isn't part of either check - those already degrade
      gracefully per-request, and killing this container over one
      unreachable switch would be wrong.
- [x] Export the webui's own metrics (2026-08-01) — new `metrics.py`
      (`prometheus_client`, same library the exporter already uses) with
      poll success/failure/duration per device, SSH reconnects per host,
      Loki query latency/failures, and command run count/duration per
      device, served at `/metrics` and added as a second Prometheus scrape
      target (`prometheus/prometheus.yml`) alongside the exporter's own
      `s4048` job. Verified live: all three real devices' poll metrics and
      a real `/api/run` call's command metrics showed up correctly, and
      the new `switchboard` Prometheus target came up healthy.
- [x] Structured (JSON) logging with a request/correlation ID (2026-08-01)
      — new `logging_setup.py`: a contextvar set once per request by a new
      FastAPI middleware, read back by a logging filter so every log line
      a request touches carries the same `request_id` with no parameter
      threading required - confirmed live this reaches synchronous code
      deep inside a route (`ssh_client.py`'s own logging) since Starlette/
      anyio copy the calling context into the thread pool sync handlers
      run on. Echoed back as an `X-Request-ID` response header too.
      `LOG_FORMAT=json` (default) or `text` (human-readable, for local
      debugging) via env var. **Note:** this changes `docker logs` output
      from plain text to one JSON object per line.

### 0.5 Resilience and graceful degradation
- [x] Audit behavior when Loki is unreachable, the switch is unreachable, or
      the DB is locked (2026-08-01). Findings: `/api/syslog` and
      `/api/devices/{id}/alarm-history` already caught `LokiError` cleanly
      (502 with a real message); `status_poller.py` already catches per-
      device; `_fetch_live_topology()` already tolerates per-device SSH
      failures. The real gap was `db.py`-backed stores
      (`store.py`/`results_store.py`/`topology_store.py`) — none of them
      catch anything, and `Database._with_reconnect` only absorbs a single
      dropped connection, not a sustained outage, so a genuinely-down
      Postgres would propagate a raw `psycopg2.Error` into FastAPI's
      generic 500 (traceback, no clear message). Fixed with two global
      `app.exception_handler`s (`psycopg2.Error` → 503 "Database
      unavailable: ...", `SwitchSSHError` → 502) as a safety net beneath
      routes that don't already catch these locally — deliberately not
      route-by-route try/except, since that would mean re-proving the same
      fix at every call site instead of once.
- [x] Global SSH concurrency guard (2026-08-01) — `ssh_client.py` gates
      the actual handshake (`_connect_once`, not steady-state `run()`
      traffic) behind a module-level `threading.Semaphore(4)` shared
      across every `SwitchSSH` instance, with a bounded 30s wait (times
      out with a clear error rather than blocking forever - trading one
      hang for another would defeat the point). This is a different risk
      than the vty-exhaustion incident the per-device lock already fixed
      (see webui README "Session model") - that was one device drowning in
      *repeated* connection attempts; this is a growing fleet's *pollers
      all reconnecting at once* after a shared blip (network hiccup,
      container restart) putting a connection-attempt spike on the webui
      host and anything shared upstream (jump host, VPN, firewall
      connection tracking), even though no single device's own vty pool is
      at risk. Verified live: normal startup (3 devices reconnecting
      concurrently) is unaffected since it's well under the cap.
- [x] Timeout/retry policy review across SSH, Loki, and the frontend
      (2026-08-01). SSH: `connect()` already retried; `run()` now retries
      once too (reconnect + re-send) — safe unconditionally since every
      command this app sends is read-only. Loki: `query_range()` had zero
      retry; now retries once (0.5s backoff) before raising `LokiError`.
      Frontend: `api()` had no client-side timeout at all (an indefinite
      spinner on a truly hung request, e.g. a dropped connection the OS
      never notices) — now wrapped in an `AbortController` with a clear
      "Request timed out after Ns" error; 60s default, 180s for
      `/api/topology` specifically (it runs several sequential SSH
      commands per device across the whole fleet, not just one device).

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
- [x] **Per-platform command trees — done 2026-07-31, for OPNsense.** Added
      a real OPNsense 26.1 firewall (root SSH, verified live), architecturally
      the odd one out among the three platforms: SSH lands in the console's
      numbered menu, not a shell at all - `ssh_client.py`'s
      `_connect_opnsense` sends `8` ("Shell") to reach a real FreeBSD shell,
      whose prompt (`root@host:~ #`) needed its own per-instance prompt
      regex (`self._prompt_re`, replacing what used to be a single
      module-level `PROMPT_RE` constant `run()` assumed everywhere) since
      it has a space before the `#` that the Dell/Junos pattern doesn't
      expect. Commands are plain FreeBSD CLI (`ifconfig`, `netstat`,
      `pfctl -s ...`), not a vendor `show` grammar. New `commands.py`'s
      `OPNSENSE_COMMAND_TREE`, `opnsense_parsers.py` (built from real
      captured output), `status_poller.py`'s `_poll_once_opnsense`,
      summaries. Deliberately **not** wired into Front Panel (a firewall
      appliance has no switch-chassis port layout to illustrate - faking
      one would violate this app's own rule against fabricating hardware),
      Topology/LLDP (`/api/topology` now explicitly skips any platform
      with no LLDP command rather than showing it as a permanently-isolated
      node), or Syslog/Alarm History (no remote syslog configured for it
      yet, same gap as the Juniper device).
- [ ] Replace hand-rolled paramiko + regex with Netmiko/Scrapli +
      ntc-templates/TextFSM for parsing across vendors.
- [ ] Normalize parsed output into a vendor-neutral shape so the UI and
      alerting don't care what's underneath.

### 3.4 Predictive / trending
- [x] **Optic degradation trending** (2026-08-01, Junos added 2026-08-01) —
      Rx/Tx power and temperature, sampled every 5 min (status poller's
      slow/transceiver cadence) into a new `metric_samples` Postgres table
      (see `trending.py`). A trend alert fires when current Rx power has
      declined ≥3dB from its peak in the window (a halving of optical
      power - the standard early-warning threshold), independent of the
      device's own absolute low-power alarm flag. Dell OS9 via per-port
      `show interfaces <port> transceiver`; Junos via one bulk `show
      interfaces diagnostics optics` round trip (`junos_parsers.
      parse_junos_optics_diagnostics`) - confirmed live, but this fleet's
      real EX3300 has no actual optical transceiver installed (its
      populated SFP+ ports are 10GBASE-CU1M DACs, which correctly report
      "N/A"), so **the code path for a real populated optic on Junos is
      unverified** - the field-level parsing for that case is deliberately
      left unimplemented (returns nothing rather than guessed numbers)
      pending a live capture against an actual Junos optical module.
      OPNsense still excluded (firewall appliance, no pluggable optics).
- [x] Port utilization and error-counter trending; capacity forecasting
      (2026-08-01, Junos added 2026-08-01) — interface Mbps and cumulative
      input/output error+discard counts. Dell OS9 via `parsers.
      parse_interfaces_errors`, reusing the same bare `show interfaces`
      output already fetched for rates (no extra SSH round trip). Junos
      via one bulk `show interfaces extensive` round trip on the slow
      cadence only (`junos_parsers.parse_junos_interfaces_errors` /
      `parse_junos_interfaces_traffic_mbps`) - Junos's fast poll (`show
      interfaces terse`) carries no rate/error data at all, so unlike Dell
      there's nothing to piggyback on. Capacity forecast is a simple
      linear regression toward 90% of the port's own negotiated link
      speed, surfaced as "expected to reach capacity in about N day(s)" -
      not a guarantee, just a rough current-trend projection.
- [x] PSU power draw trending (2026-08-01) — `power_watts`/
      `avg_power_watts` were already parsed per PSU bay; now sampled on
      the same cadence, with a "notable change" flag when the latest
      reading deviates ≥20% from its own trailing baseline (either
      direction - no single "good" direction for this one, unlike optic
      power). Dell OS9 only - confirmed live this fleet's Junos EX3300 has
      no CLI command exposing PSU wattage at all (`show chassis power` /
      `show chassis environment pem` both error "not valid on the
      ex3300-48p"); not a code gap, there's genuinely nothing to sample.
- New "Trends" nav page: device + metric/port + time-range pickers, a
  `LineChart`, and the threshold-alert/forecast banner when one fires.
  Backed by `GET /api/devices/{id}/trends` (series discovery) and
  `GET /api/devices/{id}/trends/{metric}` (samples + evaluation).
- Samples are pruned after 90 days (daemon thread, once/day) so the table
  doesn't grow unbounded.

### 3.5 Topology
- [x] LLDP neighbors are already collected — build a live topology graph
      across the fleet. New `webui/topology.py` matches each device's own
      half-view of a link (parsed by `parsers.parse_lldp_neighbors_detail`
      for OS9, `junos_parsers.parse_junos_lldp` for Junos) into one edge by
      *mutual port-name corroboration* rather than chassis ID - Dell OS9
      has no CLI command that reports its own chassis ID (`show lldp
      local-info`/`local-information` both 404), so there's no direct way
      to ask a Dell device its own identity the way Junos's `show lldp
      local-information` answers for itself. New `GET /api/topology`
      fetches live from every device (partial-failure tolerant per
      device), new Topology page (SVG diagram + a Links table) added to
      the left nav. Verified against the real fleet: correctly reconstructs
      the known Dell↔Juniper LACP bundle (`ae1`, 2 members) plus the
      separate out-of-band mgmt link, and surfaces the Dell's other real
      LLDP neighbors (an AP, a NIC) as external/unmanaged nodes.
- [x] **Multi-source discovery + MAC/ARP host discovery — done 2026-08-01.**
      Topology is now explicitly a *multi-discovery-type* graph, not just
      an LLDP one - every edge carries `discovered_via` (`["lldp"]`,
      `["mac-table"]`, or both when corroborated). New parsers
      (`parsers.parse_arp`/`parse_mac_address_table` for OS9,
      `junos_parsers.parse_arp`/`parse_ethernet_switching_table` for
      Junos, `opnsense_parsers.parse_arp` for OPNsense) feed two new
      passes in `topology.py`: (1) every device's ARP table merges into
      one MAC→IP map so external neighbors show a real IP instead of a
      bare MAC wherever the fleet's own ARP already knows it (confirmed
      live, including the case where a multi-port NIC's LLDP chassis ID
      isn't the MAC ARP actually has - falls back to the port ID); (2) MAC/
      switching-table entries become their own discovered hosts when LLDP
      never reported anything at all for that MAC - the explicit "use MAC
      as fallback" case, since most consumer/IoT devices and unmanaged
      switches never speak LLDP. Aggregate interfaces (Dell `Po *`, Junos
      `ae*`) are deliberately excluded from this pass - their
      mac-address-table entries are often transit traffic through an
      uplink bundle, not directly-attached hosts, and there's no clean way
      to reconcile which physical LAG member a learned MAC actually
      arrived on. Capped per port (4 shown + an overflow summary edge) -
      a real hub port in this fleet had 14 hosts on it. Baseline drift
      tracking deliberately only follows LLDP-backed edges (a MAC-table
      host coming and going, e.g. a guest laptop, would otherwise be
      constant false "infra changed" noise).
- [x] Overlay link state / utilization on the topology - reuses the
      already-running status poller's interface data (no extra SSH round
      trip): link state (up/down/unknown) colors each edge, and OS9's real
      input/output Mbps (`status_poller.py`'s rolling ~299s average) shows
      in the Links table. Junos has no per-interface rate data today (see
      §3.3's known issues), so its edges show state only, honestly, not a
      fabricated number.
- [x] **Baseline topology + drift detection** — new `webui/topology_store.py`
      (Postgres-backed, one row) saves the current graph as structural
      "edge signatures" (`topology.edge_signature`/`diff_against_baseline`
      - device/port identity only, no state/utilization, since that's not
      what "did the wiring change" means). `POST /api/topology/baseline`
      is the "Relearn" button (full overwrite); `POST
      /api/topology/baseline/accept` folds in *specific* added/removed
      edges by hand without discarding the rest - both exposed in the
      Topology page's new Baseline panel, alongside per-item and
      "accept all" actions and a confirm-modal-gated "Forget baseline".
      Verified live: induced a fake drift by editing the stored baseline
      directly in Postgres, confirmed the UI correctly showed one "new"
      and one "missing" edge, and that per-item Accept resolved it.
- [ ] **Historical/trend view for links** — same idea as Alarm History,
      but for link state: pull from Loki (or a new poller-recorded table)
      to show "this link flapped 3 times last week" instead of only ever
      showing current state.
- [x] **Background polling + flap alerts** — lighter than a true
      always-on server poller: the Topology page auto-refreshes every 30s
      (toggle in the UI) and diffs each fetch's per-edge link state against
      the previous one client-side, firing a Flashbar the moment a link
      goes down or recovers. Approximation worth calling out: this only
      catches a flap while the Topology page is open in a browser, unlike
      `status_poller.py`'s always-running per-device thread - a real
      always-on version would need its own background poller + a push
      channel (websocket/SSE) to notify a closed browser tab, which felt
      like more surface than this feature warranted yet.
- [x] **Click a node to jump to its Console tab** - `App.jsx` lifts
      `preselectDeviceId` state so a node click on Topology sets it and
      switches to Console, which consumes it to preselect that device.
      Verified live via Playwright screenshot.
- [x] **"Add this device" shortcut from an external/unmanaged neighbor** -
      clicking an external node navigates to Devices and opens "Add a
      device" pre-filled with the LLDP-derived label (e.g. a NIC's chassis
      description) as the suggested name; host is left blank for manual
      entry, since LLDP doesn't reliably give a management IP here.
      Verified live via Playwright screenshot.
- [x] **LACP bundle health rollup** - `app.py`'s `_lag_health()` groups
      internal edges by (device, LAG name) and flags a bundle `degraded`
      when its members disagree on link state; surfaced as a warning
      Alert above the Topology diagram. Verified against the real fleet's
      `ae1` bundle (both members up, correctly not flagged).

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
- **Junos transceiver diagnostics ARE now polled automatically** (2026-08-01,
  see 3.4) via a bulk `show interfaces diagnostics optics` on the slow
  cadence, feeding both the Front Panel hover data and optic trending -
  but only the "not present" (N/A) case is parsed/trusted; a real
  populated optical module's DOM reading fields are unverified on real
  Junos hardware (this fleet's EX3300 has none installed) and
  deliberately not guessed at. Re-verify against a live capture before
  trusting numbers there.
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
