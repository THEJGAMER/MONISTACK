"""The topology cache: the page reads a cache, a thread fills it.

Before this the page crawled every device on every load and every 30
seconds - four SSH commands per device, one device at a time. What is
pinned here: a cached read does not crawl, `refresh=1` does, the cache
carries its own age, and two refreshes cannot run at once.
"""
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import app as app_module  # noqa: E402


@pytest.fixture
def crawls(monkeypatch):
    calls = []

    def fake_fetch():
        calls.append(time.time())
        return {"nodes": [], "edges": []}

    monkeypatch.setattr(app_module, "_fetch_live_topology", fake_fetch)
    monkeypatch.setattr(app_module, "_lag_health", lambda edges: [])
    monkeypatch.setattr(app_module, "TOPOLOGY_STORE", type("S", (), {"get": lambda self: None})())
    monkeypatch.setattr(app_module.topology, "diff_against_baseline", lambda edges, base: None)
    monkeypatch.setattr(app_module, "_TOPOLOGY_CACHE",
                        {"result": None, "fetched_at": None, "error": None, "refreshing": False})
    # A crawl needs something to crawl; the fetch itself is faked above.
    monkeypatch.setattr(app_module, "DEVICES", [object()])
    return calls


def test_a_refresh_fills_the_cache_with_a_timestamp(crawls):
    assert app_module._refresh_topology_cache() is True

    c = app_module._TOPOLOGY_CACHE
    assert c["result"]["nodes"] == [] and isinstance(c["fetched_at"], datetime)
    assert c["error"] is None and c["refreshing"] is False


def test_a_cached_read_does_not_crawl(crawls):
    app_module._refresh_topology_cache()
    n = len(crawls)

    r = app_module.api_topology(refresh=0, user="x")

    assert len(crawls) == n, "the whole point: reading the page costs no SSH"
    assert r["age_seconds"] >= 0 and r["fetched_at"] and r["refreshing"] is False


def test_the_first_read_crawls_when_nothing_is_cached(crawls):
    app_module.api_topology(refresh=0, user="x")

    assert len(crawls) == 1


def test_refresh_one_forces_a_crawl(crawls):
    app_module._refresh_topology_cache()
    n = len(crawls)

    app_module.api_topology(refresh=1, user="x")

    assert len(crawls) == n + 1


def test_a_failed_crawl_keeps_the_last_good_result_and_reports_the_error(crawls, monkeypatch):
    app_module._refresh_topology_cache()

    def boom():
        raise RuntimeError("ssh exploded")

    monkeypatch.setattr(app_module, "_fetch_live_topology", boom)
    assert app_module._refresh_topology_cache() is False

    r = app_module.api_topology(refresh=0, user="x")
    assert r["nodes"] == [] and "ssh exploded" in r["last_error"]


def test_two_refreshes_cannot_overlap(crawls, monkeypatch):
    """Two Refresh-now clicks, or the loop and a click, must not double
    the SSH load - the second simply finds one in progress."""
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        release.wait(2)
        return {"nodes": [], "edges": []}

    monkeypatch.setattr(app_module, "_fetch_live_topology", slow_fetch)
    t = threading.Thread(target=app_module._refresh_topology_cache)
    t.start()
    started.wait(2)

    assert app_module._refresh_topology_cache() is False   # declined, not queued
    release.set()
    t.join(3)
    assert app_module._TOPOLOGY_CACHE["refreshing"] is False


def test_a_crawl_with_no_devices_is_not_cached(crawls, monkeypatch):
    """Seen live: the first page request landed before devices had loaded,
    the empty crawl was cached, and the page showed no links for a full
    refresh interval."""
    monkeypatch.setattr(app_module, "DEVICES", [])

    assert app_module._refresh_topology_cache() is False
    assert app_module._TOPOLOGY_CACHE["result"] is None
    assert "no devices" in app_module._TOPOLOGY_CACHE["error"]
    assert crawls == []
