"""Saved topology "baseline" - the normal-state snapshot that live LLDP
fetches get diffed against, so an infra change (a moved cable, a dropped
LAG member, a new neighbor) shows up as a flagged difference instead of
silently becoming the new normal without anyone noticing. A single row,
backed by the same shared Postgres database as everything else (db.py) -
there's one fleet, one baseline.

Stored as a list of structural edge signatures (see topology.py's
`edge_signature`) - never state/utilization, which changes constantly and
isn't what "did the wiring change" means.
"""
import json
from datetime import datetime, timezone


class TopologyStore:
    def __init__(self, db):
        self.db = db

    def get(self):
        row = self.db.query_one("SELECT data, saved_at, saved_by FROM topology_baseline WHERE id = 'default'")
        if not row:
            return None
        return {"edges": json.loads(row["data"]), "saved_at": row["saved_at"], "saved_by": row["saved_by"]}

    def save(self, edge_signatures, saved_by):
        """Full relearn - overwrites the baseline with exactly what's live
        right now."""
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO topology_baseline (id, data, saved_at, saved_by) VALUES ('default', %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, saved_at = EXCLUDED.saved_at,
                   saved_by = EXCLUDED.saved_by""",
            (json.dumps(edge_signatures), now, saved_by),
        )

    def accept(self, added, removed, saved_by):
        """Manually folds specific drift into the baseline - `added`
        (edge signatures now present that should be considered normal from
        now on) get appended, `removed` (edge signatures no longer present)
        get dropped - without touching anything else in the baseline the
        way a full relearn would. No-op (creates an empty baseline first)
        if nothing's been saved yet."""
        current = self.get()
        edges = list(current["edges"]) if current else []
        removed_set = {json.dumps(e, sort_keys=True) for e in removed}
        edges = [e for e in edges if json.dumps(e, sort_keys=True) not in removed_set]
        existing_keys = {json.dumps(e, sort_keys=True) for e in edges}
        for e in added:
            key = json.dumps(e, sort_keys=True)
            if key not in existing_keys:
                edges.append(e)
                existing_keys.add(key)
        self.save(edges, saved_by)

    def clear(self):
        self.db.execute("DELETE FROM topology_baseline WHERE id = 'default'")
