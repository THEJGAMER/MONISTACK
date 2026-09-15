# Loki (the LXC at 192.168.0.145)

Receives structured events from Vector (see [../syslog/](../syslog/README.md))
and is what the webui's Syslog tab and Alarm History read from.

`loki-config.yaml` here is the **deployed copy** of
`/etc/loki/loki-config.yaml` on that LXC — same convention as
`syslog/vector.yaml`: treat this file as the source of truth and push
changes to the host, not the other way around.

It's in the repo because it wasn't, and that was the gap: the config
existed on exactly one machine with no copy anywhere. Losing that LXC
meant reconstructing it from memory — which matters more than usual given
this is the store that's deliberately keeping logs forever (below).

## Retention: deliberately none

There is **no** `retention_period`, compactor, or table_manager here, and
that is a decision rather than an oversight — everything is kept.

Measured on the real deployment before deciding (2026-08-23): ~2 MB/day
(37 MB over 17 days of real ingest at 2,769 lines/hour) against 6.8 GB
free on an 8 GB disk. That's roughly **9 years** of headroom, so retention
would be solving a problem that doesn't exist.

What would change the picture — worth checking `df -h` on the host if any
of these happen, rather than reviewing on a schedule:

- the APs start shipping syslog (they were configured to, but have never
  appeared in Loki)
- a new, chattier class of device is added
- a flapping link produces a sustained log storm

The webui's **Syslog flow** health check (Settings page) makes a *stop* in
volume visible. It does not watch for a *surge*, and disk is what a surge
would consume.

## Version

Loki 2.9.2, `schema: v13`, `store: tsdb`, filesystem object store under
`/var/lib/loki`. Single-node: `replication_factor: 1`, in-memory ring.

## Query fan-out limits (added 2026-09-15)

Loki's defaults assume a cluster of queriers behind a frontend. This is one
process on one core, and with the defaults one open Console tab - a 7-day
hardware-alarm query every 20 seconds - turned into **337 querier
executions per query** (`split_queries_by_interval` defaulted to 30m, so
168h became 336 pieces) into a scheduler queue capped at **100**. The
overflow logged "too many outstanding requests", the frontend's own retries
piled on, and the webui's 3-second syslog pollers - which never took over
5s themselves - were cancelled behind it and retried. Measured: a quiet
Loki did 39 queries/min with 0 errors; one open tab took it to 69/min and
**45 errors/min**. Of ~803k range queries over nine days, none took over 5s
- nothing was slow, it was all queue.

`loki-config.yaml` now sets `split_queries_by_interval: 24h` (matching the
index period, so 7 days is 7 pieces), `max_outstanding_requests_per_tenant:
2048`, `parallelise_shardable_queries: false` (sharding is for many
queriers; with one it is queue pressure and nothing else), and
`max_query_parallelism: 4`. Same tab, same load, after: **1 error in 5
minutes, 8 executions per 7-day query.** Vector saw one 500 during the
restart, retried it, and ingest never paused.

The webui side stopped retrying timeouts (a timeout means Loki is still
working; retrying doubles the load at the worst moment), gave the pollers a
backoff, moved the Console page's alarm-history fetch off its 20-second
tick onto its own 5-minute timer with a visible window control, and capped
any Loki-backed request at 30 days.

## Redeploying after an edit

```bash
scp loki-config.yaml root@192.168.0.145:/etc/loki/loki-config.candidate.yaml
ssh root@192.168.0.145 'cp /etc/loki/loki-config.yaml /etc/loki/loki-config.yaml.bak-$(date +%Y%m%d%H%M%S) && \
  mv /etc/loki/loki-config.candidate.yaml /etc/loki/loki-config.yaml && \
  systemctl restart loki && sleep 3 && systemctl is-active loki'
curl -s http://192.168.0.145:3100/ready
```

Loki has no `--dry-run`/validate subcommand, so unlike Vector there's no
pre-flight check — the backup above is what makes a bad edit recoverable.
