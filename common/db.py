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
-- Added after alert_rules already shipped (same catch-up pattern as
-- interface_alert_rules.severity above) - how long this specific rule's
-- alarms are held before paging (see paging.py), overriding the app-wide
-- PAGE_DELAY_SECONDS default. NULL means "use the app-wide default", not
-- "page instantly" - a rule that has never had this touched should not
-- silently start paging immediately once the column exists.
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS page_delay_seconds INTEGER;

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
-- Added after alert_history already shipped (same catch-up pattern as
-- interface_alert_rules.severity above). Lets a history row be correlated
-- with the acknowledgement/audit records for the same alert identity,
-- which are keyed by fingerprint rather than by the (mutable) summary text.
ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS fingerprint TEXT;
CREATE INDEX IF NOT EXISTS idx_alert_history_fingerprint ON alert_history(fingerprint);

-- One row per *occurrence* of an alarm: a single fired-to-resolved
-- episode, with its own id. This is the unit everything else hangs off,
-- and the reason is record-keeping: if a port flaps four times, that is
-- four separate things that happened, each with its own acknowledgement,
-- its own discussion and its own audit trail. Collapsing them into one
-- long-lived row per alarm signature loses that separation - you can no
-- longer say who handled the second occurrence versus the fourth.
--
-- `signature` is the label-set fingerprint (alert_acks.fingerprint_for).
-- It deliberately does NOT identify the occurrence; it groups occurrences
-- of the same underlying alarm so a new one can link back to its
-- predecessors, the way a ticketing system opens a fresh ticket and
-- references the previous ones rather than reopening a closed one.
--
-- Rows are created and closed from the Alertmanager webhook
-- (app.py's /api/alertmanager/webhook), which every alert path in this
-- app already funnels through - both Prometheus rules and the
-- directly-posted per-interface alerts.
CREATE TABLE IF NOT EXISTS alert_occurrences (
    id BIGSERIAL PRIMARY KEY,
    signature TEXT NOT NULL,
    alertname TEXT NOT NULL,
    severity TEXT,
    summary TEXT,
    labels TEXT NOT NULL,
    started_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_occurrences_signature ON alert_occurrences(signature, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_occurrences_started ON alert_occurrences(started_at DESC);
-- At most one open occurrence per signature: a second "firing" for an
-- alarm that is already open is the same episode being re-notified
-- (Alertmanager repeat_interval, or this app's own heartbeat), not a new
-- one. Enforced in the database rather than only in code, because the
-- webhook can be delivered concurrently.
CREATE UNIQUE INDEX IF NOT EXISTS idx_occurrences_one_open
    ON alert_occurrences(signature) WHERE resolved_at IS NULL;

-- Paging control (see paging.py). An alarm is held back from paging for a
-- short investigation window rather than paging the instant it fires:
--   page_at         when the hold lifts and it pages (NULL = already paged)
--   paged_at        when it actually paged
--   paging_disabled operator turned paging off for this occurrence (NARG)
--   silence_id      the Alertmanager silence currently holding it back
-- The hold is an Alertmanager silence, not a Switchboard-side queue, so
-- Alertmanager remains the thing that actually delivers pages: if
-- Switchboard is down, no hold gets created and the alarm pages
-- immediately. The failure mode is "pages sooner than you wanted", never
-- "never pages", which is the right way round for a pager.
ALTER TABLE alert_occurrences ADD COLUMN IF NOT EXISTS page_at TEXT;
ALTER TABLE alert_occurrences ADD COLUMN IF NOT EXISTS paged_at TEXT;
ALTER TABLE alert_occurrences ADD COLUMN IF NOT EXISTS paging_disabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alert_occurrences ADD COLUMN IF NOT EXISTS silence_id TEXT;

-- Acknowledgement of a single occurrence (not of the alarm in general):
-- acknowledging today's flap says nothing about tomorrow's, which is the
-- whole point of keeping occurrences separate. Deliberately NOT a
-- silence - the alarm keeps firing and still notifies on state changes;
-- an ack only records that a human has taken it.
CREATE TABLE IF NOT EXISTS alarm_acks (
    occurrence_id BIGINT PRIMARY KEY REFERENCES alert_occurrences(id) ON DELETE CASCADE,
    acked_by TEXT NOT NULL,
    acked_at TEXT NOT NULL,
    note TEXT
);

-- Audit/event log: every operator-initiated mutation in the app (alert
-- ack/unack/manual resolve, silence create/expire, alert-rule and
-- interface-alert config changes), with who did it and when. Distinct
-- from alert_history, which records what the *system* did (notifications
-- Alertmanager sent); this records what *people* did, which is the half
-- that was previously only visible in container logs.
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts DESC);
-- Alert-related audit entries carry the alert's fingerprint so one alarm's
-- entire operator timeline (acks, notes, manual resolves) can be pulled by
-- alarm identity - this is what makes the per-alarm "ticket" view possible
-- without scanning and re-hashing every row's detail JSON. NULL for entries
-- that aren't about a specific alert (rule/config changes).
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS fingerprint TEXT;
CREATE INDEX IF NOT EXISTS idx_audit_log_fingerprint ON audit_log(fingerprint);

-- Discussion thread on a single alarm (the Communication tab) - the
-- human back-and-forth while an incident is being worked, kept separate
-- from audit_log on purpose. audit_log answers "who changed what, and
-- can I trust this record" and is append-only; this is conversation,
-- where being able to delete your own typo is normal and expected.
-- Mixing them would mean either an audit trail with holes in it or a
-- discussion nobody can correct. Keyed by fingerprint (see alert_acks.py)
-- so a thread follows one specific alarm across every recurrence, which
-- is what makes a shared link to it useful to a colleague.
CREATE TABLE IF NOT EXISTS alarm_comments (
    id BIGSERIAL PRIMARY KEY,
    occurrence_id BIGINT NOT NULL REFERENCES alert_occurrences(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarm_comments_occurrence ON alarm_comments(occurrence_id, created_at);

-- Ties an audit entry to the specific occurrence it was about, so one
-- occurrence's operator history can be pulled without dragging in every
-- other occurrence of the same alarm. `fingerprint` (the signature) stays
-- alongside it for signature-wide queries.
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS occurrence_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_audit_log_occurrence ON audit_log(occurrence_id);

-- Superseded by the occurrence-scoped tables above, which arrived in the
-- same change - these were keyed by signature, which is exactly the
-- collapsing this redesign exists to undo. Dropped rather than left
-- behind so there is one obvious place acknowledgements and comments
-- live; both were empty at the point of the switch.
DROP TABLE IF EXISTS alert_acks;
DROP TABLE IF EXISTS alert_comments;

-- Every command run against a device, by whom, and what happened - the
-- record that per-user OIDC login was supposed to make possible but which
-- nothing actually wrote until this table existed (runs were logged to
-- stdout and nowhere else; `results` didn't even carry an actor).
--
-- Deliberately separate from audit_log, which also gets a "command.run"
-- entry for the same event. They answer different questions and are read
-- by different people: audit_log is the admin-only, append-only "who did
-- what, can I trust this record" trail with a uniform shape across every
-- action type; this is a per-user working history ("what did I run on
-- that switch an hour ago, run it again") that needs the structured
-- device/category/command/params columns to support filtering and
-- one-click re-run, which a generic detail-JSON column can't do without
-- reparsing every row.
--
-- Failures are recorded too (status='error'), not just successes - "I ran
-- it and it blew up" is exactly the thing worth finding again later, and
-- omitting it would make the history quietly lie about what was tried.
CREATE TABLE IF NOT EXISTS command_history (
    id BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_name TEXT,
    category_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command TEXT NOT NULL,
    params TEXT,
    status TEXT NOT NULL,
    error TEXT,
    duration_ms INTEGER,
    result_filename TEXT,
    source TEXT NOT NULL DEFAULT 'console'
);
CREATE INDEX IF NOT EXISTS idx_command_history_actor_ts ON command_history(actor, ts DESC);
CREATE INDEX IF NOT EXISTS idx_command_history_ts ON command_history(ts DESC);
CREATE INDEX IF NOT EXISTS idx_command_history_device ON command_history(device_id, ts DESC);

-- A user's pinned commands. device_id is nullable on purpose: most useful
-- favourites are "run this anywhere" (show version), but a port-specific
-- one (transceiver diagnostics on Te 1/37) only means anything against
-- the device that has that port, so both shapes have to be expressible.
CREATE TABLE IF NOT EXISTS command_favorites (
    id BIGSERIAL PRIMARY KEY,
    actor TEXT NOT NULL,
    label TEXT,
    device_id TEXT,
    category_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    params TEXT,
    created_at TEXT NOT NULL
);
-- One row per (user, device-or-any, command, params) - favouriting the
-- same thing twice is a no-op rather than a duplicate row. COALESCE on the
-- nullable columns because NULL never equals NULL in a unique index, which
-- would let "any device"/"no params" favourites duplicate freely.
CREATE UNIQUE INDEX IF NOT EXISTS idx_command_favorites_unique
    ON command_favorites(actor, COALESCE(device_id, ''), category_id, command_id, COALESCE(params, ''));
CREATE INDEX IF NOT EXISTS idx_command_favorites_actor ON command_favorites(actor, created_at DESC);

-- Who ran the command that produced a saved result. Nullable because rows
-- saved before this column existed genuinely have no attribution - showing
-- "-" for those is honest, backfilling a guess would not be.
ALTER TABLE results ADD COLUMN IF NOT EXISTS actor TEXT;

-- Last time *any* Switchboard instance observed this occurrence's alarm
-- actually active (firing in Alertmanager, or pending in Prometheus).
--
-- Exists because occurrence closing used to be absence-based: "anything
-- open that I can't currently see firing is over". That is only correct
-- if this process has a complete view, and it silently isn't in two real
-- cases. The one found live: two Switchboard instances sharing this
-- database (a dev stack and a deployed one, confirmed via
-- pg_stat_activity - two distinct client_addr, the second connected
-- continuously since 2026-08-02) each reconcile against their *own*
-- Alertmanager, so each kept closing occurrences the other had just
-- opened, which the other immediately reopened. That ping-pong produced
-- ~19,800 junk occurrence rows for a single continuously-down device.
-- The second case needs no second instance at all: a brief Alertmanager
-- or Prometheus blip empties the local view for one tick and closes every
-- open alarm spuriously.
--
-- With this column, closing becomes evidence-based - an occurrence is
-- only closed once nobody has seen it active for a grace period - so a
-- partial or momentarily-empty view can no longer end an alarm that is
-- still really happening.
ALTER TABLE alert_occurrences ADD COLUMN IF NOT EXISTS last_seen_at TEXT;
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
