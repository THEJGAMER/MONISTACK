"""Postgres-backed storage for everything Switchboard persists: devices added
through the UI, and saved command results. Was SQLite on a Docker volume
until this app outgrew "one file, one process" (see ROADMAP.md Phase 2) -
this is the same storage layer, same interface, pointed at a real database.

One shared connection guarded by a lock: this is still an internal ops
console at low request volume, not a case for a connection pool. What's
different from the SQLite version is that a network database connection
can actually drop (a local file never does), so every method retries once
after reconnecting if it hits a dead connection.
"""
import threading

import psycopg2
import psycopg2.extras

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS results (
    filename TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    device_name TEXT NOT NULL,
    host TEXT NOT NULL,
    category_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command TEXT NOT NULL,
    summary TEXT,
    output TEXT NOT NULL,
    markdown TEXT NOT NULL,
    auto_saved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_device ON results(device_id);
CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at);

CREATE TABLE IF NOT EXISTS topology_baseline (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    saved_by TEXT
);

-- Trend samples for predictive/trending monitoring (see trending.py):
-- optic Rx/Tx power + temperature, PSU power draw, interface utilization
-- and error/discard counts. One row per (device, metric, port) per sample
-- tick - `port` is NULL for device-wide metrics (none yet, but kept
-- nullable rather than using a sentinel string). Written on the status
-- poller's slow (transceiver) cadence, not every fast poll, to keep row
-- growth bounded (~288 samples/day/metric/port instead of ~2880).
CREATE TABLE IF NOT EXISTS metric_samples (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    port TEXT,
    value DOUBLE PRECISION NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metric_samples_lookup
    ON metric_samples(device_id, metric, port, recorded_at);

-- Scheduled/recurring command runs (ROADMAP 3.6). Output feeds the same
-- `results` table above (auto_saved=1) rather than a separate log, so
-- scheduled config-backup/compliance-style runs show up in Saved Results
-- and Console's "Recent results" the same way any other run does.
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    params TEXT,
    interval_minutes INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    last_error TEXT,
    next_run_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compliance_config (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

-- Prometheus alert rule definitions (ROADMAP 3.2's Rules tab) - Postgres
-- is the source of truth; prometheus/alerts.yml is a generated file
-- (see alert_rules.py) kept only because Prometheus itself needs an
-- actual file on disk to load rules from. `expr`/`for_seconds` are
-- seeded from the rules verified live in the original alerting work and
-- deliberately NOT editable from the UI (a PromQL typo has no
-- pre-flight validation) - only `severity` (drives Pushover priority
-- via alertmanager.yml's template) and `enabled` are.
CREATE TABLE IF NOT EXISTS alert_rules (
    name TEXT PRIMARY KEY,
    expr TEXT NOT NULL,
    for_seconds INTEGER NOT NULL DEFAULT 0,
    severity TEXT NOT NULL,
    summary_template TEXT NOT NULL,
    description_template TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

-- Per-interface down-alerting config (ROADMAP 3.2's Interfaces tab) -
-- unlike prometheus/alerts.yml's fleet-wide rules, this is genuinely
-- per-(device,port): which specific interfaces should alert at all (most
-- ports are legitimately unused/down and shouldn't), and whether a
-- down transition alerts immediately or only after staying down for
-- `delay_seconds` (checked, not just slept - see interface_alerting.py).
-- Evaluated by a Switchboard-side loop (reusing status_poller.py's
-- already-polled interface state, no extra SSH) that posts straight to
-- Alertmanager's /api/v2/alerts, rather than being expressed as
-- PromQL - a dynamic per-port rule set isn't something Prometheus rule
-- files are a good fit for.
CREATE TABLE IF NOT EXISTS interface_alert_rules (
    device_id TEXT NOT NULL,
    port TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'immediate',
    delay_seconds INTEGER NOT NULL DEFAULT 60,
    severity TEXT NOT NULL DEFAULT 'warning',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (device_id, port)
);
-- severity was added after interface_alert_rules already shipped - this
-- table predates it on any deployment that ran the earlier migration, so
-- CREATE TABLE IF NOT EXISTS above is a no-op there and this catches it up.
ALTER TABLE interface_alert_rules ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'warning';

-- Alert history (ROADMAP 3.2's History tab) - every notification
-- Alertmanager sends its webhook receiver (app.py's
-- /api/alertmanager/webhook) gets persisted here, covering both
-- Prometheus-rule-based alerts (prometheus/alerts.yml) and the
-- directly-posted per-interface alerts (interface_alerting.py) - both
-- route through the same Alertmanager receiver, so this one table is a
-- complete history regardless of which of the two alerting paths raised
-- it. Alertmanager's own /api/v2/alerts only shows currently-active (or
-- very recently resolved) alerts, not history - this is what makes past
-- alerts queryable after they've resolved and aged out there.
CREATE TABLE IF NOT EXISTS alert_history (
    id BIGSERIAL PRIMARY KEY,
    alertname TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT,
    summary TEXT,
    labels TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_history_received ON alert_history(received_at DESC);
"""


class Database:
    def __init__(self, dsn):
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn = None
        self._connect()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(SCHEMA)
            self._conn.commit()

    def _connect(self):
        self._conn = psycopg2.connect(self._dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        self._conn.autocommit = False

    def _with_reconnect(self, fn):
        """Run `fn(cursor)` under the lock, retrying once after a fresh
        reconnect if the connection turned out to be dead - a network
        Postgres box can drop an idle connection in ways a local SQLite
        file never could."""
        with self._lock:
            try:
                with self._conn.cursor() as cur:
                    result = fn(cur)
                self._conn.commit()
                return result
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._connect()
                with self._conn.cursor() as cur:
                    result = fn(cur)
                self._conn.commit()
                return result

    def execute(self, sql, params=()):
        def run(cur):
            cur.execute(sql, params)
            return cur.rowcount

        rowcount = self._with_reconnect(run)
        return _ExecResult(rowcount)

    def query(self, sql, params=()):
        def run(cur):
            cur.execute(sql, params)
            return cur.fetchall()

        return self._with_reconnect(run)

    def query_one(self, sql, params=()):
        def run(cur):
            cur.execute(sql, params)
            return cur.fetchone()

        return self._with_reconnect(run)


class _ExecResult:
    """Mirrors the bit of sqlite3's cursor that callers actually use
    (`cur.rowcount`) without exposing a psycopg2 cursor that's already
    been closed by the time `execute()` returns."""

    def __init__(self, rowcount):
        self.rowcount = rowcount
