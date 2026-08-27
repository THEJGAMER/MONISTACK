"""Reverse and forward DNS for the traffic views, with a cache in front.

A flow table is a wall of IP addresses. `192.168.0.1` and `8.8.8.8` are
recognisable; `142.250.70.100` is not, and neither is the third-party CDN
that a host is quietly talking to all day. Resolving them is what turns
"who is this" into an answer.

The whole design is shaped by one fact: **a page needs ~100 lookups and
must not wait for them.** `socket.gethostbyaddr` is a libc call with its
own timeout from resolv.conf, and it ignores `socket.setdefaulttimeout`,
so a single unresponsive PTR can block a thread for seconds no matter
what this module asks for. So lookups run in a bounded pool, the caller
waits only until a deadline, and whatever has not arrived comes back as
None - resolved by the time the next request asks, because the straggler
threads keep going and populate the cache rather than being cancelled.

Misses are cached too. Most public addresses have no PTR record at all
(confirmed here: Cloudflare and Microsoft ranges return nothing while
Google and the resolvers do), and without negative caching every page
load would retry every one of them forever.
"""
import ipaddress
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("webui.dns")

# PTR records change rarely; a long TTL keeps the resolver quiet. The
# negative TTL is shorter because "no name yet" is the more likely thing
# to change - a new host gets a DHCP lease and a name minutes later.
POSITIVE_TTL = int(os.environ.get("DNS_CACHE_TTL_SECONDS", str(6 * 3600)))
NEGATIVE_TTL = int(os.environ.get("DNS_NEGATIVE_TTL_SECONDS", "900"))

# Enough to cover a page's worth of addresses without opening a hundred
# sockets at once. These threads spend all their time blocked on the
# resolver, so this is about not swamping it rather than about CPU.
MAX_WORKERS = int(os.environ.get("DNS_LOOKUP_WORKERS", "16"))

# How long a request will wait. Past this it returns what it has; the
# rest lands in the cache for next time.
DEADLINE_SECONDS = float(os.environ.get("DNS_LOOKUP_DEADLINE", "1.5"))

# Bounded so a long-running process cannot accumulate an entry for every
# address on the internet.
MAX_ENTRIES = int(os.environ.get("DNS_CACHE_MAX_ENTRIES", "20000"))


def _enabled():
    return os.environ.get("DNS_LOOKUP_ENABLED", "true").strip().lower() not in ("0", "false", "no")


class DnsCache:
    """Reverse/forward lookups shared across requests.

    Deliberately process-local rather than a table in Postgres. It is a
    cache of something the network already answers quickly, it is worth
    nothing after a restart, and putting it in the database would add a
    write on every page view of a page that is otherwise read-only.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}        # ip -> (name_or_None, expires_at)
        self._inflight = set()  # ips currently being resolved
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                        thread_name_prefix="dns")
        self.hits = 0
        self.misses = 0

    # --- reverse ------------------------------------------------------

    def _cached(self, ip, now):
        entry = self._cache.get(ip)
        if entry is None or entry[1] <= now:
            return None, False
        return entry[0], True

    def _store(self, ip, name):
        expires = time.time() + (POSITIVE_TTL if name else NEGATIVE_TTL)
        with self._lock:
            if len(self._cache) >= MAX_ENTRIES:
                # Cheap eviction: drop whatever expires soonest. A strict
                # LRU would need a second structure for no real gain on a
                # cache this size.
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][1])[:MAX_ENTRIES // 10]:
                    self._cache.pop(k, None)
            self._cache[ip] = (name, expires)
            self._inflight.discard(ip)

    def _resolve_one(self, ip):
        try:
            name = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            name = None            # no PTR record, or the resolver said no
        except Exception:
            log.debug("reverse lookup failed for %s", ip, exc_info=True)
            name = None
        self._store(ip, name)
        return name

    def reverse_many(self, ips, deadline=DEADLINE_SECONDS):
        """{ip: hostname or None} for as many as resolve in time.

        An address absent from the result is not "has no name" - it is
        "not known yet". Callers render the address alone in that case,
        which is also what they do for a genuine miss, so the distinction
        never reaches the page.
        """
        if not _enabled():
            return {}
        now = time.time()
        out, pending = {}, []
        with self._lock:
            for ip in {str(i) for i in ips if i}:
                name, fresh = self._cached(ip, now)
                if fresh:
                    out[ip] = name
                    self.hits += 1
                    continue
                self.misses += 1
                if ip in self._inflight:
                    continue        # another request is already on it
                if not _is_lookupable(ip):
                    continue
                self._inflight.add(ip)
                pending.append(ip)

        if not pending:
            return {k: v for k, v in out.items() if v}

        futures = {ip: self._pool.submit(self._resolve_one, ip) for ip in pending}
        end = time.time() + deadline
        for ip, fut in futures.items():
            remaining = end - time.time()
            if remaining <= 0:
                break               # out of time; the thread still finishes
            try:
                name = fut.result(timeout=remaining)
                if name:
                    out[ip] = name
            except Exception:
                pass                # timed out here, still resolving there
        return {k: v for k, v in out.items() if v}

    # --- forward ------------------------------------------------------

    def forward(self, name, deadline=DEADLINE_SECONDS):
        """Addresses for a hostname, for searching by name.

        Returns a list because a name routinely has several, and matching
        only the first would silently miss most of a CDN's traffic.
        """
        if not _enabled() or not name:
            return []
        fut = self._pool.submit(_forward_lookup, name)
        try:
            return fut.result(timeout=deadline)
        except Exception:
            return []

    def stats(self):
        with self._lock:
            return {"entries": len(self._cache), "inflight": len(self._inflight),
                    "hits": self.hits, "misses": self.misses, "enabled": _enabled()}


def _forward_lookup(name):
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, None)})
    except Exception:
        return []


def _is_lookupable(ip):
    """Skip addresses no resolver can usefully answer for.

    Not an optimisation - multicast and broadcast destinations are common
    in flow data, and asking for their PTR wastes a worker on a guaranteed
    miss while a real address waits behind it.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_multicast or addr.is_unspecified or addr.is_reserved)


def looks_like_hostname(text):
    """Whether a search string should be forward-resolved.

    An address must never be treated as a name: "192.168.0.125" would
    otherwise be sent to the resolver, and a resolver that answers
    wildcard queries could turn a precise address search into a match on
    something else entirely.
    """
    text = (text or "").strip()
    if not text or len(text) > 253:
        return False
    try:
        ipaddress.ip_address(text)
        return False               # it is an address, not a name
    except ValueError:
        pass
    if "." not in text:
        return False               # bare words are far more often a service name
    return all(c.isalnum() or c in "-._" for c in text)
