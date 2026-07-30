# Syslog interpreter (Vector, on the LXC at 192.168.0.144)

The S4048 was already configured to send syslog to `192.168.0.144`, where
[Vector](https://vector.dev) (already installed there, v0.57.0, running as
the `vector` systemd service) receives it on UDP+TCP `514`. This directory
holds the config that turns those raw syslog lines into structured events.

`vector.yaml` here is the deployed copy of `/etc/vector/vector.yaml` on that
LXC — treat this file as the source of truth and push changes to the LXC
(see below), not the other way around.

## What it does

1. **`syslog_udp` / `syslog_tcp`** — Vector's built-in syslog source, listening on `:514`.
2. **`process_syslog`** (pre-existing) — normalizes `device_host`, and
   deliberately overwrites `.timestamp` with ingestion time (keeping the
   device's own clock in `.device_timestamp`) so out-of-order UDP/TCP
   delivery can't produce out-of-order timestamps within a single Loki
   stream, which Loki rejects.
3. **`interpret_switch_event`** (new — this is "the interpreter") — parses
   the `%FACILITY-SEVERITY-MNEMONIC: detail` pattern Dell OS9 embeds in
   every message and adds:
   - `facility`, `severity_num`, `mnemonic` — the parsed pieces
   - `event_category` — `auth` / `interface` / `spanning-tree` / `hardware` / `routing` / `other`, mapped from `facility`
   - `interface` — e.g. `"TenGigabitEthernet 1/37"`, extracted from the message text when present
   - `link_state` — `"up"` / `"down"` / `null`
   - `link_event` — `true` when `link_state` isn't null, so you can filter on this one boolean instead of guessing keywords

Verified two ways: real traffic (SEC/SSH auth events from the switch)
parses correctly, and a synthetic `IFM-3-IFM_DOWN` / `IFM-6-IFM_UP` pair
injected over loopback UDP came out as
`event_category=interface, interface="TenGigabitEthernet 1/37", link_state=down/up, link_event=true`.
Dell's exact link-state mnemonic wasn't observed live (no real flap during
testing — deliberately did not force one on a production interface to
check), so the interface/link-state extraction is written generically
against message *text* (interface name + "up"/"down" wording) rather than
hardcoded to one mnemonic string, so it should hold even if the exact
mnemonic differs from the `IFM-6-IFM_UP` guess used in the synthetic test.

## Sinks — currently unchanged, needs your decision

Both existing sinks are left exactly as they were, just now fed by the
richer `interpret_switch_event` output instead of the raw `process_syslog`
one:

- `console_out` — prints JSON to stdout (visible via `journalctl -u vector`)
- `loki_sink` → `http://192.168.0.145:3100` — **this was already active and
  already receiving data before this change**, pre-dating this session.

Nothing new was pointed anywhere. But since the destination is explicitly
not decided yet, flagging that `loki_sink` already exists and is live is
worth a deliberate call on whether to keep it, pause it, or replace it once
the destination is chosen — see the architecture discussion for options.

## Redeploying after an edit

```
scp vector.yaml root@192.168.0.144:/etc/vector/vector.candidate.yaml
ssh root@192.168.0.144 'vector validate /etc/vector/vector.candidate.yaml'
# if that's clean:
ssh root@192.168.0.144 'cp /etc/vector/vector.yaml /etc/vector/vector.yaml.bak-$(date +%Y%m%d%H%M%S) && \
  mv /etc/vector/vector.candidate.yaml /etc/vector/vector.yaml && \
  systemctl restart vector && systemctl is-active vector'
```

Dated backups of prior configs are on the LXC:
`/etc/vector/vector.yaml.bak-20260728222754` (pre-interpreter),
`/etc/vector/vector.yaml.bak-20260729132823` (pre-category-fix, see below),
`/etc/vector/vector.yaml.bak-20260730072606` (pre-alarm-normalization -
this is the one that turned out to still be live until 07-30, see below),
`/etc/vector/vector.yaml.bak-20260730072941` (pre-"alarm cleared" fix).

## Changelog

**2026-07-30 (later still)** - fixed device timestamps being 10 hours
fast. The S4048's clock is configured in local time (`show clock detail`
reports "sydney", NTP-synced) and it sends legacy BSD-syslog timestamps
with no UTC offset on the wire - confirmed by sniffing the raw packet
with a small `AF_PACKET` python script (no tcpdump/tshark on this LXC):
`<189>Jul 30 17:48:00 S4048 ...`, no year, no offset. Vector's `syslog`
source has no config for the sender's timezone (checked via `vector
generate-schema` - the source only has `host_key`/`log_namespace`/
`max_length`; an earlier attempt to set `timezone: Australia/Sydney`
directly on the source validated fine but silently did nothing, since
Vector doesn't reject unknown config keys), so it assumed every naive
timestamp was already UTC. Fixed in `process_syslog` instead: take the
wall-clock digits Vector already parsed, discard its incorrect UTC
label, and reinterpret them as `Australia/Sydney` via `parse_timestamp`
with a `timezone` argument - this uses the real IANA zone database so it
keeps working across Sydney's DST transitions, rather than a hardcoded
`+10`. Verified against a live event within seconds of real UTC after
deploy.

**Note:** this only fixes timestamps for events ingested from this point
forward. Loki log lines are immutable once written, so any event already
stored (e.g. in Alarm History) keeps its old, 10-hours-fast
`device_timestamp` permanently - same category of limitation as the
alarm-severity fields below.

**Also:** the first deploy attempt of this fix broke Vector entirely for
about a minute (07:58:36-07:59:21 UTC) - a debug field I added used
`to_string(x) ?? "null"` where `x` can't actually fail, which is a VRL
validation error (E651), and the deploy script moved the candidate file
into place and restarted *before* checking the validate exit code
instead of gating on it. `vector validate` should always gate the
`mv`/`restart` step, not just be eyeballed.

**2026-07-30 (earlier)** - fixed a real bug in the alarm
normalization added earlier today: recovery messages containing the text
"alarm cleared" (e.g. `Major alarm cleared: Power supply 2 down reported
in unit 1 is cleared`) contain the substring "major alarm", so the
`contains(detail_lower, "major alarm")` check matched them and classified
a *recovery* as a brand-new active critical alarm. Found by feeding real
captured "cleared" message variants through `vector vrl` after the
Alarm History tab was reported broken. Fixed by checking for "cleared"
first. Re-verified against all 8 known real message variants (fault,
recovery, and "cleared" forms for both fan-tray and PSU events) before
redeploying.

**2026-07-30 - the alarm-normalization change below was written and
tested this day, but the redeploy to the LXC never actually happened** -
the changelog entry claimed it was deployed; the live `/etc/vector/
vector.yaml` on 192.168.0.144 was still the 07-29 pre-CHMGR-mapping
version until this was caught (user reported "alarm history is broken";
diffing the repo copy against the deployed file showed the whole block
missing). Actually deployed now, and the config in this directory is the
source of truth going forward - if you edit `vector.yaml` here, **push
it**, don't just update the changelog.

Original hardware alarm normalization work (see above for the deploy
gap): Dell's own severity for `CHMGR` (chassis manager: fan/PSU) messages
is unreliable - verified from real captured messages that `%CHMGR-0-PS_UP`
("Power supply 2 in unit 1 is up", a routine recovery) arrives at the
same severity digit as `%CHMGR-0-PS_DOWN: Major alarm: ...` (a real
failure), so the UI showed fan removals as "notice" and PSU recoveries as
"emerg". Added an alarm-normalization block deriving `alarm_severity`
(critical/minor only, matching how the switch itself reports alarms),
`alarm_active`, and `alarm_component` from the message *text* rather than
its severity digit, plus `CHMGR` → `hardware` in the category map. Tested
against every real captured variant with `vector vrl` before deploying.

**2026-07-29** - two real miscategorization bugs, found via the webui's
Syslog tab appearing to have a broken category filter (every category but
Auth came back empty): (1) `IFMGR` (interface state-change events) wasn't
in `category_map`, so real interface events landed in `other`; (2) the
mnemonic regex (`[A-Z0-9_]+`) didn't allow hyphens, so LACP's
`PORT-GROUPED`/`PORT-UNGROUPED` mnemonics failed to parse entirely,
leaving those events on Vector's native syslog facility ("local7") and
uncategorized. Fixed (`IFMGR`/`LACP` added to the map; mnemonic regex now
allows hyphens), validated with `vector validate` against the real Vector
binary on the LXC, and deployed following the process above. Only affects
events ingested after the fix - already-stored Loki data keeps its
original (wrong) category.
