"""Minimal read-only client for Loki's HTTP query API - stdlib only
(urllib), no new dependency. This is the "syslog input from vector": the
switch already sends syslog to Vector on the LXC at 192.168.0.144, and
Vector's already-live `loki_sink` (see syslog/vector.yaml) ships the
structured, interpreted events to Loki at 192.168.0.145:3100. Reading them
back from Loki - rather than SSHing into the LXC to tail a log - reuses
the parsing Vector already did (facility/severity/mnemonic/event_category/
link_state) instead of redoing it here.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class LokiError(Exception):
    pass


class LokiClient:
    def __init__(self, base_url, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query_range(self, filters=None, limit=200, since_seconds=3600):
        """`filters` is a list of already-trusted LogQL field-filter
        fragments (e.g. `event_category="auth"`, `alarm_component != ""`) -
        never raw request text; callers validate/construct these themselves
        (see app.py's VALID_CATEGORIES check for the category case). They're
        pushed into the LogQL query itself via Loki's `| json` parser stage,
        so Loki applies the filter *before* truncating to `limit` lines.

        An earlier version fetched the last `limit` raw lines and filtered
        in Python afterward - on a switch where one category (auth)
        dominates recent traffic, that silently starved out every other
        category's results even when they existed further back, since
        they'd already been truncated away before the filter ever ran.
        Verified live against the real Loki instance before shipping:
        `{job="syslog"} | json | event_category="auth"` returns real
        matching events directly from Loki."""
        query = '{job="syslog"}'
        if filters:
            query += " | json | " + " | ".join(filters)
        end = time.time()
        start = end - since_seconds
        params = {
            "query": query,
            "limit": str(limit),
            "start": str(int(start * 1e9)),
            "end": str(int(end * 1e9)),
            "direction": "backward",
        }
        url = f"{self.base_url}/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise LokiError(str(e)) from e

        events = []
        for stream in data.get("data", {}).get("result", []):
            for ts_ns, line in stream.get("values", []):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    parsed = {"message": line}
                parsed["_timestamp_ns"] = ts_ns
                events.append(parsed)
        events.sort(key=lambda e: int(e.get("_timestamp_ns", 0)), reverse=True)
        return events
