# sFlow collector (the LXC at 192.168.0.155)

The switches sample their own traffic and send it here; `sfacctd` (pmacct)
decodes it and writes flow records straight into the same Postgres the
webui reads. The **Traffic (sFlow)** page renders four views over that
table: top talkers, top hosts, protocol/service mix, and per-port traffic
with a per-interface drill-down.

Deliberately no intermediate store, unlike the syslog path's
Vector → Loki → webui: pmacct's postgresql plugin *is* the shipper, so
there is one less moving part.

## Files

| File | Deployed to | Notes |
|---|---|---|
| `sfacctd.conf` | `/etc/pmacct/sfacctd.conf` on .155 | Treat this as source of truth and push to the host |
| `sflow_flows.schema` | reference only | The table is created by the webui (`common/db.py`), **not** by pmacct — see below |
| `tests/send_test_datagram.py` | run from anywhere | Emits a real sFlow v5 datagram, for proving the pipeline without switches |

The systemd unit is `sfacctd.service` on the host, `enable`d at boot —
which is not a detail to skip: the Vector collector was found stopped for
seven days because it had never been enabled, and nothing noticed.

## The one contract that matters

`sfacctd.conf`'s `aggregate` line and the `sflow_flows` column names are a
single contract. pmacct maps each aggregation primitive to a fixed column
name (`peer_src_ip` → `peer_ip_src`, `src_port` → `port_src`, …) and
refuses to start if the table doesn't match. **Change one, change both.**

Two settings that are not optional and cost real time to discover:

- `sql_optimize_clauses: true` — without it, `peer_src_ip` makes pmacct
  infer a fixed built-in "bgp" table layout and then reject the config
  with *"IP host accounting not supported for selected
  sql_table_version/_type"*. It starts, connects, and only then fails.
- **No `sql_table_schema`.** pmacct executes that file as DDL at startup,
  colliding with the webui, which is the real creator of the table
  (`ERROR: relation "sflow_flows" already exists` on every purge).

## Verifying without touching the switches

```bash
python3 tests/send_test_datagram.py 192.168.0.155 6343 192.168.4.106
# wait for the 60s flush, then:
psql -h 192.168.0.146 -U claude -d switchboard -c "SELECT * FROM sflow_flows ORDER BY id DESC LIMIT 5"
```

## Switch-side configuration

Both switches need **per-interface** enabling; a global enable samples
nothing, which is exactly how this looked configured-but-silent at first.

**Dell OS9** — `show sflow` reports `0 UDP packets exported` until each
interface is enabled:
```
sflow collector 192.168.0.155 agent-addr <switch-ip>
sflow enable
interface TenGigabitEthernet 1/1
 sflow enable
```

**Junos** — needs `interfaces` and `sample-rate`, and the config must be
**committed**: `show protocols sflow` in `[edit]` shows the *candidate*,
while operational `show sflow` will still say `sFlow is not Configured`.
```
set protocols sflow collector 192.168.0.155 udp-port 6343
set protocols sflow sample-rate ingress 1000
set protocols sflow interfaces ge-0/0/0
commit
```

Point both at **6343** (the sFlow default and what sfacctd binds). Junos
defaults elsewhere if `udp-port` is set to something else, and a collector
listening on the wrong port looks identical to a switch not sending.

## Agent identity

sFlow identifies a switch by its **agent-id**, which is not necessarily its
management address — it is often a loopback or router-id. The EX3300 here
initially reported `192.168.5.10` while being registered at `192.168.4.1`,
which made it unselectable in the UI's filter and left its flows
unattributed.

Two consequences, both handled:

- The UI's agent filter is built from the flows themselves, not the device
  registry, so an agent always appears even when it can't be matched to a
  device (shown as `unrecognised agent`).
- An unidentified agent gets **no vendor ifIndex decode at all**. Defaulting
  to one vendor's arithmetic would risk a real-looking but wrong port name.

Setting `set protocols sflow agent-id <management-ip>` keeps the two
aligned and is worth doing, but nothing breaks if they differ.

## Reading the numbers honestly

sFlow *samples* — one packet in N (32768 by default on this S4048). Byte
counts are estimates scaled from those samples, not measurements. They are
meaningful in aggregate and in relative terms; they are not a billing
record, and the UI says so.

## ifIndex → port names

sFlow reports interfaces as ifIndex integers. Dell OS9 encodes physical
ports arithmetically, verified against the real switch (`Interface index
is …` for Te 1/1, 1/37, 1/38, 1/48 — all four matched):

```
ifIndex = 2097156 + (port - 1) * 128
```

Anything off that stride, or from a non-OS9 platform, is shown as a raw
ifIndex rather than a guessed name — a confidently wrong port label is
worse than a number.
