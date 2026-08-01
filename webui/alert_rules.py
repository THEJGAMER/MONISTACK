"""Prometheus alert rule definitions, Postgres-backed (ROADMAP 3.2's Rules
tab on the Alerts page). Postgres is the source of truth; prometheus/
alerts.yml becomes a *generated* file from here on - Prometheus itself
still needs an actual file on disk to load rules from, this module just
owns writing it and telling Prometheus to reload.

Only `severity` (which drives Pushover priority via alertmanager.yml's
`{{ if eq .CommonLabels.severity "critical" }}...` template) and
`enabled` are editable from the UI. `expr`/`for_seconds` are not: a
PromQL typo has no pre-flight validation before Prometheus rejects the
whole rules file at reload time, which is a worse failure mode than a
slightly-wrong severity label - see README/ROADMAP for the reasoning.
"""
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml

log = logging.getLogger("webui.alert_rules")

# The 5 rules from prometheus/alerts.yml as originally written and
# verified live (see ROADMAP 2026-08-01 entries) - used to seed the table
# the first time it's empty, so an existing deployment doesn't lose its
# real, tested rules just because this table is new.
_SEED_RULES = [
    {
        "name": "S4048DeviceDown",
        "expr": "s4048_up == 0",
        "for_seconds": 300,
        "severity": "critical",
        "summary_template": "S4048 poll failing",
        "description_template": "The exporter's last SSH poll of the S4048 failed - device may be unreachable, or credentials/SSH config drifted.",
    },
    {
        "name": "S4048FanDown",
        "expr": "s4048_fan_status == 0",
        "for_seconds": 120,
        "severity": "warning",
        "summary_template": "Fan {{ $labels.fan }} down (unit {{ $labels.unit }}, bay {{ $labels.bay }})",
        "description_template": "show environment reports this fan as down.",
    },
    {
        "name": "S4048PSUDown",
        "expr": "s4048_psu_status == 0",
        "for_seconds": 120,
        "severity": "critical",
        "summary_template": "PSU {{ $labels.unit }}/{{ $labels.bay }} down",
        "description_template": "show environment reports this power supply as down - if the other PSU is also degraded this is a single point of failure.",
    },
    {
        "name": "S4048TransceiverAlarm",
        "expr": "s4048_transceiver_alarm == 1 and on(port) s4048_interface_up == 1",
        "for_seconds": 300,
        "severity": "warning",
        "summary_template": "Transceiver alarm on {{ $labels.port }} ({{ $labels.flag }})",
        "description_template": "The optic's own DOM alarm bit is set (show interfaces transceiver) on a link that's currently up - Rx/Tx power, temperature, voltage, or bias current is outside the vendor's programmed threshold for this specific optic.",
    },
    {
        "name": "S4048InterfaceFlapping",
        "expr": "changes(s4048_interface_up[15m]) > 4",
        "for_seconds": 0,
        "severity": "warning",
        "summary_template": "{{ $labels.port }} ({{ $labels.description }}) is flapping",
        "description_template": "This interface's link state changed more than 4 times in the last 15 minutes.",
    },
]


class AlertRuleStore:
    def __init__(self, db):
        self.db = db
        self._seed_if_empty()

    def _seed_if_empty(self):
        existing = self.db.query_one("SELECT COUNT(*) AS n FROM alert_rules")
        if existing and existing["n"] > 0:
            return
        now = datetime.now(timezone.utc).isoformat()
        for r in _SEED_RULES:
            self.db.execute(
                """INSERT INTO alert_rules
                   (name, expr, for_seconds, severity, summary_template, description_template, enabled, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 1, %s)
                   ON CONFLICT (name) DO NOTHING""",
                (r["name"], r["expr"], r["for_seconds"], r["severity"], r["summary_template"], r["description_template"], now),
            )

    def list(self):
        rows = self.db.query("SELECT * FROM alert_rules ORDER BY name")
        return [self._to_dict(r) for r in rows]

    def update(self, name, severity=None, enabled=None):
        if severity is not None:
            if severity not in ("warning", "critical"):
                raise ValueError("severity must be 'warning' or 'critical'")
            self.db.execute("UPDATE alert_rules SET severity = %s, updated_at = %s WHERE name = %s",
                             (severity, datetime.now(timezone.utc).isoformat(), name))
        if enabled is not None:
            self.db.execute("UPDATE alert_rules SET enabled = %s, updated_at = %s WHERE name = %s",
                             (1 if enabled else 0, datetime.now(timezone.utc).isoformat(), name))
        row = self.db.query_one("SELECT * FROM alert_rules WHERE name = %s", (name,))
        return self._to_dict(row) if row else None

    @staticmethod
    def _to_dict(row):
        return {
            "name": row["name"],
            "expr": row["expr"],
            "for_seconds": row["for_seconds"],
            "severity": row["severity"],
            "summary_template": row["summary_template"],
            "description_template": row["description_template"],
            "enabled": bool(row["enabled"]),
            "updated_at": row["updated_at"],
        }


def render_yaml(rules):
    """Builds the prometheus/alerts.yml content from enabled rules only -
    a disabled rule simply isn't in the file Prometheus loads, since
    Prometheus has no native "disabled" flag on a rule."""
    prom_rules = []
    for r in rules:
        if not r["enabled"]:
            continue
        rule = {"alert": r["name"], "expr": r["expr"], "labels": {"severity": r["severity"]},
                 "annotations": {"summary": r["summary_template"], "description": r["description_template"]}}
        if r["for_seconds"]:
            rule["for"] = f"{r['for_seconds']}s"
        prom_rules.append(rule)
    doc = {"groups": [{"name": "s4048-hardware", "rules": prom_rules}]}
    header = (
        "# GENERATED by Switchboard's Alerts > Rules tab - do not hand-edit.\n"
        "# Source of truth is the alert_rules Postgres table; this file is\n"
        "# regenerated and Prometheus is live-reloaded on every save there.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def write_and_reload(rules, file_path, prometheus_reload_url, timeout=5):
    """Writes the generated YAML in place (no rename - see ROADMAP's
    2026-08-01 note on single-file Docker bind mounts losing host edits
    that go through an atomic rename) and asks Prometheus to reload via
    its /-/reload lifecycle endpoint (needs --web.enable-lifecycle, see
    docker-compose.yml) instead of requiring the container be recreated."""
    with open(file_path, "w") as f:
        f.write(render_yaml(rules))

    req = urllib.request.Request(prometheus_reload_url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.URLError as e:
        log.error("wrote %s but Prometheus reload failed: %s", file_path, e)
        raise
