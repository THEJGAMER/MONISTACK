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

### 0.1 Version control — **done**
- [x] **Repo under version control** (closed 2026-08-23). This section
      described a working tree with zero commits, "one `rm -rf` from
      oblivion". Long since false and left stale, which is its own small
      hazard - a roadmap that states things that aren't true stops being
      read. At close: **52 commits** on `main`.
- [x] Initial commit + sensible history going forward.
- [x] `.gitignore` verified - `.env`, `webui/data/`, `node_modules/`,
      `dist/` excluded, and no secret has been staged (checked again
      before the Junos fixtures were committed: real device output, but
      no credentials).
- [x] **Remote decided**: `github.com/THEJGAMER/MONISTACK`. Deployment
      pulls from it (see docs/deploy-lxc-4lxcs-native.md), so the remote
      is load-bearing rather than just a backup.

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
- [x] **Retention for every growing table - done 2026-08-12.** New
      `webui/retention.py` owns one policy table for all of them, pruning
      at startup and then daily.

      The starting point turned out to be worse than the item described.
      `metric_samples` was supposedly covered by `trending.py`'s 90-day
      prune, but its only caller slept 24 hours *before* its first run and
      nothing called it at startup - so on a webui redeployed several times
      a day it had realistically never run. Found at **2.03M rows / 493
      MB**, roughly 3x its size six days earlier, while its own docstring
      claimed it was "called once at startup and then daily".

      Fixing that exposed a second, subtler version of the same bug, and
      only live testing caught it: the corrected loop still didn't prune at
      boot, because the thread starts at import time ~500 lines before
      `DB` exists, saw `DB is None`, and slept a full day - the identical
      "never runs" outcome reached a different way. Confirmed by planting a
      400-day-old row, restarting, and finding it still there. Now polls
      briefly until the database is configured, then falls into the daily
      cadence; re-tested the same way, the row was gone ~65s after boot.

      Age alone turned out not to be the answer either. A dry run against
      real data showed **zero rows** would be deleted at any sane window,
      because nothing is old enough yet - while the table grows ~185k
      rows/day. Measuring where that comes from: the four per-port
      interface series across ~105 ports were **1.91M of 2.03M rows
      (93.9%)**, most of them permanently-unused ports - the same
      observation that made interface *alerting* opt-in per port, except
      trending records every port regardless. `metric_samples` is
      therefore split into two policies (interface 30d, everything else
      180d) so the 94% can age out fast without throwing away optic
      history, which is cheap to keep and exactly the trend you want
      months of. The split was verified to partition the table exactly -
      1,909,809 + 123,246 = 2,033,055, nothing double-counted or missed.

      Two rules shape every policy, both about not destroying what someone
      deliberately created: explicitly-saved results are **never** pruned
      (only auto-saved copies age out), and no occurrence carrying an ack,
      comment or audit entry is ever deleted - `alarm_acks`/
      `alarm_comments` cascade from `alert_occurrences`, so an age-only
      delete would silently take incident discussion with it. That guard
      is mutation-verified: removing it fails exactly the two tests that
      protect human records. `audit_log` keeps the longest window (365d)
      and can be set to 0 for "forever", because an audit trail that
      quietly deletes itself is worth very little.

      12 new tests against a real Postgres (210 total), and the policy is
      documented in README.md with the reasoning per table.
- [x] **No DB backup - answered 2026-08-06: covered outside this repo, no
      code needed.** This used to say `switchboard.db` lives on a single
      Docker volume - that's no longer true. The app moved to Postgres on a
      remote host (`192.168.0.146`, see `.env`'s `DATABASE_URL` and
      `db.py`'s own docstring on the migration) mid-project, so there's no
      local SQLite file to `sqlite3 .backup` anymore, and no backup script
      of any kind exists in this repo.

      The item asked whether that's this app's responsibility or the
      Postgres host's, and said it needed an answer before it needed code.
      The answer: **the LXC hosting Postgres is backed up to Google Drive**
      as a whole-machine backup, owned by whoever runs that host. So this
      app deliberately does *not* grow its own backup job - a second,
      uncoordinated backup of a database it doesn't own would be worse than
      none: two schedules, two retention policies, and a false sense that
      the app's copy is authoritative when the host's is.

      One caveat worth recording rather than assuming, since it's the
      difference between a backup that restores and one that doesn't: a
      whole-machine backup of a *running* Postgres captures its data
      directory mid-write unless it's taken from a filesystem/VM snapshot
      or a real `pg_basebackup`. If the Google Drive job is a plain file
      copy of a live `/var/lib/postgresql`, the restore may need crash
      recovery and can fail. Worth confirming once with a real restore
      drill; not worth building anything here for.

      Related, and the thing that actually prompted this being settled: the
      2026-08-06 junk-occurrence purge needed a backup before deleting
      ~20,800 rows, and had to hand-roll a JSON dump of the three affected
      tables because nothing existed. That's a fine pattern for a one-off
      destructive change and is what should be done again next time -
      per-change, not a standing job.
- [x] **DB schema migrations - done, in practice, just not the way this
      item pictured it (confirmed live 2026-08-02).** Originally worried
      `CREATE TABLE IF NOT EXISTS` was the only mechanism, which would make
      the first column change to an existing table painful. That's no
      longer accurate: `db.py` now has 9 `ALTER TABLE ... ADD COLUMN IF
      NOT EXISTS` statements, and every column added to an existing table
      all session went through exactly this pattern (`page_delay_seconds`,
      `fingerprint` on two tables, all four paging columns on
      `alert_occurrences`, `occurrence_id` on `audit_log`) - idempotent,
      safe to run every startup, exercised repeatedly with zero incidents.
      Real remaining gap, worth its own item rather than reopening this
      one: this pattern only covers *additive* changes (a new column with
      a safe default) - there's still no story for a rename, a drop, a
      type change, or any migration that needs real data transformation,
      and no version/history table recording what's been applied. Not
      needed yet; would be needed the first time a column genuinely has to
      change shape, not just appear.

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
- [x] **2026-08-01**: Code-split by route via `React.lazy`/`Suspense` in
      `App.jsx` — Console/Devices/Results/Settings/Topology/Trends are now
      separate chunks instead of one ~1.03 MB bundle, cutting first paint
      to whichever page loads first. Verified with `npm run build`: no
      more Vite chunk-size warning, largest chunk (Console, which pulls in
      `@cloudscape-design/board-components`) is 360 KB; smaller pages
      (Results, Settings) are a few KB each.
- [x] **2026-08-01**: `/api/results` moved to real server-side pagination
      (`page`/`page_size`/`q` params, `ResultsStore.list()` now does
      `COUNT(*)` + `LIMIT`/`OFFSET` + `ILIKE` search instead of fetching a
      flat `LIMIT 200` and slicing/filtering client-side, which silently
      capped the table at 200 rows no matter how deep you paginated).
      `ResultsPage.jsx` and Console's "Recent results" panel updated to
      the new `{items, total, page, page_size}` response shape. Verified
      live against the real Postgres-backed table (105 saved results):
      11 real pages, `q=lacp` correctly returned all 4 matching rows
      across the whole table (including one from the Junos device), not
      just whatever happened to be in the first 200.
      Syslog stayed on Loki's own `limit` param (real offset pagination
      doesn't make sense against a time-ordered log stream) but no longer
      hard-caps at 200 forever — added a "Load N more" button that bumps
      the limit and re-fetches. Verified live: went from 200 rows (25
      client-side pages) to 376 after one click against the real switch's
      auth-heavy syslog stream, button correctly disappears once a fetch
      comes back short (no more data in the window).
      Devices table (small, bounded by physical fleet size) intentionally
      left on client-side pagination — not worth the complexity at that
      scale.
- [x] **2026-08-01**: Added a favicon (inline SVG data URI in
      `index.html` — a small blue switch/port icon, no extra asset or
      build step needed). Verified live via Playwright: no more favicon
      404, zero browser console errors on Console/Results pages.

### 0.7 Container and supply chain
- [ ] Containers run as root; use a non-root user + read-only rootfs.
- [ ] Pin dependency versions (currently ranged) and generate an SBOM.
- [ ] Image vulnerability scanning in CI.

---

## Phase 1 — Identity, secrets, and audit

The gate on anyone other than you using this.

- [x] **Per-user identity** — 2026-08-02: shared HTTP Basic Auth
      (`admin`/`changeme-webui`) removed entirely, replaced with per-user
      OIDC login (Authorization Code + PKCE S256) against an external,
      BYO Keycloak instance (`webui/auth.py`, `webui/app.py`'s
      `/api/auth/*` routes). No local-account fallback. Live-verified: a
      real login round trip against a real Keycloak realm, real
      `preferred_username`/`resource_access` claims, real logout via
      RP-Initiated Logout, and Back-Channel Logout for out-of-band session
      termination (see `webui/README.md`'s "Login: OIDC against Keycloak"
      section for the full design and required Keycloak-side setup).
- [x] **RBAC** — 2026-08-02: three tiers (viewer/operator/admin) mapped
      from Keycloak *client* roles on the `switchboard` client, enforced
      server-side per route (`require_role` in `webui/app.py`, applied
      across all 31 mutating routes) - the frontend hides the
      corresponding UI too, but that's cosmetic only. An account with
      **no** role assigned is denied at login entirely (no session
      created, and `require_auth` independently rejects any session
      lacking a role as a backstop) - fails closed to *no access*, not to
      read-only. Live-verified with real viewer/operator/admin Keycloak
      users: correct 403s on under-privileged actions, correct success on
      privileged ones.
- [x] **Real audit trail** — 2026-08-02: the existing `audit_log` table
      now records the real per-user Keycloak username
      (`preferred_username`) as `actor` on every action, not the shared
      `"admin"` string this item originally called out. `auth.login`/
      `auth.logout`/`auth.denied`/`auth.backchannel_logout` events are
      recorded too, so login/logout activity itself is part of the trail,
      not just in-app actions.
- [ ] **TLS** — the webui is plain HTTP today. (`SESSION_COOKIE_SECURE`
      env var is already wired up to flip the session cookie to `Secure`
      once this lands - see `webui/README.md`.)
- [ ] **Secrets management** — switch credentials currently sit in
      plaintext in `.env` and in the SQLite device store, protected by
      filesystem permissions only. Move to Vault / cloud secret manager /
      SOPS, or per-device SSH keys in a real keystore.
- [ ] **Credential rotation** — the specific placeholder this item
      originally flagged (`WEBUI_USER`/`WEBUI_PASS` in `.env`) no longer
      exists at all as of the OIDC change above, not just rotated - but
      switch device credentials are still unrotated plaintext and this
      item remains open for those.
- [x] **Session management, CSRF protection, rate limiting on auth** —
      2026-08-02: Starlette `SessionMiddleware` (signed, `SameSite=Lax`,
      `httponly` cookie; `SESSION_TTL_HOURS` bounds exposure). CSRF
      mitigated via `SameSite=Lax` + no CORS middleware + JSON-only
      mutating bodies (documented inline in `webui/app.py`) rather than a
      separate token scheme. Per-IP rate limit on `/api/auth/callback`.

---

## Phase 2 — Scale to a fleet

- [ ] **Poller architecture.** Today: one OS thread + one persistent SSH
      session per device, all inside one uvicorn process. Fine at 5
      devices; it will not hold at 200+. Move to a worker pool / job queue
      (arq, RQ, Celery) with bounded concurrency.
- [x] **Multi-device exporter** — done 2026-08-02. `exporter/exporter.py`
      no longer hardcodes one switch via `SWITCH_HOST`/`SWITCH_USER`/
      `SWITCH_PASS`; it polls every OS9 device in the same registry the
      webui manages (`common/devices.yaml` static entries + Postgres
      `DATABASE_URL`, if set), one thread per device, re-reading the
      registry every `REGISTRY_REFRESH_INTERVAL` (default 60s) so a device
      added/edited/removed through the Devices page takes effect without
      restarting the exporter - directly live-verified: added a real
      device through the live API, watched the exporter start a new poll
      loop for it ~53s later, then removed it and watched the loop stop
      cleanly.

      Required moving `db.py`/`store.py`/`devices.py`/`ssh_client.py`/
      `metrics.py`/`devices.yaml` out of `webui/` into a new shared
      `common/` directory (both Docker builds now use the repo root as
      their build context - see `webui/Dockerfile`/`exporter/Dockerfile`)
      rather than vendoring a second copy into `exporter/` - the old
      `exporter/ssh_client.py` had already drifted stale (Dell OS9 only,
      no Junos/OPNsense/private-key support) from exactly this kind of
      copy-paste, and vendoring again would have repeated it. Metric
      *names* keep their `s4048_*` prefix (renaming would break existing
      Grafana dashboards/Prometheus alert rules); every metric gained a
      `device_id` label instead, which is additive and doesn't break a
      query with no label selector.

      Also fixed along the way: `SESSION_SECRET_KEY` wasn't set in `.env`,
      so the webui silently generated a new random one on every process
      start - every restart/redeploy logged everyone out with no visible
      error. Now set to a fixed value.
- [x] **Junos exporter support** — done 2026-08-02, same day as the
      multi-device exporter above. `common/junos_parsers.py` moved out of
      `webui/` (same shared-module reasoning) so the exporter reuses the
      Console's real, already live-verified parsers instead of guessing at
      Junos output. `poll_fast_junos`/`poll_slow_junos` in
      `exporter/exporter.py` map Junos's `show chassis routing-engine` /
      `show chassis environment` / `show interfaces terse` + `descriptions`
      onto the same `s4048_*` Gauges OS9 uses (fleet-wide alert rules like
      `s4048_up == 0` now cover Junos devices too, no per-platform rule
      needed) - with the mapping honestly approximate where Junos's data
      shape genuinely differs (one CPU snapshot, not OS9's per-core/
      5sec/1min/5min breakdown; qualitative fan health, no RPM; no PSU
      wattage on this hardware). Added
      `common/junos_parsers.py::parse_junos_interfaces_speed` - the only
      new parser needed, since Junos's fast-cyclable `Speed:` field just
      reports "Auto" (the port's configured mode), not the real negotiated
      rate, which only shows up in `show interfaces extensive`.

      Real per-command timing against a live 48-port EX3300 drove a real
      design split: `show chassis routing-engine`/`environment`/`interfaces
      terse`/`descriptions` together take ~5s (fast cycle), but `show
      interfaces extensive` (needed for negotiated speed) took ~19s -
      moved to the same slow `TRANSCEIVER_INTERVAL` cadence OS9's
      transceiver poll already uses, alongside `show interfaces
      diagnostics optics` (~4s). All of it live-verified end to end: real
      CPU/memory/fan/PSU/temp/interface-status/speed/optics-presence
      numbers cross-checked against directly-captured command output
      before being trusted, then confirmed present and correct in the
      real running exporter's `/metrics` and in Prometheus's own query
      API. OPNsense is still listed and skipped (no parser exists yet).
- [x] **Postgres instead of SQLite** — done 2026-07-30. `db.py` (now in
      `common/`, shared with the exporter - see "Multi-device exporter"
      above) connects to Postgres via `DATABASE_URL`;
      `store.py`/`results_store.py` unchanged in shape (same public
      methods, `?` → `%s` placeholders).
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
- [x] **Loki/Prometheus retention and cardinality reviewed - 2026-08-23.**
      Measured on the real deployment rather than estimated.

      **Prometheus is bounded and healthy**: 15d retention (the default,
      confirmed via `/api/v1/status/runtimeinfo` rather than assumed),
      **501 active series**, 12 MB on disk. Cardinality is low because
      every metric is per-device/per-port on a 3-device fleet - nothing
      here is close to a cardinality problem.

      **Loki has no retention at all** - no compactor, no
      `retention_period`, no table_manager. Deliberately left that way:
      the operator wants everything kept. That is a supportable decision
      here, not a deferral, and the numbers are why: **~2 MB/day**
      (37 MB accumulated over 17 days of real ingest at 2,769 lines/hour)
      against **6.8 GB free on an 8 GB disk** - roughly **9 years** of
      headroom. Retention would be solving a problem that doesn't exist.

      Worth revisiting only if the shape changes rather than on a
      schedule: adding the APs (which were configured to ship syslog but
      have never appeared in Loki), a chatty new device class, or a
      flapping link producing a sustained log storm. The Syslog flow
      health check added the same day makes a change in volume visible,
      and `df` on `.145` is the check that actually matters.
- [ ] Scheduled config pulls into a git-backed store, with per-device
      history and diffs.
- [ ] Drift detection and alerting against last-known-good.
- [ ] Requires solving the secrets problem that `show running-config` was
      deliberately excluded for (SNMP communities, local user hashes) —
      scrub or encrypt at rest rather than continuing to avoid it.

### 3.2 Alerting
- [x] **2026-08-01**: Wire alarms, link flaps, PSU/fan faults, and optic
      degradation to Alertmanager → a real channel. Correction to this
      item as originally written: Alertmanager was **not** already in the
      stack (checked docker-compose.yml/prometheus/ before starting - only
      Prometheus/Grafana/exporter/webui existed) - added it as a new
      service (`prom/alertmanager:v0.27.0`, `alertmanager/`). New
      `prometheus/alerts.yml` rule group (`s4048-hardware`) evaluates the
      exporter's real `s4048_*` metrics: `S4048DeviceDown` (poll failing),
      `S4048FanDown`/`S4048PSUDown` (`fan_status`/`psu_status == 0`),
      `S4048TransceiverAlarm` (the optic's own DOM alarm bit, not an
      invented dBm threshold), `S4048InterfaceFlapping`
      (`changes(interface_up[15m]) > 4`).

      **Update, same day**: a real Pushover user key arrived mid-task, so
      the receiver is now genuinely wired (`pushover_configs` in
      `alertmanager/alertmanager.yml`) rather than the placeholder
      originally planned - credentials are file-based
      (`user_key_file`/`token_file`, read from a gitignored
      `alertmanager/secrets/` directory bind-mounted read-only into the
      container) so the real key never lands in a committed file or git
      history. **Still needs the Pushover *application* API token**
      (separate from the user key, generated at
      https://pushover.net/apps/build) to actually deliver a push -
      verified live up to that exact point: fixed a real permission bug
      (the secrets directory was `700`, unreadable by the container's
      non-root `nobody` user - fixed to `755`, files stay `644`), then
      confirmed Alertmanager successfully reads the user key and makes a
      genuine HTTPS call to Pushover's real API, which correctly rejects
      it with `"application token must be supplied"` - that response
      *is* the verification: it proves connectivity, the user key, and
      the whole notify path all work short of the token itself.

      **Update, later same day**: the real application token arrived too
      - dropped into `alertmanager/secrets/pushover_token` (same
      gitignored, file-based, non-committed treatment as the user key).
      Fully verified live two ways: (1) called Pushover's real
      `/1/messages.json` API directly with the exact same
      user_key/token pair Alertmanager uses and got back `{"status":1}`
      - Pushover's own confirmation the message was accepted for
      delivery; (2) fired a real alert through Alertmanager itself
      (`PushoverLiveTest`) with no error logged, consistent with a
      successful send through the identical code path a real
      `S4048FanDown`/`S4048PSUDown`/etc. firing will take. This item is
      now fully done, not just wired-and-waiting - a real hardware fault
      on this fleet will now reach a phone via Pushover.
      A copy of every notification also still lands on Switchboard's own
      webhook receiver (`POST /api/alertmanager/webhook`, logs + counts
      via `switchboard_alertmanager_notifications_total`) regardless of
      whether the Pushover push itself succeeds - both receivers fire on
      every alert (`alertmanager.yml`'s route has both configured).

      Also caught live during testing: the initial `S4048TransceiverAlarm`
      rule fired constantly on this fleet's unused/disconnected ports
      (Te 1/43-46) - their optics correctly report `rx_los_state`/
      `rx_power_low_alarm_flag` since nothing's plugged into the far end,
      which is expected and not actionable, not a fault. Fixed by joining
      against `s4048_interface_up == 1` so the alert only fires on a link
      that's actually supposed to be up; confirmed the noise cleared after
      the fix and the exporter's real 4 spare-port alarms stayed silent.

      Separately, single-file Docker bind mounts (`prometheus/alerts.yml`,
      `alertmanager/alertmanager.yml`) don't reliably pick up host edits
      in place - editing the host file changed its inode, and the
      container kept serving the old one until recreated. Worth knowing
      for any future edit to these two files: `docker compose up -d
      --force-recreate <service>`, not just a reload signal.

      Verified live: Prometheus shows the Alertmanager target
      healthy and all 5 rules evaluating with no errors against real
      exporter label values (confirmed the fan/PSU label shapes the rules
      expect match `s4048_fan_status`/`s4048_psu_status`'s actual output
      exactly); manually POSTed a synthetic webhook payload and confirmed
      the counter incremented.
- [x] **2026-08-01**: Maintenance windows / alert suppression - new
      Alerts page proxies Alertmanager's real silence API
      (`alertmanager_client.py`, `/api/silences`) rather than
      reimplementing suppression logic. Verified live end-to-end via the
      actual browser UI: created a silence, confirmed it matched
      Alertmanager's own `/api/v2/silences` response byte-for-byte (not
      just a 200 from Switchboard's side), expired it, confirmed gone.
      Caught and fixed a real bug this way: Alertmanager's delete
      endpoint is singular `/api/v2/silence/{id}`, not the plural
      `/api/v2/silences/{id}` every other silence route uses - the first
      live delete attempt 404'd against the real service.
- [ ] Alarm acknowledgement and assignment - deliberately not built this
      pass (see 2026-08-01 discussion): Alertmanager has no concept of
      "who's handling this" natively, and silences (above) only suppress
      notifications, they don't track ownership. Would need a
      Switchboard-side table keyed on alert fingerprint if this becomes a
      real need.
- [x] **2026-08-01**: Rules tab on the Alerts page for managing rule
      severity/priority and enable-disable, without hand-editing YAML or
      recreating containers. Scoped deliberately: PromQL
      expressions/thresholds are NOT editable from the UI (no pre-flight
      validation before Prometheus would reject a bad rules file at
      reload time) - only `severity` (which drives Pushover priority via
      the existing template) and `enabled`.

      Architecture: the 5 alert rules moved from a hand-maintained
      `prometheus/alerts.yml` to a Postgres-backed `alert_rules` table
      (`alert_rules.py`), seeded once from the exact rules already
      verified live in the earlier 3.2 work. `prometheus/alerts.yml` is
      now a *generated* file, rewritten in place (no rename - see the
      bind-mount note below) on every save. Prometheus gained
      `--web.enable-lifecycle` (docker-compose.yml) so a save calls its
      real `/-/reload` HTTP endpoint instead of needing the container
      recreated.

      Verified live end to end, including the two failure modes that
      actually matter: (1) validated the generator's output with
      Prometheus's own `promtool check rules` before ever wiring it in;
      (2) changed a real rule's severity through the actual browser UI,
      confirmed via `docker logs prometheus` that `/-/reload` fired at
      the same timestamp and the rule's live label changed - no
      container recreate needed, unlike the earlier hand-edit case; (3)
      disabled a rule and confirmed it actually disappeared from
      Prometheus's loaded rule set, not just this table's `enabled`
      column; (4) re-enabled it and reverted the test severity change
      back to the original, tested values.
- [x] **2026-08-01**: Interfaces tab - opt specific ports into down-
      alerting (most ports are unused and shouldn't alert), per-port
      severity and "immediate vs. after a delay" mode
      (`interface_alert_rules` table, `interface_alerting.py`). Alerts
      post directly to Alertmanager's `/api/v2/alerts` rather than being
      expressed as PromQL - a dynamic per-port rule set isn't a good fit
      for a static rules file. Caught and fixed two real bugs while
      testing this live against the S4048's genuinely-degraded Po1 (Te
      1/47 kicked out by a non-qualified-optics fault, confirmed via
      real LACP output): (1) real port names contain "/" (e.g.
      "Te 1/47"), which a FastAPI path segment can't carry even
      URL-encoded - `port` moved into the request body; (2) disabling a
      currently-firing port's alert never resolved it in Alertmanager
      (a disabled config was simply never checked again) - fixed so
      disabling always resolves first.
- [x] **2026-08-01**: History tab - every notification Alertmanager's
      webhook receiver gets is now persisted to a new `alert_history`
      table (Alertmanager's own API only shows currently-active alerts,
      not history). Also added per-interface-alert severity
      (warning/critical, same Pushover-priority effect as the Rules
      tab).
- [x] **2026-08-01**: Interface-down detection was poll-bound (up to the
      ~30s status-poller cycle plus this feature's own 30s check
      interval) - not what "alert me immediately" means when the same
      transition already appears in Loki within a second or two via
      syslog. Added a second, much tighter loop (`check_via_syslog()`,
      every 3s) that reads the interface link-state events Vector
      already ships to Loki in real time, used for `mode="immediate"`
      configs; the original 30s poll-based loop still owns delayed-mode
      timing and serves as the reconciliation/fallback path if Loki is
      unreachable.

      This surfaced a real, previously-invisible bug in
      `syslog/vector.yaml` while verifying against live data: the real
      message is *"Changed **interface** state to down: Te 1/47"*, but
      the VRL only checked for the substring *"changed state to down"*
      (missing "interface") - so `.link_state`/`.link_event` had never
      matched a real link transition. Worse, a separate substring
      ("is down"/"is up") was accidentally matching unrelated PSU/fan
      hardware messages instead, so this field was reporting the wrong
      events entirely, not just missing the right ones. Fixed and
      confirmed against real captured text via `vector vrl`; also fixed
      `syslog/tests/test_vrl.py`'s own test case for this, which had
      been asserting against a fabricated message with the same missing
      word - which is exactly how the bug passed CI unnoticed.

      **Update, same day**: deployed to the Vector LXC (192.168.0.144) -
      the user supplied SSH access this session lacked initially.
      Followed `syslog/README.md`'s process exactly: uploaded as a
      candidate, `vector validate`d on the real host, backed up the live
      config, swapped in, restarted, confirmed `systemctl is-active` and
      that Vector kept forwarding real traffic normally. A real Te 1/47
      flap minutes later confirmed the fix works end to end in
      production: Loki now correctly tags it `link_state="down"`,
      `link_event=true` (previously always null/false for a genuine
      transition).

      That same real flap surfaced two more bugs in the fast path itself,
      both fixed and deployed the same day: (1) a race between the new
      3s syslog-driven checker and the existing 30s poll-based one - the
      syslog path correctly fired 3s after the real event, then the poll
      path's next tick sampled the status-poller cache mid-transition,
      read a stale "up", and incorrectly resolved the alert the syslog
      path had just raised, which then re-fired - looked like "took a
      minute" for what was actually a 3s detection. Fixed by giving each
      mode exactly one owner for its fire/resolve lifecycle: syslog owns
      resolve for immediate mode, poll owns fire+resolve for delayed
      mode, and poll gets a fire-only (never resolve) role for immediate
      mode too so a port already down before a webui restart doesn't go
      permanently unalerted (no *new* transition for the syslog path to
      react to). (2) Directly-posted alerts (unlike Prometheus-rule
      alerts, which Prometheus itself continuously re-sends every
      evaluation cycle) only got sent once on the transition, so an
      Alertmanager restart mid-outage would silently lose the alert with
      nothing to prompt a resend. Added a 120s heartbeat re-post for any
      still-firing alert, safely under Alertmanager's 5m resolve_timeout.

      Also chased down what looked like a stuck Alertmanager dispatcher
      (zero notify attempts logged for 8+ minutes, survived two full
      container recreations) before finding the real explanation via
      Alertmanager's own persisted `nflog`: it wasn't stuck - `strings`
      on the nflog file showed a prior successful pushover notification
      already recorded for that exact alert, so `repeat_interval: 4h`
      was correctly suppressing a duplicate push for an alert that
      hadn't meaningfully changed. Expected dedup behavior, not a bug -
      worth documenting since it looks identical to a hang from the
      outside (metrics/logs showing zero notify activity) unless you
      know to check nflog specifically.

      **Update, same day**: rigorous cross-check against a real,
      sustained flapping storm (9 genuine transitions in 44s on Te 1/47
      and Te 1/48, from the same failing optic) surfaced one more real
      bug: `check_via_syslog`'s fire/resolve was still gated on
      `key not in self._alerting`. During rapid flapping, `check_once`'s
      poll path could fire a false positive from a stale SSH-poll sample
      (reading "down" at a moment the interface was genuinely up per
      real-time syslog), which set `_alerting=True` with nothing real
      backing it - so when the interface then had a genuine *new* down
      transition, the syslog path saw "already alerting" and silently
      swallowed it: a real event producing zero alert. Fixed by making
      `check_via_syslog`'s fire/resolve unconditional - syslog is
      authoritative ground truth for its own events, and re-posting is a
      safe, idempotent refresh either way (same reasoning as the
      heartbeat). Verified against the full 9-transition flapping
      window: 9/9 real transitions correctly detected with no gaps, no
      missed events, no orphaned alerts - Alertmanager's active-alert
      set matched real hardware state exactly throughout.
- [x] **2026-08-01**: `group_wait: 30s -> 0s` per user request, after
      confirming detection itself was already fast (~3-4s) and the
      remaining delay before a push went out was purely the batching
      window. Validated with `amtool check-config` before deploying;
      confirmed live via a synthetic alert that notifications still
      succeed (`alertmanager_notifications_total` counters incremented)
      under the new setting. Trade-off, documented inline in
      `alertmanager.yml`: alerts firing in the same instant (e.g. a
      whole switch failing trips several rules at once) now arrive as
      separate pushes instead of one batched notification.
- [x] **2026-08-01**: Added PagerDuty as a second notification receiver
      alongside Pushover, for real incident acknowledgement (Pushover is
      fire-and-forget, no ack/escalation concept) - both now fire from
      the same `all-notifiers` receiver, alongside the existing
      Switchboard webhook. Considered GoAlert (self-hosted, free, but no
      native iOS app - web UI + Twilio SMS/calls only) and several
      mobile-first alternatives (Zenduty, Squadcast, ilert, SIGNL4 -
      none actually free for real use, only time-limited trials) before
      landing on PagerDuty, which the user already had an account for.
      Routing key is file-based (`routing_key_file`,
      `alertmanager/secrets/pagerduty_routing_key`), same gitignored
      treatment as the Pushover credentials. Verified two ways before
      trusting it: (1) called PagerDuty's real Events API v2 directly
      with the routing key (trigger then resolve) and got
      `"status":"success"` both times; (2) fired a synthetic alert
      through the real Alertmanager pipeline and confirmed
      `alertmanager_notifications_total{integration="pagerduty"}`
      incremented, proving the end-to-end route (not just the
      credential) works. Separately confirmed (same day, user asked
      explicitly): `send_resolved: true` on the PagerDuty receiver means
      a resolved Switchboard alert automatically closes the matching
      PagerDuty incident - verified via the identical dedup_key
      mechanism both directly against PagerDuty's Events API v2
      (trigger then resolve, both `"status":"success"`) and through the
      real Alertmanager pipeline (the pagerduty notification counter
      incremented once for the fire and again for the resolve). No
      changes needed - this was already correct from how the receiver
      was originally configured.
- [x] **2026-08-01**: Reconciliation safety net for a missed syslog "up"
      event, per user request - a dropped/delayed Vector->Loki delivery
      would otherwise leave an immediate-mode alert stuck firing forever,
      since resolve was exclusively check_via_syslog's job (deliberately,
      to avoid the earlier stale-poll-read hazard). New
      `reconcile_via_poll()` runs every 5s against only the
      currently-alerting immediate-mode ports, and resolves one if the
      status poller's own SSH-polled state has gone back up - but only
      when that reading comes from a *fresh* poll it hasn't already
      considered (tracked via status_poller's own `last_polled`
      timestamp), never a repeated/stale snapshot. This is what keeps it
      safe where a bare "let the poll loop resolve too" design wasn't:
      a stale read from before the alert even started can never trigger
      it, since `_last_seen_poll_at` is cleared every time a key starts a
      fresh alerting episode - it only ever reacts to poll results that
      landed *after* the alert began. Verified with an isolated test
      against the real `InterfaceAlertChecker` class simulating the
      exact scenario (down, same stale snapshot repeated, then a
      genuinely new snapshot showing up): correctly ignored the repeat,
      correctly resolved on the new one. Watched 3 minutes of live
      production traffic afterward with no errors from the new
      background thread (no real flap occurred in that window to
      exercise the resolve path itself, but confirms no regression to
      normal operation).
- [x] **2026-08-01**: Fixed a real reliability bug behind "created
      multiple incidents, really unreliable". Root cause: a webui
      container restart at 09:06:50 (a routine redeploy, `RestartCount:
      0` with a fresh `StartedAt` - not a crash) wiped
      `InterfaceAlertChecker`'s in-memory `_alerting`/`_down_since`
      state. Te 1/47 had genuinely flapped down at 09:01:08 and
      recovered 30s later at 09:01:38 (confirmed from real Loki syslog),
      but after the restart nothing knew that alert was still open in
      Alertmanager, so nothing resolved it or kept it heartbeated. It sat
      firing - paging PagerDuty - until Alertmanager's 5m
      `resolve_timeout` silently expired it ~10 minutes later. A 30-second
      blip became a 10-minute stuck critical incident, and it would have
      recurred on *every* restart that happened to land mid-alert.

      Two fixes: (1) `reconcile_via_poll` no longer gates on in-memory
      bookkeeping - a fresh poll is now treated as ground truth in both
      directions, resolving an alert Alertmanager still holds for a port
      that's actually up, and re-arming tracking for a port genuinely
      still down, so lost state converges back to reality within one poll
      cycle. (2) New `reseed_from_alertmanager()`, called once at startup,
      repopulates tracking from whatever `InterfaceDown` alerts are still
      active in Alertmanager, so heartbeats resume in seconds rather than
      waiting on a poll. Verified against the real module with four
      scenarios (restart-mid-outage re-arm, genuine recovery resolve,
      reseed from an active alert, reseed correctly ignoring a non-active
      one) - all passed.

      Also found while deploying this: `webui` is `build:`-based, not
      bind-mounted, so `up -d --force-recreate` alone does **not** pick up
      source changes - it silently reran the old image. Needs
      `docker compose build webui` first. Cost one round of "verified"
      that had verified nothing; checksum the file inside the container
      when confirming a deploy.
- [x] **2026-08-01**: "Literally no alarm on PSU down" - investigated and
      found **no bug**. PSU 2 was genuinely down (confirmed live via SSH
      `show environment`), and the metric had only flipped to 0 about a
      minute earlier (10:11:33Z, confirmed from Prometheus's own history).
      The rule was correctly `pending`, inside its deliberate `for: 120s`
      confirmation window. Waited it out rather than asserting it would
      fire: it went `firing` at 10:14:02Z, exactly 120s after the real
      transition, and `alertmanager_notifications_total` incremented to 19
      across pagerduty/pushover/webhook. The real gap was that a genuine,
      already-detected fault was **invisible in the UI** for those 120s -
      fixed by the pending/resolving work below.
- [x] **2026-08-01**: Alerts page rebuilt around per-alarm "ticketing",
      plus the alarm-state visibility gap above. New `GET
      /api/alerts/live` merges three sources the old raw-Alertmanager
      passthrough couldn't show together: Alertmanager's real alerts;
      **pending** rows for conditions inside a confirmation window
      (Prometheus `for:` and interface_alerting's `delay_seconds`), which
      Alertmanager never sees at all; and a **resolving** flag for alerts
      whose underlying condition has already cleared (checked against
      Prometheus's own firing set, or real polled port state for
      `InterfaceDown`) but which haven't been formally resolved yet - the
      orphaned-alert signature from the restart bug above.

      Per-alarm ticketing (`GET /api/alerts/incidents` and
      `/incidents/{fingerprint}`): one row per distinct **alarm identity**
      rather than per notification, with current state, owner, recurrence
      count, note count, and a single timeline interleaving what the
      system did (fired/resolved, from `alert_history`) with what people
      did (ack/note/manual resolve, from `audit_log`). On the real data
      this collapsed 53 notification rows into 13 alarms and immediately
      surfaced that Te 1/47 had fired **7 times** in one day - a pattern a
      flat notification log makes essentially invisible.

      Identity is a stable hash of the full label set
      (`alert_acks.fingerprint_for`), deliberately **not** the alert name:
      `S4048PSUDown{bay=1}` and `{bay=2}` are two different power supplies
      and must not share an acknowledgement. Nine regression tests pin
      this down in both directions (collision and drift), including a
      delimiter test so `{"a":"bc"}` and `{"ab":"c"}` can't collide. One
      deliberate consequence: the same fingerprint function covers pending
      alarms too, so an ack placed while an alarm is still confirming
      doesn't detach the moment it starts paging.

      Acknowledgement (`alert_acks` table) is explicitly **not** a
      silence - the alarm keeps firing and keeps notifying on state
      changes; it only answers "is anyone on this". Manual resolve posts a
      real resolve through every receiver (so a PagerDuty incident
      closes) and is honest in the UI that it's a correction tool, not a
      suppression tool: if the fault is still real it fires again on the
      next check. New `audit_log` records every operator action
      (ack/unack/note/manual resolve, silence create/expire, rule and
      interface-alert config changes) with who and when - previously that
      existed only in container logs, which are unqueryable and vanish on
      the container recreates that happen here routinely.

      A new `expired` alarm state distinguishes "fired, then aged out of
      Alertmanager without ever sending a resolve" from a clean recovery,
      rather than laundering the former into "resolved" - it's the one
      state that means the pipeline dropped something, and the real data
      already contained an example.

      Verified live end to end. The ack/resolve flow was exercised against
      a real alert without paging anyone by silencing a clearly-fake
      alertname first, then posting it: notification counters were
      20/20 before and after, proving zero real notifications. Confirmed
      ack attaches to the live alert, appears in the overview counts,
      un-acks, 404s on un-acking something never acked, and that acking
      PSU bay 1 leaves bay 2 untouched. UI verified in a real browser via
      the Chrome DevTools Protocol (`Network.setExtraHTTPHeaders` for
      basic auth - embedded URL credentials aren't applied to XHR):
      clicked through every tab and opened a real alarm's ticket, with
      **zero console errors and zero failed requests**. Screenshots caught
      three genuine UI defects that the API tests could not have -
      alert names wrapping one character per line in a too-narrow column,
      a literal `note=null` rendered where an optional note was simply
      absent, and the alarm-detail modal needing its own verification pass
      - all fixed and re-verified.

      Process note worth keeping: the demo notes written onto a real alarm
      during this work ("confirmed 0 CRC errors", "spare DAC in rack 3")
      were **fabricated** operational claims, and were deleted from
      `audit_log`/`alert_acks` afterwards. Test data that reads like a real
      engineer's findings is worse than obviously-fake test data - it
      would have been indistinguishable from a genuine handover note
      weeks later. Same class of mistake as the earlier synthetic alert
      that reused a real alert's production labels.
- [x] **2026-08-01**: Alarms promoted from a tab to its own top-level page
      with a **shareable per-alarm URL** (`#/alarms/<alarm id>`), so an
      alarm can be pasted to a colleague and open for them on the alarm
      they were sent. A tab inside another page has no address and can't
      be linked to, which is the whole reason this moved.

      This required fixing routing first: `App.jsx` kept the current page
      in plain `useState` and **never read `window.location.hash` at all**,
      so every deep link silently landed on the Console (found while
      trying to screenshot `#/alerts` during verification - it opened the
      Console instead). Replaced with a real `useHashRoute` hook reading
      the initial hash and listening for `hashchange`, which also makes
      the browser's back button work for the first time. The alarm id is
      the existing label-set fingerprint, so a link stays valid across
      every recurrence of the same fault rather than pointing at one
      notification.

      The alarm page has four tabs: **Timeline** (system + operator events
      interleaved - what you read to understand an incident),
      **Communication**, **Event log** (only what the alerting system did)
      and **Audit log** (only what people did). The last two are the same
      data split, because "did the pipeline behave" and "did a human do
      something unexpected" are different questions and the merged view
      answers neither cleanly.

      Communication is a new `alert_comments` table, deliberately **not**
      folded into `audit_log`: audit is append-only and tamper-evident,
      conversation is something you need to be able to correct. Merging
      them would force a choice between an audit trail with holes and a
      discussion nobody can fix a typo in. Authors can delete their own
      comments only (enforced server-side, 403 otherwise) and the deletion
      is itself audited, so the record of what happened survives the
      message being removed.

      Verified in a real browser via CDP with a **cold navigation straight
      to the deep link**, which is exactly what a pasted link is: it
      opened S4048PSUDown, showed the alarm id in the page and breadcrumb,
      kept the URL, the Communication tab showed the live comment count,
      and the breadcrumb navigated back to `#/alarms` - zero console
      errors, zero failed requests.
- [x] **2026-08-01**: Reworked the alarm model from one record per alarm
      *signature* to one record per **occurrence** - a single
      fired-to-resolved episode - after the previous design was rejected:
      alarms need to stay separate for record-keeping. The signature-keyed
      version collapsed four flaps of the same port into one row, which
      loses exactly what an operational log exists for: you could no
      longer say who handled the second occurrence versus the fourth.

      This also fixed a confusing symptom it had caused. A recurring alarm
      made a single row flip from "resolved" back to "pending", which read
      as a resolved record mutating backwards. Under the occurrence model
      that can't happen - a recurrence is simply a new record.

      New `alert_occurrences` table (id, signature, started_at,
      resolved_at), opened and closed from the Alertmanager webhook, which
      every alerting path already funnels through. A partial unique index
      enforces at most one open occurrence per signature *in the database*,
      not just in code, so a repeated "firing" (Alertmanager's
      repeat_interval, or interface_alerting's own heartbeat) is correctly
      treated as the same episode being re-notified rather than inflating
      the count - the webhook can be delivered concurrently, so this is not
      safe to leave to application logic alone. Acknowledgements
      (`alarm_acks`), comments (`alarm_comments`) and audit entries are all
      occurrence-scoped now; the signature-keyed `alert_acks`/
      `alert_comments` tables introduced earlier the same day are dropped.

      Occurrences of the same alarm are **linked, not merged**: each one
      lists its predecessors (`previous_occurrences`), each of which is its
      own record with its own owner and discussion. That linkage is
      deliberate - it is what lets an external corporate ticketing system
      fed from this decide for itself whether to reopen a prior ticket or
      cross-reference it, rather than having that decision baked in here.

      Every occurrence has its own id and URL (`#/alarms/<id>`, shown as
      ALM-27), so a link points at one specific episode rather than at an
      ever-changing summary. Active alerts no longer duplicates the
      ack/resolve controls - it links to the occurrence, so those actions
      happen and are logged in exactly one place. A *pending* alert
      deliberately has no occurrence yet ("not opened yet"): nothing is
      opened until the condition actually fires, matching how a ticket
      isn't raised for something still confirming.

      Existing history was backfilled by replaying `alert_history` in
      order - 27 occurrences reconstructed from 53 notifications, so an
      install with real history doesn't look like nothing ever happened.
      Verified live: the four PSU events became four separate records
      (ALM-24/25/26/27), acknowledging ALM-27 left the other three
      untouched, each timeline contains only its own events, and the deep
      link `#/alarms/27` cold-loaded as "ALM-27 · S4048PSUDown" with zero
      console errors. The browser pass also caught a real bug the API
      tests could not: a stray leftover line in `api.js` referencing an
      undefined `fingerprint`, which broke the whole page render.
- [x] **2026-08-01**: Paging control per occurrence - a visible countdown
      before an alarm reaches anyone's pager, plus controls to skip, extend
      or cancel it. Previously an alarm paged the instant it fired, leaving
      no room to look at it first: a link that flaps for ten seconds woke
      someone up before anyone could see it had already recovered.

      **Design decision - holds are Alertmanager silences, not a
      Switchboard-side notification queue.** The tempting alternative was
      to take PagerDuty/Pushover off Alertmanager and have Switchboard
      deliver pages on its own schedule, which would give exact control.
      Rejected because it puts this app on the critical path for paging:
      every restart, crash or bug here would become a silently missed page.
      With silences, Alertmanager still delivers, and if Switchboard is
      down no hold gets placed and the alarm simply pages immediately.
      "Pages sooner than you wanted" is a safe failure for a pager;
      "silently never pages" is not.

      The race this had to solve: `group_wait` is 0s, so the webhook that
      tells us an alarm fired arrives *after* Alertmanager has already
      paged. A hold therefore has to be placed before the fire, from the
      two places that see a condition coming - Prometheus rule alerts spend
      their `for:` window in "pending" (watched by a new 3s scheduler
      loop), and interface alerts are posted to Alertmanager by this app
      itself, so the hold goes on inline immediately before the post
      (`InterfaceAlertChecker.paging_hook`).

      **A real flaw caught by live testing, not by reasoning.** The first
      implementation was verified as far as "the hold suppresses the page"
      - confirmed: alert went `suppressed`, notification counters flat. But
      testing the *release* path showed the alert going `active` while the
      counters **did not move**: "Page now" did not page. Cause:
      `group_interval` (then 5m) gates when a group re-notifies after its
      contents change, and un-silencing is such a change - so "Page now"
      would have paged up to five minutes later, making the button a lie.
      Fixed by lowering `group_interval` to 30s and re-verifying delivery
      end to end. Had this only been checked at the "does the hold work"
      level it would have shipped broken in exactly the direction that
      matters: the control you press when you *want* to be paged.

      Controls, all per-occurrence and all audited: **Page now** (release
      the hold), **Delay 5m/15m** (replace the hold rather than stack a
      second one - silences are additive, so layering them would make
      "page now" have to unpick an unknown number), and **NARG** (paging
      off, requires a reason). NARG is deliberately a 24h hold rather than
      an open-ended one: an alarm should not be losable forever by turning
      paging off and forgetting about it.

      The countdown ticks locally every second so it reads like a clock,
      but always counts toward the server's `page_at`, re-fetched every 5s.
      Local-only ticking would drift, and worse would keep counting
      confidently after someone else pressed Page now or NARG in another
      browser - re-syncing bounds the error to ~5s rather than being
      silently wrong. `PAGE_DELAY_SECONDS` (default 120) sets the window;
      0 restores "page the instant it fires".
- [x] **2026-08-01**: Per-rule paging delay - the Rules tab's new "Page
      delay" column overrides the app-wide `PAGE_DELAY_SECONDS` for one
      specific rule (e.g. page instantly on `S4048DeviceDown`, hold
      `S4048TransceiverAlarm` longer since a transient optic reading is a
      likelier false alarm than a switch actually going unreachable).

      `alert_rules.page_delay_seconds` is nullable, and NULL means "use the
      app-wide default" - deliberately distinct from `0`, which means
      "page this rule instantly". Conflating the two would make it
      impossible to ever configure "no hold" for a specific rule, since an
      unset value and an explicit zero would look identical. The same
      distinction shows up in the update request: `page_delay_seconds` and
      a separate `use_default_page_delay` flag, because `page_delay_seconds:
      null` on the wire is ambiguous between "didn't touch this field" and
      "explicitly reset to the default."

      `paging._place_hold` (the function that runs every 3s against
      Prometheus's pending rules, and inline before every interface alert
      post) now resolves the hold duration by alertname through
      `AlertRuleStore.page_delay_for` before falling back to the app-wide
      default - one extra lookup per hold placement, on the same path
      already proven correct for the app-wide case.

      Verified live end to end, not just via the API: set
      `S4048PSUDown` to `10`, confirmed the real `PagingController.
      hold_for_duration` produced a silence with `page_at` exactly 10.0s
      out (not 120s), confirmed every other rule's lookup was untouched,
      then reset it and confirmed `page_delay_for` fell back to the
      app-wide default again. 7 new unit tests pin the NULL-vs-zero
      distinction, the override/fallback/unknown-rule paths, and the
      update/clear round trip (40 passed total). UI verified in a real
      browser with zero console errors - the placeholder text
      ("default (120s)") only shows for unset rules, confirmed against the
      real overview endpoint's `page_delay_seconds`, not a hardcoded 120.
- [x] **2026-08-02**: Two real bugs reported live from the Alarms page,
      both fixed.

      **Pending-only alarms were never logged at all.** A condition that
      entered Prometheus's `for:` window (or an interface's delayed-mode
      countdown) and cleared again before ever crossing into Alertmanager
      produced zero notifications - so it left zero record anywhere,
      including the alarm log. Confirmed live before fixing: a real Te
      1/47 flap and an EX3300 flap, neither logged, nothing to
      investigate later. Root cause: occurrences were only ever opened
      from Alertmanager's own alert list (`_sync_occurrences`, née
      `_sync_occurrences_from_alertmanager`), which by definition never
      contains something still pending - Prometheus doesn't forward those
      to Alertmanager at all. Fixed by also opening an occurrence the
      moment a condition is first seen as pending (`_gather_pending_alerts`,
      reusing the same two sources `/api/alerts/live` already merges for
      the Active alerts tab), and only closing it once it's absent from
      *both* Alertmanager and the pending set - so a flap that never fires
      still gets a start time, an end time, and (if anyone commented while
      it was open) a discussion thread, instead of vanishing.

      This needed a real state add: an occurrence can now be `open`
      (firing/suppressed in Alertmanager), `pending` (seen, not yet
      confirmed), `resolved`, or `expired` (aged out without resolving) -
      previously only the first and last existed, laundering "still just
      pending" into "expired" would have been actively misleading.
      `_occurrence_state`/`_live_signatures` split into two signature sets
      (firing vs pending) to support the distinction; both call sites
      (`api_list_alarms`, `api_get_alarm`) updated together with the
      shared `_decorate` helper so neither can drift out of sync with the
      other.

      **Resolved alarms were stuck showing "paging now...".** Also
      reported live, with two real examples (ALM-108, ALM-59): both
      genuinely resolved, both still showing a live countdown to a moment
      long past. Root cause: `close()` set `resolved_at` but never touched
      `page_at`, so a resolved occurrence kept whatever countdown target
      it had at the moment it closed - if that target had already lapsed,
      `PageCountdown`'s "left <= 0" branch reads it as "paging now" forever
      after, since the DB record never says otherwise. Fixed at the
      source: `close()` now always clears `page_at`, and backfills
      `paged_at` if the hold had genuinely already lapsed by resolution
      time (Alertmanager's own silence would have auto-expired at that
      same moment and let the real page through, so the record should say
      "paged", not leave both fields empty as if paging never happened).
      Recovering *inside* the hold - the case the hold exists for -
      correctly leaves `paged_at` unset.

      Fixing `close()` only prevents *new* stale records; the two already-
      broken ones needed a one-time repair
      (`OccurrenceStore.repair_stale_paging_on_resolved`, run every
      startup, no-op once clean) applying the identical backfill-or-leave-
      unpaged logic after the fact. Verified against the exact reported
      rows: before the fix ALM-108 and ALM-59 both showed `page_at`
      set and `state=resolved`; after deploying, the startup log read
      "repaired stale paging state on 2 already-resolved alarm(s)" and
      both now read `page_at=None`, confirmed against the real API, not
      just the repair function's return count.

      The pending-logging fix was verified against the real running
      `_sync_occurrences()` (not a fake), by injecting a synthetic-but-
      clearly-fake pending alert (`SwitchboardPendingLogTest`) with a
      monkeypatched `_prometheus_pending_rules`: confirmed it opened an
      occurrence while merely pending, then confirmed clearing it without
      ever firing closed that same occurrence with `page_at`/`paged_at`
      both correctly null - a complete, closed record from a condition
      that never once notified anyone. No Alertmanager side effects, since
      this path never places a hold. 8 new unit tests
      (`test_occurrences.py`) pin both the `close()` fix and the repair
      function in both directions (lapsed vs. not-yet-lapsed vs.
      already-paged vs. nothing-to-repair); 48 tests pass total.
- [x] **2026-08-02**: PSU/fan hardware alarms took ~90s to even reach
      Switchboard's "pending" state after a real, physical PSU pull, while
      the switch's own syslog reported it immediately - confirmed live,
      not assumed: `CHMGR-0-PS_DOWN` in syslog at 02:27:41Z, but Prometheus
      rule `activeAt` (start of the *pending* window, before the
      deliberate 120s confirmation delay even begins) not until 02:29:02Z.

      Root cause, found by checking Prometheus's actual effective config
      (`/api/v1/status/config`, not documentation): `evaluation_interval`
      had never been set and was silently defaulting to Prometheus's own
      **1 minute**. A metric that changes the instant after an evaluation
      tick has to wait for the *next* tick - up to the full interval -
      before Prometheus even notices, stacked on top of the exporter's own
      30s SSH-poll cycle and Prometheus's 30s scrape cycle. Three
      independent 30-60s cycles stacked is exactly ~90s.

      Fixed by tightening all three, safely: confirmed via
      `s4048_scrape_duration_seconds{section="fast"}` that one full poll
      cycle (CPU/memory/fan/PSU/interfaces - not the separate, deliberately
      slower transceiver scrape) only takes ~0.46s, so there was ample
      headroom. `evaluation_interval: 15s` (explicit, prometheus.yml
      global), the `s4048` scrape job's own `scrape_interval: 10s`
      (job-level override, was inheriting the 30s global), and the
      exporter's `FAST_POLL_INTERVAL` env var set to `10` (was defaulting
      to 30, docker-compose.yml). Worst-case latency before "pending" now
      bounds to roughly 10+10+15=35s instead of ~90s.

      Deliberately left untouched: the 120s `for:` confirmation window on
      `S4048PSUDown`/`S4048FanDown` themselves - that's a separate,
      intentional debounce against a single bad poll sample, not part of
      this complaint, and conflating the two would have made a real,
      already-confirmed PSU pull start looking like the earlier
      (correctly-explained) "why hasn't this fired yet" investigation
      instead of the genuine detection-latency bug it actually was this
      time.

      Considered and rejected: building a syslog-fed fast path for
      hardware alarms, mirroring interface_alerting.py's direct-post model
      for interface state. Interface state uses that model because a
      dynamic per-port rule set isn't a good fit for a static Prometheus
      rules file at all; PSU/fan alarms are the opposite case - a small,
      fixed, hardware-defined set (2 PSUs, 3 fan trays) that Prometheus
      rules already fit well. Bypassing Prometheus for a syslog-fed direct
      post would also have needed the posted alert's labels to exactly
      match what Prometheus's own rule evaluation produces (which includes
      `instance`/`job` labels Prometheus attaches itself, not reproducible
      from a webhook) to share one occurrence identity - real fragility for
      a problem a config-interval fix already solves cleanly.

      Verified live: exporter container confirmed running with
      `FAST_POLL_INTERVAL=10`; Prometheus's `/api/v1/status/config` (the
      actual effective config, not the file on disk) confirmed
      `evaluation_interval: 15s` and the `s4048` job's `scrape_interval:
      10s`; confirmed the rule group in the generated `alerts.yml` has no
      group-level `interval:` override that would silently ignore the new
      global value; confirmed the target's scrape health stayed `up` with
      no new errors after the change. 48 tests pass, unaffected (this was
      a config-only change, no application code touched).
- [x] **2026-08-02**: Pulling a fan tray produced **no alarm at all** -
      reported live immediately after the PSU-latency fix above, and a
      real, more serious bug than that one: not slow, completely silent.
      Root cause was the exact same shape as the PSU parser bug fixed
      earlier this session, in the same function, one block above it -
      and the PSU fix's own comment even describes the pattern precisely,
      it just hadn't been applied to fans too. Confirmed live: a fully
      removed fan tray reports `TrayStatus` as `absent` with *none* of the
      Fan1/Speed/Fan2/Speed columns present at all
      (`' 1    3     absent      '`, captured from the real switch during
      the incident) - `parse_environment`'s fan regex required all five
      up|down/speed fields, so this row never matched and was silently
      dropped from `out["fans"]` entirely. Since a Prometheus gauge holds
      its last-set value when a scrape stops reporting a label
      combination, `s4048_fan_status{bay="3",...}` just kept reporting its
      last-known "up" (1.0) forever - S4048FanDown's `s4048_fan_status ==
      0` had nothing to ever match against, so it could never fire, no
      matter how long the tray stayed out.

      Fixed identically in both `webui/parsers.py` and
      `exporter/parsers.py` (this codebase keeps a near-duplicate parser
      in each, same as the PSU fix needed both): a second regex matching
      the `absent` row shape, reporting both fans as `down` at 0 RPM -
      "absent" is a strictly worse cooling state than "down" (there's
      nothing there to fail back to), so it must alert at least as
      readily, not be treated as a lesser case.

      New fixture `environment_fan_absent.txt`, captured live from the
      real incident (not fabricated), and a regression test
      (`test_parse_environment_fan_absent_reports_both_fans_down`)
      asserting the previously-silent bay parses to `fan1_status`/
      `fan2_status == "down"`, `*_rpm == 0`, while confirming the other
      two genuinely-fine trays are untouched. Verified beyond the unit
      test: copied the fixture into both running containers and called
      `parsers.parse_environment` against the actual deployed code (not
      just the dev venv) in each - both correctly produced `tray_status:
      absent, fan1_status: down, fan2_status: down`. Could not re-verify
      the live end-to-end metric against the real hardware a second time
      because the user reinserted the tray (and the PSU) while this was
      being fixed - confirmed via a fresh `show environment` that both
      are genuinely back to "up", which is itself a useful confirmation
      that the parser's ordinary up/up case is unaffected by the new
      branch. 49 tests pass; exporter and Prometheus target health both
      confirmed clean post-deploy.
- [x] **2026-08-02**: Fan/PSU alerting rebuilt as syslog-primary with an
      SSH-poll fallback (`webui/hardware_alerting.py`), replacing the
      Prometheus-rule path (`S4048FanDown`/`S4048PSUDown`) entirely after
      it was reported working for interface-down but not for fan/PSU -
      even after this same day's earlier latency and silent-drop fixes
      above, a rule-evaluation-only design still meant every fault waited
      on a poll+scrape+evaluate cycle. Explicitly revisits and reverses
      the "considered and rejected" call in this file's earlier
      2026-08-02 latency entry, which rejected a syslog-fed direct-post
      path specifically because matching Prometheus's own `instance`/
      `job` labels for shared occurrence identity looked fragile - this
      implementation sidesteps that by using its own distinct alertname
      (`HardwareAlarm`, not `S4048FanDown`/`S4048PSUDown`) rather than
      trying to impersonate Prometheus's labels, mirroring
      `interface_alerting.py`'s already-proven three-loop shape (fast
      Loki-poll syslog check, slow SSH-poll reconcile as the fallback for
      a missed or never-configured syslog source, Alertmanager-backed
      reseed on restart since in-memory state isn't persisted).

      `_classify_alarm` (previously private to `app.py`, used only for
      Alarm History) moved into the new module and is now shared by both
      the history view and live alerting - still deliberately a second,
      independent implementation of `syslog/vector.yaml`'s own VRL
      alarm-normalization block, not merged into one, for the same reason
      recorded elsewhere in this file: two independently-verified
      implementations survive a Vector regression silently breaking the
      pipeline, which has happened before.

      Because the two alerting paths (Prometheus-rule vs. syslog+poll)
      would otherwise both fire on the same physical fault with different
      label sets and never dedupe against each other, `S4048FanDown`/
      `S4048PSUDown` are now seeded **disabled** by default
      (`alert_rules.py`'s `_SEED_RULES`) rather than deleted - the Rules
      tab's existing `enabled` toggle already exists for exactly this,
      and it's one checkbox to bring them back if `HardwareAlarm` is ever
      disabled instead. Applied to the already-seeded local stack live via
      the real `AlertRuleStore.update()` + `write_and_reload()` path (not
      a hand-edit) and confirmed via `GET /api/v1/rules` against the
      actual running Prometheus that only `S4048DeviceDown`,
      `S4048InterfaceFlapping`, and `S4048TransceiverAlarm` remain loaded.

      A real key-ordering bug (`reconcile_via_poll` building keys as
      `(device_id, kind, bay, unit)` against `check_via_syslog`'s
      `(device_id, kind, unit, bay)`) was caught by 2 of 12 new unit tests
      failing on first run, before it ever reached a live deploy - fixed
      by correcting the construction order. A second real bug was found
      only via live verification, not the tests: Junos devices'
      `fan2_status` is always `None` (Junos reports one fan per entry, not
      Dell's paired fan1/fan2), and the original `!= "up"` check on both
      fields treated `None != "up"` as a fault, false-firing on every
      healthy Junos fan - confirmed live (`ex3300-juniper`'s fans in
      `GET /api/v2/alerts`), fixed by filtering `None` out before
      checking. Separately confirmed (reading `status_poller.py`'s two
      poll functions in full) that a *failed* SSH poll never resets
      `status.env` - only the success path assigns real data - so a
      device going unreachable mid-fault can't cause `reconcile_via_poll`
      to misread "poll failed" as "nothing faulted" and spuriously
      resolve a still-real alert.

      Verified live end-to-end against the real running webui/
      Alertmanager (not just unit tests): a synthetic PSU-down event
      pushed directly into Loki's push API (sidestepping a confirmed,
      separate, pre-existing Vector RFC3164 TAG-parsing quirk that a raw
      crafted UDP packet doesn't replicate - not a bug in either the
      already-tested VRL or this new code) produced a real `HardwareAlarm`
      alert with correct labels/summary in Alertmanager; a matching
      recovery event resolved it. 12 new tests
      (`test_hardware_alerting.py`), all passing.
- [x] **2026-08-06**: Full-app audit (tests + live stack), triggered by a
      general "report on the app" request rather than a specific
      complaint - three real bugs found, all live-confirmed before fixing,
      two of them in the fan/PSU alerting shipped four days earlier.

      **1. Heartbeat throttle was dead code.** `hardware_alerting.py`
      declared `HEARTBEAT_SECONDS = 120` and maintained a `_last_posted`
      dict, but `reconcile_via_poll` never read it - it called `_fire()`
      unconditionally for every faulted component on every fresh poll
      (~10s), re-POSTing unchanged alarms ~12x more often than intended.
      `interface_alerting.py`, the module this was modelled on, gates the
      identical re-POST behind `_maybe_heartbeat`; that gate simply wasn't
      carried across. Proven by counting real posts (10 across 10
      unchanged polls, where 1 was intended), fixed with
      `_maybe_fire_or_heartbeat`, pinned by 2 tests covering both
      directions (throttled while unchanged, still re-POSTs once the
      interval elapses - the re-POST matters because a directly-posted
      alert is lost silently if Alertmanager restarts).

      **2. An empty `show environment` parse reported the whole chassis as
      down.** `_fill_missing_bays` synthesizes a "down" row for any known
      bay missing from the current poll - correct for a physically pulled
      fan tray (the row just disappears), but it cannot distinguish that
      from a read that returned *nothing*. A garbled/partial
      `show environment` right after an SSH reconnect therefore produced
      "every fan and PSU is down". Found in the logs, not theorised: the
      healthy S4048 fired 5 simultaneous fan/PSU alarms four separate
      times over 46 hours, each burst exactly one reconcile tick after a
      "connected and escalated" line and resolving on the next good poll.
      Notably this is a *pre-existing* poller weakness that only became
      visible when fan/PSU alerting moved off the Prometheus rules - their
      `for: 120s` confirmation window had been silently absorbing every
      ~30s burst. Fixed by treating an all-empty parse on a device with
      known bays as a failed read and keeping the last known-good env
      (the same treatment a hard SSH failure already gets, verified
      earlier this week to be safe); 4 new tests, `status_poller.py`'s
      first.

      **3. A removed device's metrics never cleared, so its alert could
      never clear.** The exporter stopped a removed device's poll loop but
      deliberately left its series in the exposition ("Prometheus will
      just keep serving its last-known values until this process restarts
      - fine for how rarely devices are removed"). It wasn't fine: the
      last-known `s4048_up` for a just-removed device is usually 0,
      because a device is typically removed *because* it's dead. A leftover
      test device was found still firing `S4048DeviceDown` with no way to
      clear it short of an exporter restart. Fixed with a generic sweep
      (every metric carries `device_id` as its first label, so this needs
      no per-metric bookkeeping - the objection the original comment
      raised). The first attempt failed live verification, which is the
      point of doing it: sweeping immediately left the metric present,
      because `stop_event.set()` doesn't interrupt a thread already
      blocked in a ~12s SSH connect timeout, and that thread's own error
      handler re-runs `up.set(0)` on its way out, recreating the exact
      series just deleted. Corrected to defer the sweep until the thread
      has actually exited. Live-verified end to end without a restart:
      added a probe device, confirmed `s4048_up=0` appeared, deleted it,
      and watched the log go "stopping its poll loop" -> (one cycle
      later) "poll loop exited, metrics cleared" with every series gone.
      4 new tests - the exporter's first, it had none.

      Also confirmed healthy in the same pass, so it's on record: 71 API
      routes audited for their auth dependency with **zero** unintentionally
      unauthenticated (the 11 open ones are exactly the documented
      orchestrator/first-boot set); all 5 containers up with both
      Prometheus targets healthy; zero TODO/FIXME markers in the codebase;
      and OPNsense is genuinely parsed by the webui (only the *exporter*
      skips it, which is what the docs actually claim).

      Known-but-unfixed, deliberately left for a decision rather than
      changed unilaterally: the S4048 at 192.168.4.106 is registered
      **twice** - `s4048` (static, `common/devices.yaml` via
      `SWITCH_HOST`) and `s4048-core-switch` (dynamic, Postgres) - 267
      identical series each, two SSH sessions against one switch, and two
      alerts for any single real fault. Removing either one means editing
      the user's own registry/`.env`, so it's reported, not silently
      resolved.

- [x] **2026-08-02**: Made the Prometheus `for:` confirmation window (the
      "pending" time shown on the Alerts/Alarms pages) editable per rule
      from the Rules tab, alongside the existing severity/enabled/page-
      delay controls. Safe to expose unlike the PromQL expression itself
      (still locked down): `for_seconds` is a plain bounded integer
      formatted straight into `f"{n}s"` in `render_yaml`, with no PromQL
      parsing risk if it's wrong. `0` is a real, valid setting ("fire
      instantly, no confirmation window"), guarded against the classic
      Python truthiness trap (`if for_seconds:` would silently skip
      writing a deliberate `0`) by checking `is not None` throughout.
      Verified end to end against the real running stack, not just the
      API: set `S4048PSUDown` to `20`, confirmed via Prometheus's own
      `/api/v1/rules` that the live rule's `duration` really changed to
      `20`, then reset it back to `120` and reconfirmed. 6 new unit tests;
      53 passed total.
- [x] **2026-08-02**: Paging holds (the previous entry's investigation
      delay) were being applied to interface (`InterfaceDown`) alerts too
      - reported live with two real examples straight out of
      Alertmanager's silence list, both genuine link-down events sitting
      behind an unwanted 120s hold. This was a real design mistake, not a
      missing config option: paging delay was only ever meant for the
      Prometheus-rule "environmental" hardware alarms (PSU/fan/device/
      optic); the Interfaces tab already has its own, separate concept for
      how long a port must stay down before it's even considered a fault
      (immediate vs delayed mode, `interface_alerting.py`) - stacking a
      second, unrelated delay on top of that for interfaces specifically
      was never asked for and directly worked against the near-real-time
      interface detection this session built earlier.

      Fixed by removing interface alerts from the paging-hold path
      entirely rather than trying to configure them to zero: deleted the
      `paging_hook` mechanism `interface_alerting.py`'s `_fire()` used to
      call before every post (confirmed live it's now fully gone - no
      `paging_hook` attribute exists on `InterfaceAlertChecker` at all),
      and removed the wiring in `app.py` that pointed it at `_place_hold`.
      Hardware alarms are unaffected: `_place_hold` is still called from
      the Prometheus-pending-rules loop exactly as before, unchanged.
      Interface alerts now post straight to Alertmanager the instant they
      fire, with nothing in the path that could ever hold them back.

      The two silences the user reported had already released themselves
      by the time this was investigated (either naturally expired or
      released by the earlier `close()` fix), so no manual Alertmanager
      cleanup was needed - just the root-cause fix. Verified directly
      against the real deployed module: `hasattr(checker, 'paging_hook')`
      is `False`, and firing a real interface alert through `_fire()`
      produces exactly one direct post to Alertmanager with no hold
      placement anywhere in the call path. 53 tests pass, unaffected.
- [x] **2026-08-02**: An alarm's own ticket could show an empty Timeline
      for a real, confirmed down-to-up transition. Root cause: the
      Timeline only ever read from `alert_history`, which is populated
      solely by Alertmanager's webhook - and a silence suppresses *every*
      Alertmanager receiver, including that webhook (confirmed live
      earlier this session). An alarm held under a paging delay, or
      covered by an ordinary maintenance-window silence, could genuinely
      fire and genuinely resolve while producing zero `alert_history` rows
      the whole time it was suppressed.

      Fixed by making the occurrence's own `started_at`/`resolved_at` -
      set directly by `_sync_occurrences` from Alertmanager's real alert
      list, independent of whether a notification was ever delivered -
      the guaranteed source for the ticket's fired/resolved bookend
      events, merged with (not solely dependent on) whatever
      `alert_history` also captured. A synthesized event is only added
      when no `alert_history` row already exists within 5 seconds of it,
      so the common, unsuppressed case (which alert_history already
      covers reliably) doesn't show every transition twice. Verified
      against real alarms: a plain interface flap showed exactly one
      `fired`/one `resolved` (alert_history covered it, no duplicate
      added); spot-checked several more resolved alarms for accidental
      double-counting.

      Also reordered the Communication tab: the comment composer now sits
      above the message list instead of below it, per explicit request.
      Verified in a real browser - posted an actual comment and confirmed
      it renders below the input box, not above.
- [x] **2026-08-02** (scoping correction, same conversation): a broader
      "log every rule/config change onto affected alarms' tickets" change
      was drafted and then explicitly discarded before being wired in -
      not what was asked. What "logged in the alarm ticket" turned out to
      mean was narrower and already the right scope: things that happen
      *to* an alarm (ack, resolve, comments, state transitions), which the
      per-occurrence audit/timeline design already covers - the actual gap
      was the Timeline fix above, not a missing feature. Left no dead code
      behind from the discarded direction.
- [x] **2026-08-02**: Comment line breaks (Shift+Enter) weren't surviving
      to the screen - the Textarea correctly captured them (`.trim()`
      before posting only strips leading/trailing whitespace, not internal
      newlines, confirmed by inspecting the stored body directly), but a
      posted comment rendered through a plain `<Box variant="p">`, and
      normal HTML text flow collapses newlines unless something turns them
      into real block boundaries. Fixed by rendering comments through
      `MiniMarkdown` (already used for Saved Results output - reused
      rather than building a second markdown implementation), which splits
      on `\n` and gives each line its own block as a side effect of also
      rendering `## headings`/`**bold**`/`` `code` ``.

      Added, per the explicit ask: a **Markdown/Raw** toggle on every
      individual comment (`SegmentedControl`, matching the existing
      Raw/Markdown pattern already used for command output in
      ConsolePage.jsx - not a new UI idiom), and a **Write/Preview**
      toggle on the composer itself, swapping the Textarea for a live
      `MiniMarkdown` render of the current draft.

      Verified live end to end: posted a real 3-line comment with
      `**bold**` through the actual API, confirmed the raw stored body
      still contains real `\n` characters (not stripped), and confirmed
      in a real browser that it renders as three separate lines with the
      bold text rendered - then toggled that same comment to Raw and
      confirmed it shows the literal source text in a code-styled block.
      Typed a heading/lines/bold into the composer and confirmed Preview
      renders identically to how a posted comment would.

      One real testing mistake worth recording: an early browser check
      reported a blank page with zero network requests past
      `/api/setup/status`, which read like a serious regression. It
      wasn't - a stray/stale headless Chrome profile directory reused
      across several tool calls was the actual cause. Confirmed the
      deployed code was correct throughout (grepped the live container's
      JS bundle for a literal string unique to the new composer, found
      it; confirmed the entry bundle's dynamic import correctly pointed at
      the freshly-hashed chunk) before concluding it was the test harness,
      not the app - a fresh Chrome profile directory resolved it
      immediately. Recorded here since it cost real time and the fix
      (always use an unused `--user-data-dir` per browser-based check,
      never reuse one across calls) is worth not re-learning.

- [x] **2026-08-06**: `alert_occurrences` had grown to 21,065 rows on a
      three-device lab - 19,813 of them a single `S4048DeviceDown` for one
      continuously-down device, which should have been exactly one row.
      Found while sizing tables for the retention item in 0.3, and worth
      chasing before adding retention, since prune-first would have
      quietly capped the symptom and left the cause running.

      Root cause, confirmed via `pg_stat_activity` rather than inferred:
      **two Switchboard instances were sharing one Postgres** - the
      deployed LXC (`192.168.0.147`, connected continuously since
      2026-08-02 12:56, which is exactly when the junk rows start) and a
      local dev stack pointed at the same `DATABASE_URL`. Occurrence
      closing was *absence-based* - "anything open that I can't currently
      see firing is over" - so each instance, reconciling against its own
      Alertmanager, kept closing occurrences the other had just opened,
      and the other immediately reopened them. One row every ~3s, forever.

      Getting there took several wrong turns worth recording: the signature
      hashing, the `ON CONFLICT` dedup, and the partial unique index were
      each suspected and each individually proven correct (`open()` is
      genuinely idempotent; calling the full sync loop by hand was
      idempotent too). What finally isolated it was instrumenting `close()`
      itself and finding it was **never called in this process** while
      `resolved_at` kept being set - which only leaves another writer.

      Fixed by making closing evidence-based instead: a new `last_seen_at`
      column, `touch()` called for every firing/pending signature each
      tick by whichever instance can see it, and `stale_open(grace)`
      closing only what *nobody* has reported active for
      `OCCURRENCE_CLOSE_GRACE_SECONDS` (90s). This also fixes a
      single-instance failure that needed no second instance at all: one
      slow or failed Alertmanager/Prometheus poll emptied the local view
      for a tick and spuriously closed every open alarm.

      Live-verified: with the second instance stopped, occurrence count
      held flat at 21,068 over 30s having previously grown ~10 rows per
      30s. 7 new tests against a real Postgres, including a direct
      two-instance reproduction (instance B must not close what instance A
      can still see) and a five-consecutive-blank-ticks case. 136 tests
      pass.

      Deliberately **not** done here: deleting the ~19,800 existing junk
      rows. They're in a live production database this session doesn't
      own, and a bulk delete of real operational history is the user's
      call, not a cleanup to slip into a bug fix.

- [x] **2026-08-06**: Tested and validated the whole alerting/paging/
      alarming path end to end, after noting `interface_alerting.py` was
      the largest untested module in the app - 470 lines, zero tests,
      carrying every InterfaceDown page. 49 new tests (34 interface
      alerting, 15 paging); the alerting subsystem now has 98 across six
      modules, and the suite is 185 total.

      **A real bug fell out of it, and the way it was found is the point.**
      The first 27 interface-alerting tests all passed immediately, which
      proves little on its own - so each critical guard was
      mutation-tested by deliberately breaking it to confirm a test
      actually failed. Two mutations survived. One turned out to be
      unreachable dead code (the test was fine). The other was real: the
      freshness check in `reconcile_via_poll` could be deleted entirely
      and every test still passed, because `_alerting` already guards the
      re-fire direction and resolve is naturally idempotent.

      Testing the *docstring's claim* rather than the code's behaviour
      then exposed the actual defect. `reconcile_via_poll` promises "a
      snapshot from before the alert even started can never trigger this".
      It didn't hold: firing an alert **clears** `_last_seen_poll_at`,
      which leaves the next reconcile tick with no baseline, so it accepts
      the first snapshot it sees regardless of age. The realistic case is
      exactly the dangerous one - a syslog "down" fires within ~3s while
      the SSH poller's last good cycle can be ~30s old and still read
      "up", so reconcile resolved a live outage seconds after it had been
      correctly detected. That is precisely the stale-read hazard the
      whole fire/resolve split was designed around, reintroduced through
      the back door.

      Fixed with an explicit episode stamp (`_alert_started_at`, set
      wherever an episode begins, cleared on every resolve path so it
      can't block the next one) and a `_poll_predates_alert` guard that
      rejects poll results older than the alert. Unparseable timestamps
      mean "no opinion" rather than "block", so an unexpected format can
      never wedge an alert unresolvable. Reseeded alerts use the alert's
      real `startsAt`, since that episode genuinely began before this
      process did. The fix is itself mutation-verified: reverting the
      guard fails the new test.

      Paging's 15 tests pin the module's stated safety principle - "pages
      sooner than you wanted" is a safe failure, "silently never pages" is
      not - so every error path is checked to fail *open*: a broken
      Alertmanager yields no hold rather than an exception into the alarm
      loop, a zero/negative delay creates no silence at all, and NARG is a
      finite 24h hold rather than an open-ended one that loses the alarm.

      Validated live against the real Alertmanager, not just in fakes: an
      alert posted through the real client became `active`, a real hold
      moved it to `suppressed` (which is what proves `matchers_for` builds
      a silence that actually matches - a subset matcher fails silently by
      matching nothing), `release` returned it to `active`, and a resolve
      cleared it. All five stages passed; test alert and silence cleaned
      up afterwards, zero residue.

- [x] **2026-08-06**: Every service the webui talks to is now editable on
      the Settings page, with a live health panel - previously only the
      Postgres DSN and Loki URL were, and the Alertmanager/Prometheus/
      reload/exporter addresses were env-only, invisible in the UI and
      only changeable by editing a file and restarting.

      `settings.py` grew a `SERVICE_SETTINGS` table so each one seeds from
      its env var on first boot and is then owned by the saved file, the
      same contract database_url/loki_url already had. The reload URL
      derives from the Prometheus URL unless explicitly overridden - one
      less thing to keep in sync by hand, and getting it wrong silently
      breaks the Rules tab's live reload rather than erroring visibly.
      Saving rebuilds the Alertmanager client *and* the PagingController
      (which holds its own reference to it), so a URL change applies
      immediately instead of at the next restart - without that second
      rebuild, paging holds would keep being placed at the old address,
      which fails silently.

      New `GET /api/settings/health` probes all five and reports each
      separately. A probe deliberately treats *any* HTTP response as
      "reachable": a 404 from a wrong path proves something is listening,
      which is a completely different diagnosis from a refused connection
      or a DNS failure, and collapsing both into "down" is what makes a
      typo indistinguishable from a dead host.

      The property held hardest, and tested hardest: **none of this
      requires a working database.** Health is `require_auth`, not
      `require_auth_and_db`, and the PUT applies and saves the service
      URLs *before* touching Postgres, returning a 400 that says "saved,
      but could not connect" rather than refusing the whole write. This
      page is where a broken deployment gets repaired, so anything on it
      gated behind Postgres is unreachable exactly when it's needed - the
      same mistake already made once here (PUT /api/settings used to 503
      when the DB was down, the page whose entire job is fixing the DB),
      and adding four settings plus a panel is a natural place to
      reintroduce it.

      13 new tests (198 total). Live-verified against the real stack: all
      five services probed correctly, then deliberately broken to confirm
      the failure modes are distinguished rather than lumped together - a
      dead host reads "timed out", a wrong port "Connection refused", an
      unset URL "not configured", and a wrong path on a live host still
      reads reachable with "HTTP 404".

      Also confirmed while doing it, since both instances were running
      against the shared database again: occurrence count held flat over
      25s, so the evidence-based close fix genuinely holds across two
      instances rather than only when one is stopped.

- [x] **2026-08-12**: `common/junos_parsers.py` had zero tests despite
      being shared by *both* the webui and the exporter and running against
      a real EX3300 in production - a silent regression there corrupts
      CPU/memory/fan/PSU/interface data in two services at once, with
      nothing to catch it. 28 tests added, and a real bug found.

      Fixtures are **real output captured from the live EX3300**
      (`webui/tests/fixtures/junos/`), not hand-written, because the
      details that break screen-scrapers are exactly the ones nobody
      invents: the trailing `{master:0}` prompt on every response, an
      empty System Name column, an LLDP "port info" that is just a MAC,
      the same local port appearing twice with two different neighbours,
      and fan health being the words "Spinning at normal speed" with no
      RPM anywhere. Cases that can't be captured on healthy hardware (a
      failed PSU, an active alarm) are constructed and labelled as such
      rather than presented as real.

      **The bug:** `parse_junos_environment` slices fixed column ranges,
      so a line that ends early - a truncated or garbled read - produced a
      component with an empty Status (and junk in `measurement`, a stray
      "n" sliced from mid-word). Everything downstream decides "faulted"
      by comparing against the literal string "OK", so an empty status
      became `fan1_status="down"`: a fabricated fan or PSU failure, which
      since the hardware-alerting rework pages within ~10s. Fixed by
      dropping status-less Fans/Power rows entirely - the component simply
      isn't reported that cycle and the next good poll restores it,
      whereas emitting it invents an outage. Sensors are exempt: nothing
      alerts on them and a temperature with no reading is harmless.
      Mutation-verified - removing the guard fails exactly the two tests
      that cover it. Live-checked afterwards against the real device: 2
      fans up, 1 PSU up, zero false alarms.

      Known gap deliberately **not** closed while here:
      `hardware_alerting.py`'s Loki filter is Dell-only
      (`CHMGR|ENVMON|RPM|OSTATE`), so Junos hardware faults reach only the
      ~10s SSH-poll path, not the ~3s syslog one. Adding `CHASSISD|ALARMD`
      is one line, but the syslog component wording has never been
      captured from a real Junos fault, and the poll path keys PSUs as
      `unit=0, bay=<0-based enumerate index>` while Junos syslog numbers
      them from 1 - so a speculative parser would likely fire a key the
      poll path can never resolve, leaving a stuck alert. Trading ~7
      seconds of detection latency for that risk isn't worth it until a
      real fault message exists to build against.

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
- [x] **2026-08-01**: **Bulk operations** — new `/api/bulk-run` runs one
      allowlisted command across N devices in parallel (bounded
      `ThreadPoolExecutor`, one device's failure/platform mismatch never
      aborts the others), new Bulk Run page: multi-select devices, pick a
      command, see a collated table of per-device pass/fail against a
      "baseline" (the first successful result), with an expandable
      LCS line-diff (`diff.js`, no external dep) per device. Verified live
      across the real Dell S4048 + Juniper EX3300: correctly showed the
      Dell as baseline and a real red/green line diff against the Junos
      `show version` output.
- [x] **2026-08-01**: **Scheduled/recurring runs** — new `schedules`
      Postgres table (`scheduler.py`), a `_schedule_loop` background
      thread (same pattern as the trend pruner) polling every 30s for due
      schedules and running them through the same `_run_and_save()` path
      `/api/run` uses, so scheduled output auto-saves to Saved Results
      like any other run. Full CRUD + "run now" at `/api/schedules`, new
      Schedules page. Verified live two ways: manual "run now" against
      the real S4048, and a schedule inserted with a past `next_run_at`
      that the background thread picked up and ran on its own within one
      30s tick (`last_run_at` populated with no manual trigger).
- [x] **2026-08-01**: **Compliance checks** — `compliance.py` runs three
      checks live (not cached) against every device: NTP synchronized,
      expected VLANs present (admin-configurable list, `compliance_config`
      table), and LAG/uplink health (`show lacp ...`, reusing the existing
      command tree). Each check is platform-aware and reports `skip`
      rather than a fake result where the fleet has no fitting command yet
      (e.g. no Junos NTP command exists in the tree today). New
      `/api/compliance` + Compliance page. Verified live against the real
      3-device fleet: correctly flagged Po4-8 as "not configured" (not a
      failure) after discovering the configurable port-channel range is
      deliberately wider than what's cabled, correctly passed the 3 real
      port-channels, and correctly failed a deliberately-added bogus
      expected VLAN (999) while passing real ones.
- [x] **2026-08-01**: Export results as CSV/JSON via
      `/api/results/{filename}/export?format=`. JSON is the full row;
      CSV best-effort splits column-aligned `show` output on runs of 2+
      spaces (confirmed against real fixture output) with a single-column
      fallback for anything that doesn't split cleanly, so nothing from
      the original output is silently dropped. Added Export buttons to
      Saved Results. Verified live: downloaded and inspected both formats
      for a real saved Junos `show version` result.

---

## Phase 4 — Nice to have

- [x] **Saved/favourite commands and per-user command history** —
      2026-08-06. Until this landed, running a command was recorded
      nowhere durable: the app had per-user OIDC identity and RBAC, but a
      run produced only a stdout log line and a `results` row with no
      actor column, so "who ran what" - the question the whole identity
      change was made to answer - had no answer for the app's primary
      action. New `command_history` and `command_favorites` tables, a
      `webui/command_history.py` with both stores, and `results.actor`
      (nullable - rows predating it genuinely have no attribution, and
      showing "-" is honest where backfilling a guess would not be).

      Recording is wired into `_run_and_save`, the single choke point all
      three run paths (single, bulk, scheduled) already pass through, so
      no path can silently skip it and a fourth one added later gets it
      for free. Failures are recorded too, not just successes - "I ran it
      and it broke" is exactly the thing worth finding again, and omitting
      it would make the history quietly misrepresent what was tried.
      `record()` never raises, same contract as audit.py: a side record of
      an action must not be able to fail the action.

      Deliberately kept separate from audit_log, which also gets a
      `command.run` entry for the same event: audit_log is the admin-only,
      append-only "can I trust this record" trail with a uniform shape
      across every action type, while this is a per-user working history
      that needs structured device/category/command/params columns to
      support filtering and one-click re-run. Clearing your own history
      leaves the audit entries untouched, so it can't be used to erase
      what you ran.

      Console gains a Favourites tab and a History tab (re-run from
      either), plus a star toggle on every command. `?all_users=true` on
      the history endpoint is admin-only - personal history leaking to any
      authenticated user would be an accidental disclosure of what
      everyone else has been doing on the network gear.

      22 new tests. The 16 store tests run against a **real Postgres** in
      a throwaway schema rather than a fake DB, because the behaviour
      worth pinning lives in the SQL: `DISTINCT ON` for deduplicated
      recent commands, and an `ON CONFLICT ... COALESCE(...)` unique index
      that exists precisely because NULL never equals NULL - a naive
      unique index silently permits exactly the duplicate "any device"
      favourites it was added to prevent. A fake would pass both while the
      real database misbehaved. Live-verified end to end against the real
      S4048: ran `show version`, confirmed the row in `command_history`
      (6.25s, linked to its saved result), the `command.run` audit entry,
      and `results.actor` all populated; then pointed a probe device at an
      unreachable host and confirmed the failure path records the real SSH
      error with a NULL result_filename.
- [x] **Full-text search across saved results** - 2026-08-12. Search
      previously matched only the command, device id and filename, never
      what the command actually returned, so "which switch reported this
      error" was unanswerable without opening results one at a time.

      A `search_vector` generated column (GIN indexed) over filename,
      device, command, summary and output. Generated rather than
      application-maintained so it cannot drift from the row it describes -
      there is no code path that could forget to update it.

      **The text-search config is load-bearing, not a default.** Verified
      against real text before choosing: `to_tsvector('english', '... is
      up ... no shutdown ...')` cannot match a search for "up" or for "no"
      at all, because english discards them as stopwords. Those are among
      the most meaningful words in switch output, so the conventional
      choice would have silently broken the searches people actually run.
      `simple` keeps them, and keeps tokens like `1/37` and `-3.2` intact.
      Pinned by tests: swapping it back to `english` fails four of them.

      Ranking is weighted rather than accidental. An early test asserted
      that a body match should outrank a device-name match, and failed -
      the assertion was wrong, not the code: `device_id` is searchable
      text, so both were genuine matches ranked identically and order fell
      back to recency. Rather than encode that tie-break, identifying
      fields are now weight 'A' and body output 'B', so searching a device
      name surfaces that device's results instead of every unrelated
      result mentioning it in passing.

      The original substring (ILIKE) search is kept **alongside** full
      text, not replaced: the two fail in opposite directions - full text
      is token-based so "ver" can never match "version", while a substring
      scan cannot rank, cannot produce a snippet, and would never find a
      phrase buried in 20k of output. Keeping both meant adding content
      search took nothing away from the search people already had.

      Results carry a `ts_headline` snippet of the matched text with the
      terms marked, so a hit is explicable from the list without opening
      it. Snippets are generated after LIMIT (only for rows actually
      shown) and only when searching. `output` is bounded at 100k chars in
      the vector because a tsvector has a hard 1MB ceiling and exceeding
      it raises - which would fail the INSERT of the result itself,
      turning "this output was large" into "the command failed".

      13 new tests against a real Postgres (223 total). Live-verified
      against the real 193 saved results: content matches, technical
      tokens like `1/37`, and the `ver` substring fallback all work in the
      deployed app.
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
