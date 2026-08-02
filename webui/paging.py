"""Paging control - deciding *when* an alarm reaches a human's pager.

By default an alarm pages the instant it fires, which leaves no room to
look at it first: a link that flaps for ten seconds wakes someone up before
anyone can see it already recovered. This module puts a short, visible hold
in front of paging - a countdown an operator can watch, extend while
investigating, cancel entirely, or skip to page immediately.

The hold is implemented as an Alertmanager silence, deliberately, rather
than as a Switchboard-side notification queue. Alertmanager stays the thing
that actually delivers pages, so the failure modes stay sane:

  - Switchboard down / slow / mid-restart: no hold is created, and the
    alarm pages immediately through the normal Alertmanager receivers.
    "Pages sooner than you wanted" is a safe failure for a pager;
    "silently never pages" is not, and that is what a Switchboard-owned
    delivery queue would risk every time this app restarted.
  - Hold created but Switchboard then dies: the silence carries its own
    expiry, so Alertmanager lifts it on schedule with no further help.

The race that matters: a silence must exist *before* Alertmanager
dispatches, and with group_wait at 0s the webhook that tells us an alarm
fired arrives after the page has already gone out. So holds are placed
ahead of the fire, from the two places that see a condition coming:

  - Prometheus rule alerts spend their `for:` window in "pending", which
    the scheduler loop below watches and pre-silences.
  - Interface alerts are posted to Alertmanager by this app itself
    (interface_alerting.py), so the hold goes on immediately before the
    post.

An alarm that slips through either path pages immediately - see above, that
is the intended direction to fail in.
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("webui.paging")

# Label names that must not go into a silence matcher: they vary between a
# pending Prometheus alert and the fired one Alertmanager ends up holding,
# so matching on them would produce a silence that quietly matches nothing.
_VOLATILE_LABELS = {"alertname"}


class PagingController:
    def __init__(self, alertmanager, default_delay_seconds=120):
        self.alertmanager = alertmanager
        self.default_delay_seconds = default_delay_seconds

    # --- matchers ---------------------------------------------------

    @staticmethod
    def matchers_for(labels):
        """An exact-match silence on the full label set. Every label is
        included (alertname too - see below), because a silence built from
        a subset would suppress more than the one alarm it is meant to
        hold: silencing `{alertname=InterfaceDown}` alone would hold back
        every port on every device."""
        return [
            {"name": k, "value": str(v), "isRegex": False, "isEqual": True}
            for k, v in sorted((labels or {}).items())
        ]

    # --- holds ------------------------------------------------------

    def hold_until(self, labels, until, comment, created_by="switchboard"):
        """Silences this exact alarm until `until`. Returns the silence id.

        Alertmanager updates an existing silence in place when given its
        id, but creating a fresh one for an already-held alarm is harmless
        (silences are additive - the alarm stays suppressed while any of
        them is active), so this does not need to find-and-update."""
        now = datetime.now(timezone.utc)
        if until <= now:
            return None
        try:
            result = self.alertmanager.create_silence(
                self.matchers_for(labels),
                now.isoformat(),
                until.isoformat(),
                created_by,
                comment,
            )
        except Exception:
            log.warning("could not place paging hold for %s", labels, exc_info=True)
            return None
        return (result or {}).get("silenceID")

    def hold_for_default(self, labels, comment=None):
        """The standard investigation window applied when an alarm first
        appears. Returns (silence_id, page_at) - page_at being when the
        hold lifts, which is what the UI counts down to."""
        return self.hold_for_duration(labels, self.default_delay_seconds, comment)

    def hold_for_duration(self, labels, delay_seconds, comment=None):
        """Same as hold_for_default, but for an explicit duration - used
        when a rule has its own page_delay_seconds (see alert_rules.py's
        Rules tab) overriding the app-wide default. Returns (silence_id,
        page_at), both None if the duration means "don't hold at all"."""
        if delay_seconds <= 0:
            return None, None
        page_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        comment = comment or f"Paging held {delay_seconds}s for investigation (Switchboard)"
        return self.hold_until(labels, page_at, comment), page_at

    def release(self, silence_id):
        """Lifts a hold early - "page now". Deleting the silence makes the
        alarm active again and Alertmanager notifies on its next dispatch.
        A silence that is already gone (expired on its own, or deleted from
        Alertmanager's UI) is not an error: the end state is what was
        wanted either way."""
        if not silence_id:
            return True
        try:
            self.alertmanager.delete_silence(silence_id)
            return True
        except Exception:
            log.warning("could not release paging hold %s", silence_id, exc_info=True)
            return False

    def hold_indefinitely(self, labels, created_by, reason):
        """Paging off for this alarm (NARG). Not open-ended: a silence with
        no end date is a way to lose an alarm permanently by accident, so
        this is a long, finite hold that will eventually lapse and page
        again if nobody has dealt with it. Deliberate - the point is to
        stop the pager tonight, not to delete the alarm."""
        until = datetime.now(timezone.utc) + timedelta(hours=24)
        return self.hold_until(labels, until, f"Paging disabled: {reason}", created_by)
