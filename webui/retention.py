"""Data retention - one place that decides how long each table keeps rows.

Every table here grew unbounded until this existed (ROADMAP 0.3). The one
apparent exception, `metric_samples`, only *looked* covered: `trending.py`
has had a 90-day prune since early on, but its only caller slept 24 hours
before its first run and there was no startup call, so on a process that
restarts more often than daily - which a webui redeployed several times a
day certainly is - it had realistically never run at all. That table was
found at 2.03M rows / 493 MB, ~3x its size six days earlier. The prune
here runs at startup *and* daily, which is what its docstring always
claimed.

Two rules shape the policies below, and both are about not destroying
things a person deliberately created:

1. **Deliberate keeps outlive automatic ones.** A saved result someone
   clicked Save on is not the same as the auto-saved copy of every command
   ever run, and they must not share a lifetime.
2. **Never cascade over human records.** `alarm_acks` and `alarm_comments`
   are `ON DELETE CASCADE` against `alert_occurrences`, so a naive
   "delete old occurrences" silently destroys acknowledgements and
   incident discussion. Confirmed real: a manual cleanup of ~20,800 junk
   occurrences had to explicitly exclude rows carrying that data, and
   would otherwise have taken 8 comments and 2 acks with it.

`audit_log` is deliberately the longest-lived and is never trimmed by the
aggressive default: it is the record of who did what, and an audit trail
that quietly deletes itself is worth very little.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webui.retention")


def _days(env_var, default):
    """Retention windows are env-tunable per deployment - a lab keeping 30
    days and a site that needs a year shouldn't need different code. 0 or
    negative disables that table's prune entirely, which is the honest way
    to say "keep forever" rather than setting an absurd number."""
    try:
        return int(os.environ.get(env_var, default))
    except (TypeError, ValueError):
        log.warning("%s is not an integer - falling back to %s days", env_var, default)
        return default


class Policy:
    """One table's retention rule. `sql` must be a DELETE parameterised by
    exactly one cutoff timestamp."""

    def __init__(self, name, env_var, default_days, sql, note=""):
        self.name = name
        self.env_var = env_var
        self.default_days = default_days
        self.sql = sql
        self.note = note

    @property
    def keep_days(self):
        return _days(self.env_var, self.default_days)


# Occurrences are matched on started_at, and any occurrence still carrying
# human records is excluded outright rather than by age - see rule 2 above.
_OCCURRENCE_SQL = """
DELETE FROM alert_occurrences
 WHERE started_at < %s
   AND resolved_at IS NOT NULL
   AND id NOT IN (SELECT occurrence_id FROM alarm_comments WHERE occurrence_id IS NOT NULL)
   AND id NOT IN (SELECT occurrence_id FROM alarm_acks     WHERE occurrence_id IS NOT NULL)
   AND id NOT IN (SELECT occurrence_id FROM audit_log      WHERE occurrence_id IS NOT NULL)
"""

# Only the auto-saved copies age out. A result someone explicitly saved is
# a deliberate keep and is left alone entirely by this policy.
_RESULTS_SQL = "DELETE FROM results WHERE created_at < %s AND auto_saved = 1"

POLICIES = [
    # metric_samples is split in two because one class of series utterly
    # dominates it. Measured on a real 3-device fleet: the four per-port
    # interface series (input/output mbps, input/output errors) across ~105
    # ports were 1.91M of 2.03M rows - 94% - while optics and PSU power
    # together were ~123k. Most of those ports are legitimately unused,
    # which is the same observation that made interface *alerting* opt-in
    # per port; trending records them all regardless, so their history is
    # mostly a very long run of zeroes.
    #
    # A single window therefore forces a bad trade: short enough to control
    # the interface series throws away optic history that costs almost
    # nothing to keep and is far more useful (a slowly degrading transceiver
    # is exactly the trend you want months of). Splitting lets each be set
    # on its own merits.
    Policy(
        "metric_samples (interface)", "RETAIN_IFACE_SAMPLES_DAYS", 30,
        "DELETE FROM metric_samples WHERE recorded_at < %s AND metric LIKE 'iface\\_%%'",
        note="~94% of all samples; ~105 ports x 4 series, most ports unused",
    ),
    Policy(
        "metric_samples (other)", "RETAIN_METRIC_SAMPLES_DAYS", 180,
        "DELETE FROM metric_samples WHERE recorded_at < %s AND metric NOT LIKE 'iface\\_%%'",
        note="optics/PSU - low volume, high diagnostic value over long periods",
    ),
    Policy(
        "alert_history", "RETAIN_ALERT_HISTORY_DAYS", 90,
        "DELETE FROM alert_history WHERE received_at < %s",
        note="raw Alertmanager webhook log; the durable record is alert_occurrences",
    ),
    Policy(
        "alert_occurrences", "RETAIN_OCCURRENCES_DAYS", 180,
        _OCCURRENCE_SQL,
        note="resolved only, and never one carrying acks/comments/audit entries",
    ),
    Policy(
        "results", "RETAIN_AUTOSAVED_RESULTS_DAYS", 90,
        _RESULTS_SQL,
        note="auto-saved only - explicitly saved results are never pruned",
    ),
    Policy(
        "command_history", "RETAIN_COMMAND_HISTORY_DAYS", 90,
        "DELETE FROM command_history WHERE ts < %s",
        note="per-user working list; audit_log keeps the durable record of the same runs",
    ),
    Policy(
        "audit_log", "RETAIN_AUDIT_LOG_DAYS", 365,
        "DELETE FROM audit_log WHERE ts < %s",
        note="longest by design - set RETAIN_AUDIT_LOG_DAYS=0 to keep forever",
    ),
]


def prune_all(db, dry_run=False):
    """Applies every policy. Returns [{table, keep_days, deleted, skipped}].

    Never raises: this runs on a background thread, and one malformed
    policy or a transient DB error must not kill the loop that would
    otherwise succeed tomorrow. Each table is independent for the same
    reason - one failing doesn't skip the rest.
    """
    if db is None:
        return []
    results = []
    for policy in POLICIES:
        keep_days = policy.keep_days
        if keep_days <= 0:
            results.append({"table": policy.name, "keep_days": keep_days,
                            "deleted": 0, "skipped": "retention disabled"})
            continue
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        try:
            if dry_run:
                # Same predicate, counted rather than applied - so a
                # policy can be checked against real data before it
                # deletes anything.
                count_sql = policy.sql.replace("DELETE FROM", "SELECT COUNT(*) AS n FROM", 1)
                row = db.query_one(count_sql, (cutoff.isoformat(),))
                deleted = (row or {}).get("n", 0)
            else:
                cur = db.execute(policy.sql, (cutoff.isoformat(),))
                deleted = getattr(cur, "rowcount", 0) or 0
            results.append({"table": policy.name, "keep_days": keep_days,
                            "deleted": deleted, "skipped": None})
            if deleted:
                log.info("retention: %s %d row(s) older than %d days from %s",
                         "would delete" if dry_run else "deleted", deleted, keep_days, policy.name)
        except Exception as e:
            log.warning("retention: could not prune %s: %s", policy.name, e, exc_info=True)
            results.append({"table": policy.name, "keep_days": keep_days,
                            "deleted": 0, "skipped": str(e)})
    return results
