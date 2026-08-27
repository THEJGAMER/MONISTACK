"""Tests for the reverse/forward DNS cache behind the traffic views.

The properties worth pinning are all about *not* letting the resolver
dictate whether a page renders: a page needs ~100 lookups, and
socket.gethostbyaddr is a libc call that ignores Python's socket timeout,
so a single unresponsive PTR can hold a thread for seconds.
"""
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import dns_cache  # noqa: E402


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setenv("DNS_LOOKUP_ENABLED", "true")
    return dns_cache.DnsCache()


def _answers(mapping, delay=0.0):
    def fake(ip):
        if delay:
            time.sleep(delay)
        if ip in mapping:
            return (mapping[ip], [], [ip])
        raise socket.herror(1, "Unknown host")
    return fake


def test_it_resolves_what_it_can(cache, monkeypatch):
    monkeypatch.setattr(socket, "gethostbyaddr", _answers({"8.8.8.8": "dns.google"}))

    assert cache.reverse_many(["8.8.8.8"]) == {"8.8.8.8": "dns.google"}


def test_an_address_with_no_ptr_is_simply_absent(cache, monkeypatch):
    """Most public addresses have no PTR at all - that is normal, not an
    error, and must not surface as one."""
    monkeypatch.setattr(socket, "gethostbyaddr", _answers({}))

    assert cache.reverse_many(["104.22.127.97"]) == {}


def test_a_miss_is_cached_so_it_is_not_retried_every_page_load(cache, monkeypatch):
    calls = []

    def counting(ip):
        calls.append(ip)
        raise socket.herror(1, "Unknown host")

    monkeypatch.setattr(socket, "gethostbyaddr", counting)
    cache.reverse_many(["10.0.0.1"])
    cache.reverse_many(["10.0.0.1"])
    cache.reverse_many(["10.0.0.1"])

    assert len(calls) == 1, "negative caching is what keeps a wall of unnamed IPs cheap"


def test_a_hit_is_served_from_cache(cache, monkeypatch):
    calls = []

    def counting(ip):
        calls.append(ip)
        return ("dns.google", [], [ip])

    monkeypatch.setattr(socket, "gethostbyaddr", counting)
    cache.reverse_many(["8.8.8.8"])
    cache.reverse_many(["8.8.8.8"])

    assert len(calls) == 1


def test_an_expired_entry_is_looked_up_again(cache, monkeypatch):
    monkeypatch.setattr(dns_cache, "POSITIVE_TTL", -1)
    calls = []

    def counting(ip):
        calls.append(ip)
        return ("dns.google", [], [ip])

    monkeypatch.setattr(socket, "gethostbyaddr", counting)
    cache.reverse_many(["8.8.8.8"])
    cache.reverse_many(["8.8.8.8"])

    assert len(calls) == 2


def test_a_slow_resolver_does_not_hold_the_page(cache, monkeypatch):
    """The property this module exists for. A lookup that takes longer
    than the deadline must not extend the request - it returns without
    it, and the answer lands in the cache for next time."""
    monkeypatch.setattr(socket, "gethostbyaddr", _answers({"8.8.8.8": "dns.google"}, delay=2.0))

    started = time.time()
    out = cache.reverse_many(["8.8.8.8"], deadline=0.2)
    elapsed = time.time() - started

    assert elapsed < 1.0, f"waited {elapsed:.2f}s on a 0.2s deadline"
    assert out == {}


def test_the_straggler_still_populates_the_cache(cache, monkeypatch):
    """It is not cancelled, so the next request gets it for free -
    otherwise a resolver slower than the deadline would never resolve
    anything at all, no matter how many times the page was loaded."""
    monkeypatch.setattr(socket, "gethostbyaddr", _answers({"8.8.8.8": "dns.google"}, delay=0.3))

    cache.reverse_many(["8.8.8.8"], deadline=0.01)
    time.sleep(0.6)

    assert cache.reverse_many(["8.8.8.8"], deadline=0.01) == {"8.8.8.8": "dns.google"}


def test_addresses_no_resolver_can_answer_are_skipped(cache, monkeypatch):
    """Multicast is common in flow data; asking for its PTR spends a
    worker on a guaranteed miss while a real address waits behind it."""
    calls = []
    monkeypatch.setattr(socket, "gethostbyaddr", lambda ip: calls.append(ip) or (_ for _ in ()).throw(socket.herror()))

    cache.reverse_many(["224.0.0.251", "255.255.255.255", "0.0.0.0", "not-an-ip"])

    assert calls == []


def test_lookups_can_be_turned_off_entirely(cache, monkeypatch):
    monkeypatch.setenv("DNS_LOOKUP_ENABLED", "false")

    assert cache.reverse_many(["8.8.8.8"]) == {}
    assert cache.forward("google.com") == []


# --- what counts as a hostname to search for -------------------------

def test_an_address_is_never_treated_as_a_name():
    """Otherwise a precise address search gets sent to the resolver, and
    a wildcard-answering resolver turns it into a match on something
    else."""
    assert not dns_cache.looks_like_hostname("192.168.0.125")
    assert not dns_cache.looks_like_hostname("8.8.8.8")
    assert not dns_cache.looks_like_hostname("fe80::1")


def test_a_bare_word_is_not_a_hostname():
    """"https" and "smb" are service names the search already handles;
    resolving them would be a pointless round trip at best."""
    assert not dns_cache.looks_like_hostname("https")
    assert not dns_cache.looks_like_hostname("")


def test_a_dotted_name_is_a_hostname():
    assert dns_cache.looks_like_hostname("google.com")
    assert dns_cache.looks_like_hostname("OPNsense-Pad-Syd.internal")


def test_something_that_could_not_be_a_name_is_rejected():
    assert not dns_cache.looks_like_hostname("a b.com")
    assert not dns_cache.looks_like_hostname("x" * 300 + ".com")
