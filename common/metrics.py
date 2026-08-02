"""Prometheus metrics for Switchboard's own health (ROADMAP 0.4) - not the
switch's metrics (that's exporter/exporter.py's `s4048_*` namespace, a
completely separate process monitoring the device itself), but this app's
own operational signals: is polling actually succeeding, how long is it
taking, is SSH churning through reconnects, is Loki slow, how much is the
Console actually being used. Exposed at `/metrics` (see app.py) for the
same Prometheus instance already scraping the exporter (see
prometheus/prometheus.yml) - this app was, until now, a monitoring tool
that couldn't itself be monitored.

Kept as its own module (no dependency on app.py or any other Switchboard
module) specifically so ssh_client.py and loki_client.py - both used
outside a request context (background polling, standalone scripts) - can
import and increment these directly without a circular import.
"""
from prometheus_client import Counter, Histogram

poll_success_total = Counter(
    "switchboard_poll_success_total", "Successful status polls", ["device_id"]
)
poll_failure_total = Counter(
    "switchboard_poll_failure_total", "Failed status polls", ["device_id"]
)
poll_duration_seconds = Histogram(
    "switchboard_poll_duration_seconds", "Status poll duration in seconds", ["device_id"]
)

# Every device's SwitchSSH instance is long-lived (one per device, reused
# for the app's lifetime - see app.py's "Session model") - only the first
# successful connect() on a given instance is a fresh login; every one
# after that means the persistent session dropped and had to be
# re-established, which is the actual operational signal this is meant to
# catch (a healthy device settles to 0/min once its session is up).
ssh_reconnect_total = Counter(
    "switchboard_ssh_reconnect_total", "SSH sessions re-established after the first successful connect", ["host"]
)

loki_query_duration_seconds = Histogram(
    "switchboard_loki_query_duration_seconds", "Loki query_range() call duration in seconds"
)
loki_query_failure_total = Counter(
    "switchboard_loki_query_failure_total", "Failed Loki queries"
)

command_run_total = Counter(
    "switchboard_command_run_total", "Commands run from the Console", ["device_id", "platform"]
)
command_run_duration_seconds = Histogram(
    "switchboard_command_run_duration_seconds", "Command run duration in seconds", ["device_id"]
)

# Alertmanager's webhook receiver (ROADMAP 3.2) posts here on every
# firing/resolved notification - see alertmanager/alertmanager.yml's
# placeholder receiver docstring for why this exists instead of a real
# Slack/email/PagerDuty destination. Counting them (rather than just
# logging) makes "is alerting actually flowing end to end" itself
# observable from this same /metrics endpoint.
alertmanager_notifications_total = Counter(
    "switchboard_alertmanager_notifications_total",
    "Alertmanager webhook notifications received",
    ["alertname", "status"],
)
