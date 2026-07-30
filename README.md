# S4048 SSH-polled monitoring stack

Monitors the Dell EMC S4048-ON (`192.168.4.106`) purely over SSH `show`
commands instead of SNMP. Stack: a custom Python exporter (Prometheus format)
+ Prometheus + Grafana, all run via Docker Compose.

## How it works

`exporter/ssh_client.py` opens one interactive SSH shell to the switch,
logs in, runs `enable` (same password), and disables paging. It then reuses
that session to run `show` commands on a loop, reconnecting automatically if
the session drops.

`exporter/exporter.py` polls two groups on that session:

- **Fast (every 30s, `FAST_POLL_INTERVAL`)**: `show processes cpu`,
  `show memory`, `show environment`, `show interfaces status`.
- **Slow (every 300s, `TRANSCEIVER_POLL_INTERVAL`)**: per-port
  `show interfaces <port> transceiver` optical diagnostics (temp, voltage,
  bias current, Tx/Rx power, alarm flags) for all 54 ports. This is
  sequential over one SSH session, so it's deliberately not run every cycle.

Metrics are served on `:9101/metrics` for Prometheus to scrape.

## Running it

```
docker compose up -d --build
```

- Exporter metrics: http://localhost:9101/metrics
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin / value of `GRAFANA_ADMIN_PASSWORD` in `.env`, default `admin`)
  - Dashboard "Dell S4048-ON (SSH polled)" is auto-provisioned.

Credentials live in `.env` (gitignored, not committed). `.env.example` shows
the shape.

## Metrics exposed

- `s4048_up` — 1 if the last SSH poll cycle succeeded
- `s4048_cpu_utilization_percent{core,window}`
- `s4048_memory_bytes{type}`
- `s4048_fan_status{unit,bay,fan}` / `s4048_fan_speed_rpm{...}`
- `s4048_psu_status{unit,bay}` / `s4048_psu_power_watts{unit,bay,kind}`
- `s4048_unit_temperature_celsius{unit}` / `s4048_sensor_temperature_celsius{sensor}`
- `s4048_interface_up{port,description}` / `s4048_interface_speed_mbps{port}`
- `s4048_transceiver_present{port}`, `_temperature_celsius`, `_voltage_volts`,
  `_tx_bias_ma`, `_tx_power_dbm`, `_rx_power_dbm`, `_alarm{port,flag}`

## Notes

- The switch's `admin` account lands in unprivileged EXEC (`>`); the
  exporter escalates with `enable` using the same password
  (`SWITCH_ENABLE_PASS` env var can override if it's ever set differently).
- The account only has read (`show`) commands run against it — nothing in
  this stack issues config-mode commands.
- The temp password used to set this up was shared in plaintext in chat;
  worth rotating it on the switch once you're done validating the stack.
