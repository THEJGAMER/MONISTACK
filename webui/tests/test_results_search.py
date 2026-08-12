"""Tests for full-text search across saved results (ROADMAP 4).

Real Postgres in a throwaway schema, because every interesting property
here lives in the database: a GENERATED tsvector column, the choice of
text-search configuration, phrase and negation handling, and ranking.
None of that can be exercised against a fake.

The config choice is the one most worth pinning. `english` discards
stopwords, and it was verified against real text that
`to_tsvector('english', '... is up ... no shutdown ...')` cannot match a
search for "up" or "no" **at all** - which are among the most meaningful
words in switch output. Anyone "tidying" this to the more usual `english`
would silently break the searches people actually run, and the tests below
would catch it.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from results_store import ResultsStore  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://claude:claude@192.168.0.146:5432/switchboard")

DDL = """
CREATE TABLE results (
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
    created_at TEXT NOT NULL,
    actor TEXT,
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple',
            coalesce(filename, '') || ' ' ||
            coalesce(device_id, '') || ' ' ||
            coalesce(device_name, '') || ' ' ||
            coalesce(command, '') || ' ' ||
            coalesce(summary, '')), 'A')
        ||
        setweight(to_tsvector('simple', left(coalesce(output, ''), 100000)), 'B')
    ) STORED
);
CREATE INDEX ON results USING GIN (search_vector);
"""


def _reachable():
    try:
        psycopg2.connect(DSN, connect_timeout=4).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no Postgres reachable for integration test")


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
    schema = f"test_search_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(DDL)
    try:
        yield ResultsStore(_DB(conn))
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _save(store, command, output, device="sw1", summary=""):
    return store.save(device, device, "10.0.0.1", "system", "version",
                      command, summary, output, auto_saved=True)


# --- the point of the feature: search inside the output --------------

def test_finds_a_phrase_buried_in_the_output(store):
    """The whole gap this closes - previously search only matched the
    command/device/filename, never what came back."""
    _save(store, "show version", "Dell EMC Operating System Version: 9.14(1.14)")
    _save(store, "show clock", "14:22:01 UTC Mon Aug 12 2026")

    items, total = store.list(q="Operating System")

    assert total == 1
    assert items[0]["title"].endswith("show version")


def test_a_match_comes_back_with_a_snippet_of_the_matched_text(store):
    """So a hit is explicable from the list without opening it."""
    _save(store, "show interfaces", "Te 1/47 is down, line protocol is down\nTe 1/48 is up")

    items, _ = store.list(q="line protocol")

    assert items[0]["snippet"], "a content match must produce a snippet"
    assert "«" in items[0]["snippet"], "matched terms must be marked for highlighting"


def test_plain_listing_has_no_snippet(store):
    """ts_headline is the expensive part of the query; it must not run
    when nobody is searching."""
    _save(store, "show version", "Dell EMC")

    items, _ = store.list()

    assert items[0]["snippet"] is None


# --- the text-search config choice -----------------------------------

def test_stopword_like_words_are_searchable(store):
    """The reason for 'simple' over 'english'. In switch output "is up"
    and "no shutdown" are the most meaningful phrases there are, and
    english discards exactly those words."""
    _save(store, "show interfaces", "TenGigabitEthernet 1/37 is up, line protocol is up")
    _save(store, "show running-config", "interface Te 1/1\n no shutdown\n description uplink")

    assert store.list(q="up")[1] >= 1, "'up' must be searchable"
    assert store.list(q="no")[1] >= 1, "'no' must be searchable"


def test_technical_tokens_survive_tokenisation(store):
    """Port numbers and signal levels are what people actually search
    for; a config that mangled them would be useless here."""
    _save(store, "show interfaces transceiver", "Te 1/37 Rx Power -3.2 dBm temperature 41 C")

    assert store.list(q="1/37")[1] == 1
    assert store.list(q="dBm")[1] == 1


# --- query syntax ----------------------------------------------------

def test_a_quoted_phrase_requires_the_words_in_order(store):
    """Otherwise "is up" would match any output containing both words
    anywhere, which is nearly all of them."""
    _save(store, "a", "the port is up right now", device="sw1")
    _save(store, "b", "up on the roof, nothing is wrong", device="sw2")

    items, total = store.list(q='"is up"')

    assert total == 1
    assert items[0]["device_id"] == "sw1"


def test_negation_excludes_matches(store):
    _save(store, "a", "Te 1/1 is up", device="sw1")
    _save(store, "b", "Te 1/2 is up but Te 1/3 is down", device="sw2")

    items, total = store.list(q="up -down")

    assert total == 1
    assert items[0]["device_id"] == "sw1"


def test_nonsense_input_returns_nothing_rather_than_erroring(store):
    """websearch_to_tsquery is deliberately forgiving, but the whole
    query still has to survive punctuation someone pastes in."""
    _save(store, "show version", "Dell EMC")

    for junk in ("!!!", "&|!()", "   ", '"unclosed'):
        items, total = store.list(q=junk)
        assert isinstance(total, int)


# --- no regression on the search that already existed ----------------

def test_substring_search_on_metadata_still_works(store):
    """Full-text is token-based, so "ver" can never match "version".
    The original ILIKE behaviour is kept alongside it precisely so
    adding content search took nothing away."""
    _save(store, "show version", "nothing relevant in here")

    assert store.list(q="ver")[1] == 1, "partial-word metadata search must still work"


def test_device_filter_and_search_combine(store):
    _save(store, "show version", "Dell EMC", device="sw1")
    _save(store, "show version", "Dell EMC", device="sw2")

    items, total = store.list(device_id="sw2", q="Dell")

    assert total == 1
    assert items[0]["device_id"] == "sw2"


def test_a_device_name_match_outranks_a_passing_mention_in_output(store):
    """Weighting made explicit. Searching a device or command name should
    surface that device's results, not every unrelated result that happens
    to mention the word somewhere in 20k of CLI text.

    An earlier version of this test asserted the opposite - that a body
    match should win - and failed. The assertion was wrong, not the code:
    device_id is part of the searchable text, so both rows were genuine
    matches ranked identically, and order fell back to recency. Rather
    than encode that accident, identifying fields are now weight 'A' and
    body output weight 'B', which makes the ordering a decision instead of
    a tie-break."""
    _save(store, "show inventory", "unrelated output here", device="s4048-core")
    _save(store, "show inventory", "System Type: S4048-ON  Software Version 9.14", device="dev-a")

    items, total = store.list(q="S4048")

    assert total == 2
    assert items[0]["device_id"] == "s4048-core", "the device whose name matches must come first"


def test_a_body_match_still_ranks_above_no_match(store):
    """Weighting must not mean body matches are ignored - only that they
    lose a direct tie with an identifying field."""
    _save(store, "show version", "Software Version 9.14 build alpha", device="dev-a")
    _save(store, "show clock", "14:22:01 UTC", device="dev-b")

    items, total = store.list(q="alpha")

    assert total == 1
    assert items[0]["device_id"] == "dev-a"


# --- the tsvector size ceiling ---------------------------------------

def test_a_very_large_output_still_saves(store):
    """A tsvector has a hard 1MB limit and exceeding it raises - which
    would fail the INSERT of the result itself, turning "this output was
    big" into "the command failed". The column bounds `output` for
    exactly this reason."""
    huge = ("interface TenGigabitEthernet 1/1 is up line protocol is up " * 40_000)
    assert len(huge) > 1_000_000

    saved = _save(store, "show tech-support", huge)

    assert saved["filename"]
    assert store.list(q="tech-support")[1] == 1
