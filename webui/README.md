# Switchboard

A web app for running pre-approved, read-only commands against network
devices: search a device, expand a category, hit Run. Built with
[Cloudscape](https://cloudscape.design) - the actual open-source design
system AWS Console is built from - not a hand-styled imitation of it (an
earlier pass tried to fake the look with plain HTML/CSS; no amount of
color-matching got it right, because the fidelity comes from the real
component behavior, not the palette). Runs at http://localhost:8080 (via
the `webui` service in the top-level `docker-compose.yml`), protected by
HTTP basic auth. Every command run also gets a best-effort one-line summary
above the raw output.

A brand new deployment (empty `webui-data` volume, no `.env` overrides)
boots straight into a **setup wizard** instead of crashing on missing
config: admin login, the Postgres connection string, and (optionally) a
Loki URL, all editable later from the in-app **Settings** page. See
"Deployment config" below.

Four pages, switched via the left nav - **Console**, **Devices**,
**Saved Results**, and **Settings**. Syslog and the front-panel visual used to be their own
top-level pages; both are now tabs on the Console instead, scoped to
whichever device is selected there, rather than a separate page you'd have
to re-pick the device on.

Selecting a device on the Console shows a **Device summary** panel
(`KeyValuePairs`): live Status (Up/Alarm/Down, from `status_poller.py` -
see below), how old that reading is ("Data age"), host, make/model,
platform, and an interfaces-up count, plus a short **Recent actions** list
of results for that device. The output side is a `Tabs` component with
five tabs:

- **Output** — the result of the last command you ran, defaulting to
  **Markdown** view (a formatted report: heading, device/command metadata,
  summary, output in a terminal-styled code block) with a toggle back to
  **Raw** CLI text. Every run is **auto-saved** the moment it completes -
  no separate "Save" step; a "Download .md" button next to the view toggle
  grabs the current result as a local file too.
- **Recent results** — every report for *this* device, with View/Delete
  inline, paginated.
- **Syslog** — this device's recent syslog, read live from Loki (see
  "Syslog" below) - category filter, text search, pagination, and a
  Refresh button.
- **Front Panel** — a live visual of the switch's physical ports (see
  "Front Panel" below): three-state coloring (up / down / administratively
  down), transceiver detail on click, a choice of illustration accuracy.
- **Switch Status** — the in-depth counterpart to the compact Device
  summary: CPU and memory (formatted, not raw byte counts), and expandable
  Fans / Power supplies / Thermal sensors tables, all with a Refresh
  button that forces an immediate poll instead of waiting for the next
  30-second cycle.

- **Devices** — a table of every configured device (searchable, paginated),
  with an "Add device" form to register a new one by IP, make/model/OS,
  and either a password or a pasted SSH private key.
- **Saved Results** — every auto-saved (and any manually-saved) report,
  across *all* devices, searchable and paginated, with View/Delete. "View"
  renders the saved Markdown in place. (The Console's own "Recent results"
  tab is the same underlying table, pre-filtered to the selected device.)

The top nav also has a **Dark mode** / **Light mode** toggle and a
**Compact density** / **Comfortable density** toggle, using Cloudscape's
real theming API (`applyMode`/`applyDensity` from
`@cloudscape-design/global-styles`, per this project's `CLAUDE.md`) rather
than a hand-rolled dark theme. Both choices persist in `localStorage`, and
mode defaults to the browser's `prefers-color-scheme` on first visit.

## Why this shape

The one rule this whole thing is built around: **the browser can never send
arbitrary CLI text to a device.** The frontend only ever POSTs
`{device_id, category_id, command_id, params}` — plain identifiers, not
command strings. The server looks those identifiers up in `commands.py`
(the allowlist) and only then builds the literal command to send over SSH.
If an identifier isn't in the allowlist, or a parameter isn't one of a
device's pre-generated valid values (e.g. a real port name), the request is
rejected (404/400) before anything reaches the switch. There's no path from
user input to shell/CLI injection because user input never becomes part of
the command text - it only ever selects among fixed, known-safe options.
This holds for every device regardless of how it was added.

Deliberately excluded from the allowlist: `show running-config` and
anything config-mode. This is a read-only observability tool; config-mode
commands (or a command that can dump secrets like SNMP community strings)
are out of scope on purpose. Every one of the 40 commands currently in
`commands.py` starts with `show` - but note this is **convention, not yet
enforced**: there is no automated test asserting it. Writing that test is
the highest-value item in `ROADMAP.md` §0.2, since it's the one check that
protects the core security property of this whole tool. (An earlier
version of this README claimed the rule *was* test-enforced; it wasn't.)

## Deployment config: the Settings page, not just `.env`

`webui/settings.py` holds the handful of things that differ per
deployment - admin login, the Postgres DSN, the Loki URL - as a small JSON
file (`webui-data/settings.json`, `hash_password()`'d, never plaintext) on
the same Docker volume that already held `devices_store.json`/
`switchboard.db` pre-Postgres. It's read once at startup and again on every
Settings-page save; `app.py`'s `_apply_settings()`/`_load_database()`
validate a new Postgres DSN by actually connecting before committing any
globals, so a typo'd save can't half-apply or drop the working connection.

Precedence: `settings.json` wins if present. `WEBUI_USER`/`WEBUI_PASS`/
`DATABASE_URL`/`LOKI_URL` in `.env` are only consulted to *seed* that file
on the very first boot of a fresh volume (`settings.bootstrap_from_env()`)
- handy for anyone who'd rather keep configuring per-deployment values the
old docker-compose way instead of clicking through the wizard. Once
`settings.json` exists, later `.env` edits have no effect; use the Settings
page instead. If neither is present, the app boots anyway (`CONFIGURED =
False`) and serves the setup wizard - unauthenticated `/api/setup` is the
only way in at that point, and it locks itself out (403) the moment setup
completes, so it can't be used to reconfigure a live deployment without a
Settings-page login.

If Postgres is unreachable using the *stored* settings (e.g. the box is
down), the app still starts and login still works - `CONFIGURED` stays
`True` but `DB_ERROR` is set and `require_auth_and_db` 503s anything that
touches devices/results until it's fixed from the Settings page, which
surfaces `DB_ERROR` directly.

## Storage: Postgres

Devices added through the UI and every saved result live in Postgres
(configured via the Settings page - see above), replacing an earlier SQLite
file on the `webui-data` Docker volume once the app outgrew "one file, one
process" (see ROADMAP.md Phase 2). `db.py`'s `Database` class is still a
thin wrapper with no ORM - one shared connection behind a lock, same
reasoning as before (this is a low-request-volume internal ops console,
not a case for pooling) - but now retries once after reconnecting if the
connection drops, since a network database can actually do that in a way
a local SQLite file never could. `store.py` and `results_store.py` keep the
exact same public methods they've always had (`.load()`/`.add()`/`.delete()`
and `.save()`/`.list()`/`.read()`/`.delete()`), so nothing above them had to
change shape - only the driver and SQL placeholder style (`?` → `%s`)
underneath.

On startup, if an old `devices_store.json` or `switchboard.db` exists on
the volume and Postgres is still empty, their contents are imported once
(`_migrate_legacy_json_devices()` / `_migrate_legacy_sqlite()` in
`app.py`) - both checks are idempotent, safe to run on every startup, and
a no-op once Postgres already has rows. Verified live: 54 real saved
results and the device store migrated cleanly from the old SQLite file,
survived a container restart, and new saves/device adds land in Postgres
correctly.

One thing this surfaced worth knowing: the Postgres cluster this points at
was initialized with `SQL_ASCII` encoding, not UTF8, which can't store the
em-dash characters this app's own markdown rendering uses (`render_markdown`
in `results_store.py`) - the very first migrated row crashed the app on
startup until the database was recreated with `ENCODING 'UTF8' TEMPLATE
template0`. Worth checking `SHOW server_encoding;` before pointing this app
at any other Postgres instance.

Every result now also carries an `auto_saved` flag: `/api/run` saves a row
on every single command execution, no click required: the "Kind" column
in Saved Results / Recent results shows "Auto" vs "Manual" (the manual
`POST /api/results` endpoint still exists, e.g. for scripted use, and rows
it creates are flagged "Manual").

## Syslog: read live from Loki, not re-derived

The switch already sends syslog to Vector on the LXC (`syslog/vector.yaml`
in this repo, deployed at `192.168.0.144`), which parses the
`%FACILITY-SEVERITY-MNEMONIC` format into structured fields
(`event_category`, `interface`, `link_state`, ...) and ships them to a
Loki instance at `192.168.0.145:3100` that was **already live** before
this feature existed. `loki_client.py` is a ~50-line stdlib (`urllib`)
client that queries Loki's HTTP API directly (`/loki/api/v1/query_range`)
- confirmed reachable and returning real events during development. This
reuses Vector's own parsing instead of re-implementing it, and needs no
SSH credentials for the LXC at all.

`/api/syslog` takes `device_id`/`category`/`limit`/`since_seconds`.
`category` is checked against a fixed allowlist first, then pushed into
the LogQL query itself via Loki's `| json` parser stage
(`{job="syslog"} | json | event_category="auth"`) rather than being
applied in Python after fetching - it's still never raw request text
reaching the query (the allowlist check happens first), just a validated
value being interpolated into a query Loki filters server-side. Device
filtering stays in Python, matching each event's own `source_ip` field
against the device's configured host rather than the Loki stream's `host`
label (the switch's self-reported hostname, e.g. "S4048" - a much less
reliable join key than the IP Vector saw the packet come in on).

**Bug, found and fixed**: an earlier version fetched the last `limit` raw
lines from Loki and filtered by category in Python *afterward*. On a
switch where one category dominates recent traffic (measured live: this
switch's log is ~99.8% `auth` - repeated admin SSH login/logout churn),
that silently starved out every other category, even when matching events
existed further back - the events had already been truncated away before
the filter ever ran. Selecting anything but Auth always came back empty,
which looked exactly like a broken filter. Moving the filter into the
LogQL query itself (so Loki filters *before* truncating to `limit`) fixes
this - confirmed live: `category=other` now correctly returns real LACP/
interface-state events that were invisible before.

A second, related bug lived in `syslog/vector.yaml` itself: `IFMGR`
(interface state-change events) wasn't in the facility→category map, so
real interface events were landing in "other" instead of "interface"; and
the facility/mnemonic regex used `[A-Z0-9_]+` for the mnemonic, which
doesn't allow hyphens - so LACP's `PORT-GROUPED`/`PORT-UNGROUPED` mnemonics
failed to parse at all, leaving those events on Vector's native syslog
facility ("local7") and uncategorized. Both fixed (`IFMGR` and `LACP`
added to the category map; the mnemonic regex now allows hyphens),
validated with `vector validate` against the real Vector binary on the
LXC before deploying, then deployed following the documented process in
`syslog/README.md` (backed up the running config, swapped in the fix,
restarted the service, confirmed `active`). This only affects newly
ingested events, not ones already in Loki.

## Front Panel: live port visual, traced against real photos

A physical front-panel view has no equivalent stock Cloudscape component
(same reasoning as the terminal output panel below: this is new content,
not an override of something Cloudscape already renders), so it's a
bespoke chassis illustration (`FrontPanelView.jsx`, `.switch-chassis` and
friends in `index.css`) fed by `/api/devices/{id}/status?interfaces=true` -
the same `status_poller.py` background poll used for the Console's Device
summary, just asked to also include its full per-port list instead of only
the up/total counts.

This isn't an abstract colored grid - it's built against several real
reference photos of this switch (kept at the repo root: `S4048-ON.webp`,
`dell_S4048-ON_front_closeup_DSC4689.webp`,
`StorageReview-Dell-S4048-Ports.webp`,
`dell-emc-powerswitch-s4048-on-...-w-ears.webp`), not guessed at:

- **Flat 1U proportions, not a tall block**: an early pass read as much
  taller than a real 1U strip (too much padding/stacked chrome on the left)
  - tightened throughout (grille, cage height, management block) to match
  how wide-and-thin the real unit actually is. Scrolling horizontally to
  see the far end (rather than shrinking everything to fit one screen) is
  the accurate behavior for something this wide relative to its height,
  not a bug.
- **Chassis chrome, split left/right like the real unit**: the left side
  (sticky, stays visible while the port area scrolls) has just the rack
  ear, DELL wordmark, icon-button cluster, and model name. Stack ID (a
  green digit display) and the SYS/FAN/PSU status LEDs sit **above the
  QSFP+ bank on the right**, not on the left - confirmed against a
  close-up reference photo, not guessed at.
- **Port cages**: a metal SFP+ cage per port with the distinctive
  release-latch tab visible on the real unit, tightly packed (thin
  dividers, not big gaps) within each group, and rectangular - wider than
  tall, not square, matching the real cage proportions.
- **Correct grouping**: 3 groups of 16 ports (not a guessed 4-of-12) -
  confirmed by counting the actual gaps in two different reference photos
  of this switch family before picking a number.
- **Staggered QSFP+ uplinks**: the 6 QSFP+ 40G ports are a 3-column x 2-row
  staggered bank (same top/bottom pairing as the main 48 ports), not a
  single row - also confirmed against the photos - and render as visibly
  wider cages since they're physically larger transceivers on the real
  unit.
- **Four arrows in one row between each pair, not a 2x2 block**: left two
  are **Link** (up arrow = top port's state, down arrow = bottom port's),
  right two are **Activity** - all four the same small triangle shape,
  matching a real close-up photo's "LNK 1▲▼2" silk-screening exactly (an
  earlier pass used a 2-row block with circular Activity dots before this
  correction).

**Illustration accuracy** (`chassisProfiles.js`) is a `Select` on the tab
itself, not auto-locked: `s4048-on` uses the photo-traced layout above;
`ex3300-48p` is the same treatment for the Juniper EX3300-48P, traced
against real photos (`img_5c7d30bd92eae48P.webp`, `1862015.webp`) rather
than Dell's layout reused with different labels - materially different in
several ways confirmed from those photos: 48 RJ45 ports in 4 groups of 12
(not 3 groups of 16), **one link LED per port column** (not Dell's
four-arrow link/activity pair - EX-series switches don't have a separate
activity indicator per port at all), an "EX3300" label + green LCD status
readout + two small STAT/ALM LEDs positioned *between* the RJ45 bank and
the SFP+ uplinks (not a logo block before all the ports, like Dell's), and
4 individually-numbered SFP+ uplinks in one row rather than a staggered
3x2 QSFP+ bank - rendered exactly as populated on the real unit (2 empty,
2 real DAC-connected ports, from live `show chassis hardware`). One thing
the reference photos' resolution couldn't confirm: which physical port is
"top" vs "bottom" in each column - that follows Juniper's documented
even-top/odd-bottom convention, not a pixel-verified read of a legible
silkscreen number, unlike everything else above. `generic-48` and
`generic-16` remain an honest fallback for any other device - a plain
sequential grid, no staggering, no fake branding, since there's no
documented or photographed layout to trust for hardware this app doesn't
actually know. The dropdown defaults to `s4048-on` when the device's
`model` field contains "S4048", `ex3300-48p` for "EX3300", and
`generic-48` otherwise.

**Three port states, not two** - `show interfaces status` alone can't
distinguish an administratively-shut-down port from one that's simply
enabled with no link; both just show "Down". `show interfaces description`
can (its own `Status`/`Protocol` columns), so `status_poller.py` merges
both commands' output (verified live: this switch currently has ports in
all three states at once - see `_merge_port_state` in `status_poller.py`).
Rendered as three distinct colors on the Link arrows: green (up), red
(enabled, no link - worth noticing), dark/off (administratively down -
intentional, not a fault). The SYS/FAN/PSU LED cluster above the QSFP+
bank reflects the same overall state and fan/PSU alarms the Device
summary reports.

**Activity is real traffic, not a fabricated blink** - `status_poller.py`
also runs a bare `show interfaces` (no arguments) on every 30-second fast
poll, which - verified live - dumps *every* interface's full detail,
including the switch's own device-computed "Rate info" (a rolling
~299-second average of Mbit/s and packets/sec) in a single round trip
(~1.4s added to the cycle, confirmed by timing it directly against the
switch before wiring it in - trivial next to the 30s interval). This one
command replaces what would otherwise be 54 separate per-port polls just
to answer "is there traffic on this port right now" - `parsers.
parse_interfaces_rates` turns it into `{port: {input_mbps, input_pps,
output_mbps, output_pps}}`, and a port's Activity LED lights up only when
its own real packets/sec is nonzero in either direction. The alternative
(guessing at activity, or blinking on a timer) was deliberately rejected -
every indicator on this panel reflects something the switch actually
reported.

**Transceiver detail on click** - a background poll every 5 minutes (like
the exporter's own transceiver cadence; 54 sequential `show interfaces
<port> transceiver` calls is too slow to do on the 30-second status
cadence) fills in each port's transceiver type and, when available, real
light-level diagnostics. Verified live against three real cases on this
switch: a genuine optical SFP+ (type `10GBASE-LR`, real
temperature/voltage/bias/Tx/Rx power readings), an Active Optical Cable
(`10GBASE-SR-AOC-5M`), and a copper DAC (`10GBASE-CU1M`) - the latter two
both report "DOM is not supported", which `parsers.parse_transceiver`
surfaces as `dom_supported: false`, so the popover shows the transceiver
type either way but only shows light readings when the transceiver
actually has them - no fabricated numbers for cables that don't support
DOM.

## Console: dynamic board layout

All nine sections of the Console page (Devices, Device summary, Commands,
Output, Recent results, Syslog, Alarm History, Front Panel, Switch Status)
render simultaneously as a Cloudscape `Board` - the drag/resize/auto-reflow
dashboard system AWS Console uses for CloudWatch dashboards - instead of
the fixed sidebar + `Tabs` layout this replaced. Every panel has a drag
handle and a resize handle; the board reflows (with Cloudscape's own
built-in animation) as panels move or resize, and auto-collapses to fewer
columns as the viewport narrows.

`src/boardConfig.js` holds the static config: `DEFAULT_BOARD_ITEMS` (id,
default `columnSpan`/`rowSpan`/`columnOffset` per panel - sidebar panels
default to the left column, richer panels like Front Panel and Switch
Status default to full width) and the `i18nStrings` both `Board` and
`BoardItem` require for accessible drag/resize (live announcements for
screen readers, ARIA labels on the drag/resize handles).

Layout persists to `localStorage` (`switchboard-console-board-layout`,
same `switchboard-*` key convention as the dark-mode/density toggles in
`App.jsx`), saved on every `onItemsChange`. A real bug was caught and
fixed here during verification: the first version of the merge-on-load
logic preserved each panel's saved size but always rebuilt the array in
`DEFAULT_BOARD_ITEMS`'s fixed order - since Board has no explicit
row-position field (vertical stacking comes from array order plus
`columnOffset`/`rowSpan`), this silently discarded every drag-reorder on
reload while resizes kept working, which made the bug easy to miss.
Fixed by preserving the saved array's order for known panel ids and only
appending unrecognized/new ids at the end. A "Reset layout" button
(top-right) clears the saved layout back to `DEFAULT_BOARD_ITEMS`.

Verified live in a real browser: all nine panels render with real data
simultaneously, dragging a panel's handle reorders it and the new order
survives a page reload, resizing a panel's handle changes its size and
that also survives a reload, and "Reset layout" clears `localStorage` and
reverts to the default arrangement.

## Two ways a device gets into Switchboard

1. **Static** (`devices.yaml`) — id/name/platform plus **env var names**
   for host/user/pass, never the secrets themselves, so the yaml file is
   safe to commit. This is how the original S4048 is configured. Add one
   by editing the yaml + `.env` and restarting the container.
2. **Added** (Devices page, `store.py`) — filled in through the UI: name,
   host, make, model, OS, username, and either a password or a pasted SSH
   private key (+ optional passphrase) plus an optional enable password
   (skipped for Junos - see below), plus an optional "Interface whitelist"
   JSON field (same `ports`/`port_channels` shape `devices.yaml` uses,
   with an optional per-entry `template` for non-Dell interface naming -
   e.g. Junos's `{"prefix": "ge", "range": [0, 47], "template":
   "{prefix}-0/0/{n}"}`) so parameterized commands have a real value list
   without needing a devices.yaml entry at all. Saved to Postgres (see
   "Storage" above) - same threat model as `.env`: plaintext secrets on
   disk, protected by database access, not encryption. A "Test connection"
   button attempts a real login before you save, but doesn't block saving
   if it fails - a device running something other than Dell OS9/Junos may
   legitimately fail the login handshake this checks while still being
   fine to store for later. Devices added this way can be deleted from the
   table; static ones can't (edit the yaml/`.env` instead).

**Dell OS9 and Juniper Junos both have real command trees, parsers, and
live status polling today** (`commands.py`'s `COMMAND_TREES`, keyed by
`device.platform`; `parsers.py` vs `junos_parsers.py`; `status_poller.py`'s
`_poll_once_os9`/`_poll_once_junos`). Verified live against a real EX3300-48P
(root SSH at 192.168.4.1) - see `ssh_client.py`'s module docstring for the
real, materially different login flow Junos needs (lands in a FreeBSD
shell, not the Junos CLI; no enable/privilege-escalation concept; its own
`---(more)---` pagination). Any other "Operating System" selection is
still marked "(experimental)" - it can be saved and reached over SSH, but
the Console's command menu has nothing wired up for it and will show an
empty Commands panel rather than guessing at syntax.

**Known gaps for the Juniper device specifically** (a deliberate scope
decision, not an oversight): it has no remote syslog configured, so its
Syslog and Alarm History tabs are empty - Switch Status instead shows
live alarms straight from `show chassis alarms`/`show system alarms`
polling. Per-port transceiver diagnostics aren't polled automatically
(no slow-cadence Junos equivalent to Dell's transceiver poll yet), though
the command is available manually. See ROADMAP.md's known issues for
both of these plus one more found live: the EX3300's own clock is
drifted (shows April 2026 while it's really July) - informational only,
not something this tool changes on production gear.

**OPNsense also has a real command tree, parsers, and live status polling**
(verified live against a real OPNsense 26.1 box, root SSH at 192.168.0.1).
Architecturally the odd one out among the three: SSH lands in the
console's numbered menu, not a shell - `ssh_client.py`'s `_connect_opnsense`
sends `8` ("Shell") to reach a real FreeBSD shell first, whose prompt
(`root@host:~ #`) needed its own prompt regex (`self._prompt_re`, no
longer just a module-level constant) since it has a space before the `#`
that the Dell/Junos prompt pattern doesn't expect. Commands are plain
FreeBSD/OPNsense CLI (`ifconfig`, `netstat`, `pfctl -s ...`), not a vendor
`show` grammar - kept strictly read-only same as the other two platforms,
just without a shared verb prefix to lean on. Deliberately **not**
integrated with Front Panel (a firewall appliance, not a switch chassis -
faking one would violate this app's own rule against fabricating hardware
layouts) or Topology/LLDP (it isn't part of the LLDP-discovered switch
fabric, so `/api/topology` skips it entirely rather than showing it as a
permanently-isolated node) or Syslog/Alarm History (no remote syslog
configured for it yet, same deliberate gap as the Juniper device).

## Layout

- `devices.yaml` — static device registry (see above).
- `db.py` — shared Postgres connection + schema (`devices`, `results`
  tables), behind a lock. See "Storage" above.
- `store.py` — devices added through the UI, backed by `db.py`.
- `loki_client.py` — stdlib HTTP client for Loki's query API. See
  "Syslog" above.
- `devices.py` — `Device` base class plus `StaticDevice` (env-var creds)
  and `StoredDevice` (inline creds, password or SSH key) subclasses;
  `load_devices()` merges both into one registry.
- `commands.py` — the command allowlist: `COMMAND_TREE` (40 Dell OS9
  commands across System, Interfaces, Port Channels, Layer 2, Layer 3,
  OSPF, Neighbors, Logging, Diagnostics) and `JUNOS_COMMAND_TREE` (System,
  Interfaces, Port Channels, Layer 2, Neighbors), both in
  `COMMAND_TREES` keyed by platform id. This is the "command hierarchy" -
  add a new command by adding an entry here, not by accepting new input
  from the client. `find_command(category_id, command_id, platform)`
  looks up the right tree.
- `parsers.py` — regex parsers for Dell OS9 command outputs (copied from
  `exporter/parsers.py`, reused by `summarize.py`), plus three added for
  the Front Panel and verified against real captured output before being
  wired in: `parse_interfaces_description` (admin-down/down/up
  three-state), an extended `parse_transceiver` (transceiver type +
  `dom_supported`), and `parse_interfaces_rates` (real per-port
  Mbit/s + packets/sec from a single bare `show interfaces` call, backing
  the Activity LED) - see "Front Panel" above.
- `junos_parsers.py` — the Junos equivalent, kept in its own file since
  the two platforms' output shares no format in common: `parse_junos_
  alarms` (merges `show chassis alarms` + `show system alarms` into the
  same `{'minor': [...], 'major': [...]}` shape `parse_alarms` already
  returns), `parse_junos_routing_engine` (temp/CPU/memory/uptime),
  `parse_junos_environment` (fans/PSUs/sensors), `parse_junos_
  interfaces_terse`/`parse_junos_interfaces_descriptions`. Every sample
  these were built against was captured live from the real EX3300, not
  guessed from Junos documentation.
- `opnsense_parsers.py` — the OPNsense equivalent: `parse_ifconfig`
  (per-interface status/description/inet, block-split since `ifconfig -a`
  has no fixed column layout to key off), `parse_uptime` (load averages),
  `parse_top` (CPU% + memory from FreeBSD `top`'s batch mode, `-b -d 1` -
  the Linux `-bn1` spelling isn't valid here), `parse_pf_info` (pf
  enabled/state-table size). Every sample was captured live from the real
  OPNsense box, not guessed from FreeBSD/OPNsense documentation.
- `summarize.py` — best-effort one-line summaries per command (e.g. "54
  ports: 8 up, 46 down" for interface status, "1 neighbor(s): 1 FULL" for
  OSPF). Keyed by `(category_id, command_id)`; can never raise - anything
  unexpected just means no summary line, never a broken response.
- `status_poller.py` — live device status (Up/Alarm/Down + how stale that
  reading is), polled the same way the Prometheus exporter does - over SSH,
  parsed with the same `parsers.py` functions the exporter uses. Two
  cadences on the same background thread: a fast poll (every 30s -
  environment, interface status merged with `show interfaces description`
  for the admin-down/down/up three-state distinction, CPU, memory) and a
  slow poll (every 300s, matching the exporter's own transceiver cadence -
  54 sequential per-port `show interfaces <port> transceiver` calls is too
  slow for the fast cadence). One background thread per device, taking
  that device's existing persistent SSH session's lock around each
  individual command (see "Session model" below) so a status poll and a
  user-triggered command never race on the same channel, and neither
  blocks the other for longer than a single command. Deliberately **not**
  wired to the actual `s4048-exporter` container or Prometheus: it's a
  separate, self-contained module so the webui gets live status for *any*
  configured device (including ones the exporter was never pointed at)
  without requiring the Prometheus/Grafana stack to be up, and the
  exporter keeps running as its own independent service, unaffected by
  whether the webui is even running.
- `app.py` — FastAPI backend: `/api/devices` (GET/POST),
  `/api/devices/{id}` (DELETE), `/api/devices/test` (try creds without
  saving), `/api/devices/{id}/values/{param}`,
  `/api/devices/{id}/status` (`?interfaces=true` for the Front Panel's
  per-port data, including transceiver detail) and
  `/api/devices/{id}/status/refresh` (POST, forces an immediate poll - the
  Switch Status tab's Refresh button), `/api/commands`, `/api/run`
  (auto-saves on every call), `/api/results` (GET/POST, GET takes optional
  `?device_id=` filter), `/api/results/{filename}` (GET/DELETE),
  `/api/syslog`. Every route requires basic auth.
- `results_store.py` — saved results, backed by `db.py` (see "Storage"
  above). Each row keeps a pre-rendered Markdown snapshot plus an
  `auto_saved` flag.
- `ssh_client.py` — `SwitchSSH.connect()` dispatches on `platform`:
  `_connect_os9` (enable escalation, shared in spirit with the exporter's
  copy) or `_connect_junos` (send `cli` to leave the FreeBSD shell SSH
  actually lands in, no enable concept, its own `---(more)---`
  pagination handling in `run()`) - see the module docstring for what was
  verified live against the real EX3300 to get this right.
  `load_private_key()` parses a pasted PEM (RSA/Ed25519/ECDSA) for
  SSH-key auth.
- `frontend/` — React + `@cloudscape-design/components`, built with Vite.
  `src/App.jsx` is the `AppLayout` shell (`TopNavigation`, `SideNavigation`,
  `BreadcrumbGroup`, `Flashbar` for notifications, plus the dark
  mode/density toggle) for the three top-level pages: `src/ConsolePage.jsx`,
  `src/DevicesPage.jsx`, and `src/ResultsPage.jsx`. Syslog and the Front
  Panel are no longer separate pages - `ConsolePage.jsx` renders them as two
  of nine panels on a dashboard (`@cloudscape-design/board-components`'
  `Board`/`BoardItem` - the same drag/resize/reflow system AWS Console uses
  for CloudWatch dashboards), scoped to whichever device is selected, using
  `src/FrontPanelView.jsx` (presentational - takes `device`/`status` as
  props, doesn't fetch anything itself) and `src/chassisProfiles.js` (the
  accurate-vs-generic illustration profiles). `src/boardConfig.js` holds
  the board's static config: panel titles, default position/size per
  panel, and the fairly verbose `i18nStrings` Board/BoardItem require for
  drag/resize accessibility - see "Console: dynamic board layout" below.
  `src/syslogUtils.js` holds the category options and severity/time
  formatting for the Syslog panel. `src/useClientPagination.js` is a small
  shared hook (slice + page-count + reset-on-filter) since Cloudscape's
  `Pagination` is presentation-only and four different tables needed the
  same logic.
  `src/markdown.js` formats a result into the same Markdown shape the
  backend generates; `src/MiniMarkdown.jsx` is a ~50-line renderer for
  exactly that subset (`##` headings, `**bold**`, `` `code` ``,
  ` ```text ` fences) - not a general Markdown parser, so no
  `react-markdown`/remark/rehype dependency chain for three constructs this
  app fully controls. `src/index.css` sets one global rule (`html, body {
  font-family: "Open Sans", ... }`, matching Cloudscape's own
  `--font-family-base` token value exactly), one for the terminal-style
  output panel (a fake macOS-style title-bar strip - three dots via
  `background-image: radial-gradient(...)`, no extra markup, applied once
  so it shows up everywhere `.terminal-output` is used), and one for the
  Front Panel's port grid - everything else is stock Cloudscape components
  and tokens, per the project's own `CLAUDE.md`: no Tailwind, no
  Material-UI, no hand-rolled `<button>`s.

  The title-bar strip initially overlapped/clipped whatever text came
  right before it instead of sitting in its own row - root cause: Cloudscape's
  `Box variant="code"` renders an inline `<code>` element, and vertical
  padding on an inline element paints without reserving space in the
  surrounding block flow. Fixed with `display="block"` on every
  `.terminal-output` Box (both call sites) plus a matching `display: block`
  in the CSS itself as a backstop.

  The global font rule exists because Cloudscape's own CSS only sets
  `font-family` *inside* its component classes - never on `html`/`body` -
  so any plain element not wrapped in a Cloudscape typographic component
  silently fell back to the browser's default serif font. Confirmed by
  grepping the built CSS bundle directly (`docker exec s4048-webui grep
  ... dist/assets/*.css`) for every `font-family` rule before assuming
  anything.

### Frontend build / performance

No frameworks beyond React + Cloudscape + Vite - no router library (two
pages are just a bit of `useState` in `App.jsx`), no state-management
library, no extra CSS layer, no icon pack beyond what Cloudscape ships.
Cloudscape components are imported per-component
(`@cloudscape-design/components/app-layout`, not the barrel import), so
Vite only bundles what's actually used. Production build is ~940KB JS +
~1.1MB CSS unminified, which gzips down to roughly 500KB combined -
reasonable for a full design-system app, not bloated by anything extra.
Backend (`app.py`) serves it with `GZipMiddleware` (confirmed the JS
bundle actually transfers gzip-encoded, not just theoretically eligible)
and immutable, one-year `Cache-Control` on Vite's content-hashed
`/static/assets/*` files - a repeat visit re-fetches only the tiny
`index.html`, not the bundle. `Dockerfile` is a multi-stage build:
`node:20-slim` runs `npm install && npm run build`, and only the compiled
`dist/` output (no `node_modules`, no source) makes it into the final
`python:3.13-slim` image.

### Session model: one persistent connection per device, not one per click

This *used* to open a fresh SSH session per click and close it immediately
after. That turned out to be actively wrong, not just slow: Dell OS9 only
has a handful of concurrent vty (SSH) slots, and opening/closing a new one
per command reliably starved it under real use - load-testing this by
firing all 39 commands back-to-back caused most connection attempts to
fail with "Error reading SSH protocol banner" (the switch accepted the TCP
connection but had no free session slot to hand it, so it never got to the
SSH handshake). This got worse because the exporter *also* holds one
persistent session open continuously, plus whatever else happens to be
polling the switch. Fixed by holding one shared, persistent `SwitchSSH` per
device (`_sessions` in `app.py`, mirroring the exporter's own design),
reconnecting only when the session actually drops, with a per-device lock
so concurrent requests serialize instead of racing on the same channel.
`ssh_client.py`'s `connect()` also retries once with a short backoff for
transient failures of that kind.

## Adding a command

Add an entry (with a unique `id` inside its category) to the right category
in `commands.py`. If it needs a parameter, add `"param": "<name>"` and
reference `{name}` in the `cmd` string - but only do this if you can also
generate a safe, exhaustive whitelist of valid values for it (the way
`ports` and `port_channels` work today). Never add a command that takes
free-text input. Optionally add a summarizer for it in `summarize.py`.

## Verified

Every command was tested for real, twice over: directly over SSH against
the live switch to find the actual working CLI syntax before adding
anything to the allowlist (caught two real syntax bugs - `show
mac-address-table` needs the hyphen, and `show spanning-tree 0 brief` is
the working form on a switch that doesn't run STP at all), and again
through the actual web API checking response *content* for Dell's error
markers, not just HTTP status (the switch answers syntax errors with HTTP
200, so status-only checks would have missed both).

The Devices page and its add-device flow were exercised end-to-end in a
real browser, on both frontend generations: password/SSH-key auth mode
toggle, Test Connection against the live switch with both correct and
wrong credentials, Save, the new device immediately searchable and
runnable from the Console (`show version` returned real output), Delete,
and that an added device survives a full container restart via the
`webui-data` volume. (The hand-rolled first version of this UI had a real
CSS bug here - `.field { display: flex }` silently overriding the
`hidden` attribute on the Password field when switching to SSH-key mode.
The Cloudscape rebuild doesn't have an equivalent failure mode: the
password/key fields are conditionally rendered in JSX, not
hidden-via-CSS, so there's nothing for a stray style rule to override.)

Dark mode, density, the Markdown view, saving, and the Saved Results page
were all exercised end-to-end in a real browser too: toggled dark mode and
compact density and ran a command under both, switched the result to
Markdown view and confirmed the rendered heading/bold/code-fence output
matched the raw output, saved it (real Flashbar success message), opened
it from Saved Results (content matched what was saved), and confirmed the
file survives a full container restart via the same `webui-data` volume
before deleting it.

The storage, auto-save, Syslog, and Front Panel work was verified in a
real browser too, across two rounds (the first round caught two real
bugs, both fixed and re-verified before calling this done):

- **Auto-save**: running a command with no "Save" click produces an
  immediate row in both the Console's "Recent results" tab (Kind: Auto)
  and the global Saved Results page; "Download .md" triggers a real
  browser download whose filename matches the "auto-saved as ..." note.
- **Persistence**: results and the device's make/model survived a full
  image rebuild + container recreate (not just a restart) via the
  `webui-data` volume when this was SQLite; re-verified against Postgres
  after the migration - 54 real results and the device store survived a
  container restart with the database on a separate host entirely.
- **Syslog**: both the standalone page and the Console's per-device tab
  render real, live, non-empty syslog (this switch has active auth
  traffic); category filter and text search were confirmed to actually
  change the result set, not just cosmetically re-render it.
- **Front Panel**: port coordinates were measured directly via
  `getBoundingClientRect()` in a real browser, not eyeballed - confirmed
  column 1 = ports 1/2 (not 1/25), the staggered scheme holds across all
  24 columns, QSFP+ boxes measure ~28% wider than SFP+ boxes, and the
  three status LEDs carry correct per-LED native `title` text. The first
  round of this measurement caught a real bug (the group-break math used
  the wrong modulus and produced one 24-port split instead of four
  12-port groups) - fixed and the corrected column math was re-verified
  by hand (`node -e` printing every column's port pair and break points)
  after the fix, matching the intended 12/24/36 break points exactly.
- **Terminal chrome**: the macOS-style title-bar dots initially overlapped
  and clipped the line of text above them (root cause: Cloudscape's `Box
  variant="code"` renders inline, and vertical padding on inline elements
  paints without reserving block-flow space) - confirmed via a cropped,
  pixel-level screenshot before the fix, and confirmed clean (dots in
  their own row, no clipped text) in both Markdown and Raw view, in both
  light and dark mode, after adding `display="block"`.

A later round moved Syslog and the Front Panel from top-level pages into
Console tabs, added the Switch Status tab, and added the admin-down/down/up
port distinction and transceiver detail - also verified in a real browser,
against real switch state captured directly over SSH beforehand (this
switch currently has ports in all three admin/link states, plus one port
with a real optical transceiver, one with an AOC cable, and one with a DAC,
specifically so the three-way distinction and the DOM-supported-vs-not
distinction could be checked against genuine data instead of guessed at):

- **Three-state port coloring**: all 54 ports' colors and `data-state`
  attributes were dumped and diffed against the real admin/protocol state
  pulled independently over SSH - exact match, including the specific
  detail that Te 1/33-36 and Te 1/43-46 are enabled-but-linkless (red)
  while Te 1/1-32 and Fo 1/49-54 are administratively shut down (dim gray).
- **Transceiver detail**: port 37's popover (real optical SFP+) showed real
  temperature/voltage/bias/Tx/Rx numbers matching a fresh SSH query at the
  same moment; ports 39 (AOC) and 47 (DAC) showed the correct type string
  with a "no light readings" note instead of fabricated numbers, matching
  `dom_supported: false` from `parsers.parse_transceiver`.
- **Switch Status tab**: Memory displayed as "2.7 MB used / 3.20 GB free"
  (formatted), not the raw byte counts the backend actually returns -
  confirmed the frontend's `formatBytes` conversion is applied, and that
  Refresh forces an immediate poll (Data age visibly reset to "just now").
- **Front Panel overflow bug**: the port grid (54 ports + LED cluster)
  initially spilled up to 608px past its dark container at 1024px viewport
  width, with no wrap and no scrollbar - measured directly via
  `getBoundingClientRect()` at four real viewport widths (2200/1920/1366/
  1024px) before concluding it was real, not a screenshot artifact. Fixed
  with `overflow-x: auto; max-width: 100%` on `.switch-chassis`, and
  re-verified at all four widths afterward: `scrollWidth`/`clientWidth`
  confirm the box is genuinely scrollable, `document.documentElement.
  scrollWidth` equals `window.innerWidth` at every width (zero page-level
  spillage), and setting `scrollLeft` directly was confirmed to bring
  later ports into view rather than leaving them permanently unreachable.

A subsequent photo-accuracy pass rebuilt the chassis chrome, grouping,
QSFP+ staggering, and the Link/Activity LED cluster against real reference
photos (see "Front Panel" above) - this caught a second real regression
before it shipped: the ventilation-grille decoration was originally two
`::before`/`::after` pseudo-elements set as real flex items with
`width:100%` inside the chassis's flex row, so each one consumed a full
container-width's worth of layout space and pushed every real element
(rack ear, DELL logo, all 48 ports) out of the initially-visible scroll
window - the tab looked completely blank until you scrolled. Caught by a
test agent measuring `scrollWidth` vs `clientWidth` at `scrollLeft: 0` and
noticing nothing was visible; fixed by moving the grille to a layered
`background-image` on `.switch-chassis` itself (doesn't participate in
flex layout at all) and re-verified: `chassis.scrollLeft` stays `0` and
the DELL logo/rack ears/all 48 ports are visible on first paint, no
scrolling required. The Link LEDs' real per-port activity data (`show
interfaces`'s Rate info, parsed by `parsers.parse_interfaces_rates`) was
also spot-checked against a fresh direct SSH query at the same moment -
the three ports shown active in the UI (nonzero packets/sec in either
direction) matched exactly.

A further round of direct user feedback against a 4th close-up reference
photo (`dell-emc-powerswitch-s4048-on-...-w-ears.webp`) restructured the
chassis again: flattened it to true 1U proportions (an earlier pass read
as noticeably taller), moved Stack ID + SYS/FAN/PSU status from the left
management block to above the QSFP+ bank on the right (matching that
photo), made the port cages rectangular instead of square, and rebuilt the
Link/Activity indicator from a 2x2 block of mixed triangle/circle shapes
into a single row of four same-shaped arrows (left pair Link, right pair
Activity) - each screenshotted and compared directly against the reference
photos after every change, not just described.

**Reported bug: the Syslog tab's category filter looked broken** - real
root cause was in `/api/syslog` fetching the last `limit` raw Loki lines
and filtering by category in Python afterward, so on a switch whose recent
traffic is ~99.8% one category (auth), every other category came back
empty even when matching events existed further back - see "Syslog" above
for the two-part fix (LogQL-side filtering in `loki_client.py`, plus a
real miscategorization bug in `syslog/vector.yaml` itself that this
investigation surfaced: `IFMGR` wasn't in the facility→category map, and
the mnemonic regex didn't allow hyphens, so LACP events failed to parse
and stayed uncategorized). Confirmed live post-fix: `category=other`
correctly returns real LACP/interface-state events that were invisible
before; `category=auth` still works exactly as before. The `vector.yaml`
fix was validated against the real Vector binary before being deployed to
the LXC (backed up the running config first, per `syslog/README.md`'s
documented process) - it only affects events ingested after the fix, not
ones already stored in Loki.

**Reported bug: hardware alarm severities were nonsense** - a fan removal
showed as a plain "notice" while a routine "Power supply 2 in unit 1 is
up" recovery showed as "emerg". Root cause: Dell's own severity for
`CHMGR` (chassis manager) messages is genuinely unreliable - verified
against real captured messages that `%CHMGR-0-PS_UP` (a recovery notice)
arrives at the same severity digit as `%CHMGR-0-PS_DOWN: Major alarm:
...` (a real failure). Fixed in `syslog/vector.yaml` by deriving alarm
severity from the message *text* instead (`Major alarm:` → critical,
`Minor Alarm :` → minor, bare "is down"/"is removed" → minor,
"is up"/"is inserted" → recovery, fan-speed-% telemetry → not an alarm at
all), emitting `alarm_severity`/`alarm_active`/`alarm_component`. Only
Minor/Critical are used as alarm levels, per how the switch itself
reports them. `CHMGR` was also added to the category map so these land in
`hardware` rather than `other`. The transform was tested against all the
real captured message variants using Vector's own `vector vrl` REPL
before deploying - every case (major/minor/bare-down/recovery/telemetry/
non-alarm auth) produced the expected output.

The webui side adds two things on top of that: an **Alarms** section on
the Switch Status tab driven by the switch's own `show alarms` command
(newly added to the allowlist - authoritative current Minor/Major state,
much more reliable than inferring from log history), and an **Alarm
History** tab backed by `/api/devices/{id}/alarm-history`, which reads
the alarm-tagged events back from Loki (filtered server-side via LogQL,
same reasoning as the category fix above - alarm events are rare next to
auth churn and would otherwise be starved out of the window). Its
"Active" tag is computed by correlating each component's most recent
tagged event: a fault with no later recovery logged is still in progress.
That correlation was unit-tested directly against a fault→recovery
sequence rather than assumed.

Separately: **removed fan trays and PSUs used to silently vanish** from
the Switch Status tables, because `show environment` simply stops listing
a bay once it's physically pulled - so a removed fan looked like "nothing
to see here" instead of a fault. `status_poller.py` now remembers every
(unit, bay) it has ever seen for a device and synthesizes a "down /
Removed" row for any that disappear, so a pulled fan tray or unplugged
PSU reads as a real fault in both the table and the alarm list.

## Note

`WEBUI_USER`/`WEBUI_PASS` in `.env` currently still hold the placeholder
`admin` / `changeme-webui` used to build and test this - change those
before leaving this reachable on the network, same as the switch password
it sits in front of.
