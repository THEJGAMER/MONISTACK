"""Alert acknowledgements (ROADMAP 3.2) - "someone has seen this and is on
it", recorded against a specific alert identity with who/when/why.

Deliberately not a silence. A silence stops Alertmanager notifying at all,
which is the right tool for planned maintenance but the wrong one for a
live incident: the fault is still real, still needs to stay visible, and
still needs to notify if it changes state. An ack leaves all of that alone
and only answers the question the Active alerts table couldn't previously
answer - is anyone actually dealing with this, or has it been sitting
unowned?

Identity is the full label set, not the alert name. Alertmanager itself
fingerprints purely by labels (a lesson learned the hard way during the
2026-08-01 alerting work: a test alert reusing a real alert's labels was
genuinely indistinguishable from it), so acking `S4048PSUDown{bay=2}` must
not touch `S4048PSUDown{bay=1}` - two different power supplies, two
different faults, two different acks.

The fingerprint here is Switchboard's own, not Alertmanager's: the same
function has to fingerprint things Alertmanager has never seen - Prometheus
rules still inside their `for:` window, and interface_alerting.py's
delayed-mode ports still counting down (both surfaced as "pending" by
app.py's /api/alerts/live). Reusing Alertmanager's fingerprint where it
exists and inventing one elsewhere would mean the same logical alert
changed identity the moment it crossed `for:` - and any ack placed while it
was pending would silently detach at exactly the moment it started paging.
"""
import hashlib
import json
from datetime import datetime, timezone


def fingerprint_for(labels):
    """Stable short hash of a label set. Order-independent (dict iteration
    order must not change identity) and null-separated so that labels like
    {"a": "b=c"} and {"a=b": "c"} can't collide into the same string."""
    parts = "".join(f"{k}\0{v}\0" for k, v in sorted((labels or {}).items()))
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


# Acknowledgements and comments used to live here, keyed by signature.
# They moved to occurrences.py and are now keyed by occurrence id: an ack
# is a statement about one episode of an alarm, not about the alarm
# forever. This module is now purely "what counts as the same alarm".
