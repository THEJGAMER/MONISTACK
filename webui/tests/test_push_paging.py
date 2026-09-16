"""Pager semantics on top of Web Push: repeat until acknowledged, stop
everywhere when anyone acknowledges, cap so nothing pages forever.

The send is faked; these pin the decisions. Against a real Postgres per
test, because the page ledger (push_pages) and the ON CONFLICT counting
are the substance of "repeat" and "cap".
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import push  # noqa: E402

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")
DDL = """
CREATE TABLE push_subscriptions (
    endpoint TEXT PRIMARY KEY, subscription TEXT NOT NULL, username TEXT NOT NULL, label TEXT,
    min_severity TEXT NOT NULL DEFAULT 'warning', notify_resolved INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_used_at TIMESTAMPTZ,
    failures INTEGER NOT NULL DEFAULT 0, last_error TEXT,
    repeat_minutes INTEGER NOT NULL DEFAULT 5, max_repeats INTEGER NOT NULL DEFAULT 12);
CREATE TABLE push_pages (
    occurrence_id BIGINT NOT NULL, endpoint TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
    first_paged_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_paged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (occurrence_id, endpoint));
"""


class _DB:
    def __init__(self, conn):
        self.conn = conn

    def _cur(self):
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur

    def query(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchall()

    def query_one(self, sql, params=()):
        cur = self._cur(); cur.execute(sql, params); return cur.fetchone()


@pytest.fixture
def store():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=4)
    except Exception:
        pytest.skip("test Postgres not reachable")
    conn.autocommit = True
    schema = f"test_pg_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"'); cur.execute(f'SET search_path TO "{schema}"'); cur.execute(DDL)
    try:
        yield push.PushSubscriptionStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _sub(endpoint):
    return {"endpoint": endpoint, "keys": {"p256dh": "P", "auth": "A"}}


class _Keys:
    available = True
    public_key = "pk"
    private_key_pem = "pem"
    subject = "mailto:x@example.com"


def _occ(id=7, severity="critical", started_minutes_ago=30):
    started = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
    return {"id": id, "alertname": "FanFailure", "severity": severity, "summary": "Fan tray 2 down",
            "device": "s4048", "started_at": started.isoformat(), "paged_at": started.isoformat()}


def _notifier(store, sent):
    return push.PushNotifier(store, _Keys(), send_fn=lambda sub, payload: (sent.append((sub["endpoint"], payload)), (True, None, False))[1])


def _age_page(store, occ_id, endpoint, minutes):
    """Pretend the last page happened `minutes` ago."""
    store.db.execute("UPDATE push_pages SET last_paged_at = now() - (%s || ' minutes')::interval WHERE occurrence_id = %s AND endpoint = %s",
                     (str(minutes), occ_id, endpoint))


# --- the first page is remembered -----------------------------------

def test_the_first_page_is_recorded_per_device(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info")
    sent = []
    _notifier(store, sent)("alarm.paged", {"occurrence": _occ()})

    pages = store.pages_for(7)
    assert pages["https://push/a"]["count"] == 1


# --- repeat until acknowledged ---------------------------------------

def test_no_repeat_before_the_interval(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info", repeat_minutes=5)
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ()})
    _age_page(store, 7, "https://push/a", 2)

    assert n.repeat_due([_occ()]) == 0


def test_a_repeat_fires_once_the_interval_has_passed_and_says_so(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info", repeat_minutes=5)
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ()})
    _age_page(store, 7, "https://push/a", 6)

    assert n.repeat_due([_occ(started_minutes_ago=30)]) == 1
    title, body = sent[-1][1]["title"], sent[-1][1]["body"]
    assert title.startswith("[page 2]") and "Unacknowledged for 30 min" in body
    assert sent[-1][1]["tag"] == sent[0][1]["tag"], "same tag: it replaces the notification rather than stacking"
    assert store.pages_for(7)["https://push/a"]["count"] == 2


def test_repeat_off_means_once_only(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info", repeat_minutes=0)
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ()})
    _age_page(store, 7, "https://push/a", 60)

    assert n.repeat_due([_occ()]) == 0


def test_the_cap_stops_a_forgotten_alarm(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info", repeat_minutes=1, max_repeats=3)
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ()})
    for _ in range(5):
        _age_page(store, 7, "https://push/a", 2)
        n.repeat_due([_occ()])

    assert store.pages_for(7)["https://push/a"]["count"] == 3
    assert len(sent) == 3


def test_the_severity_floor_applies_to_repeats_too(store):
    store.upsert(_sub("https://push/crit"), "a", min_severity="critical", repeat_minutes=1)
    sent = []
    n = _notifier(store, sent)

    assert n.repeat_due([_occ(severity="warning")]) == 0
    assert sent == []


def test_a_device_enrolled_after_the_alarm_opened_is_paged_on_the_next_pass(store):
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ()})          # nobody enrolled yet
    store.upsert(_sub("https://push/late"), "a", min_severity="info")

    assert n.repeat_due([_occ()]) == 1
    assert sent[-1][0] == "https://push/late" and not sent[-1][1]["title"].startswith("[page")


# --- one acknowledgement stops every pager --------------------------

def test_an_ack_closes_the_page_on_every_device_that_was_paged(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info")
    store.upsert(_sub("https://push/b"), "b", min_severity="info")
    store.upsert(_sub("https://push/never"), "c", min_severity="critical")   # floor too high: never paged
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ(severity="warning")})
    sent.clear()

    n("alarm.acknowledged", {"occurrence": _occ(severity="warning"), "by": "jacob", "note": "on it"})

    targets = sorted(e for e, _ in sent)
    assert targets == ["https://push/a", "https://push/b"], "only devices that were paged get the close"
    p = sent[0][1]
    assert p["close"] is True and p["title"].startswith("Acknowledged by jacob") and p["body"] == "on it"
    assert store.pages_for(7) == {}, "the ledger is cleared, so no repeat can follow"


def test_after_an_ack_nothing_repeats(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info", repeat_minutes=1)
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ()})
    n("alarm.acknowledged", {"occurrence": _occ(), "by": "jacob"})
    sent.clear()

    # the caller only passes *unacknowledged* alarms; an acked one is simply absent
    assert n.repeat_due([]) == 0
    assert sent == []


def test_a_resolve_clears_the_ledger(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info")
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ()})

    n("alarm.resolved", {"occurrence": _occ()})

    assert store.pages_for(7) == {}


def test_ledger_rows_for_alarms_no_longer_open_are_pruned(store):
    """The resolve event clears the ledger as it happens; the repeater's
    pass prunes whatever a restart or a lost event left, keyed on what is
    still open. Found live: two rows survived a resolve that never emitted."""
    store.upsert(_sub("https://push/a"), "a", min_severity="info")
    sent = []
    n = _notifier(store, sent)
    n("alarm.paged", {"occurrence": _occ(id=7)})
    n("alarm.paged", {"occurrence": _occ(id=8)})

    assert store.prune_pages({8}) == 1
    assert store.pages_for(7) == {} and store.pages_for(8) != {}
    assert store.prune_pages(set()) == 1, "nothing open: nothing may remain"
    assert store.pages_for(8) == {}


# --- preferences and removal -----------------------------------------

def test_prefs_can_change_without_resubscribing(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="warning", repeat_minutes=5, max_repeats=12)

    row = store.update_prefs("https://push/a", "critical", False, 2, 30)

    assert (row["min_severity"], row["notify_resolved"], row["repeat_minutes"], row["max_repeats"]) == ("critical", False, 2, 30)


def test_prefs_are_validated(store):
    store.upsert(_sub("https://push/a"), "a")
    for bad in [("loud", True, 5, 12), ("warning", True, -1, 12), ("warning", True, 5, 0), ("warning", True, 999, 12)]:
        with pytest.raises(ValueError):
            store.update_prefs("https://push/a", *bad)


def test_prefs_for_an_unknown_device_is_none_not_an_error(store):
    assert store.update_prefs("https://push/nope", "warning", True, 5, 12) is None


def test_removing_a_device_stops_it_being_paged(store):
    store.upsert(_sub("https://push/a"), "a", min_severity="info")
    assert store.remove("https://push/a") is True
    sent = []
    _notifier(store, sent)("alarm.paged", {"occurrence": _occ()})

    assert sent == []
    assert store.remove("https://push/a") is False
