"""Minimal client for Alertmanager's HTTP API v2 - stdlib only (urllib),
same style as loki_client.py. Alertmanager runs as its own service in this
docker-compose stack (see alertmanager/alertmanager.yml) - Prometheus is
wired to send it alerts (prometheus/prometheus.yml's `alerting:` block +
prometheus/alerts.yml's rules), and this module lets Switchboard read
active alerts and manage silences (ROADMAP 3.2's "maintenance windows")
without a separate UI - Alertmanager's own web UI still works too, this
just surfaces the same data/actions inside Switchboard next to everything
else.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class AlertmanagerError(Exception):
    pass


class AlertmanagerClient:
    def __init__(self, base_url, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise AlertmanagerError(f"Alertmanager {method} {path} -> HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise AlertmanagerError(f"Alertmanager unreachable: {e.reason}")

    def list_alerts(self):
        return self._request("GET", "/api/v2/alerts")

    def list_silences(self):
        return self._request("GET", "/api/v2/silences")

    def create_silence(self, matchers, starts_at, ends_at, created_by, comment):
        body = {
            "matchers": matchers,
            "startsAt": starts_at,
            "endsAt": ends_at,
            "createdBy": created_by,
            "comment": comment,
        }
        return self._request("POST", "/api/v2/silences", body)

    def delete_silence(self, silence_id):
        # Deliberately singular "/silence/" here, unlike list/create which
        # use "/silences/" - confirmed live against a real Alertmanager
        # instance after the plural form 404'd; Alertmanager's own API
        # really is inconsistent about this between the collection and
        # single-resource routes.
        self._request("DELETE", f"/api/v2/silence/{urllib.parse.quote(silence_id)}")

    def post_alerts(self, alerts):
        """Pushes alerts directly into Alertmanager (POST /api/v2/alerts) -
        used by interface_alerting.py's per-port down-alerting, which
        isn't expressed as a Prometheus rule (see that module's docstring
        for why). `alerts` is a list of {labels, annotations, startsAt,
        endsAt?} dicts - omitting endsAt keeps an alert active; posting the
        same labels again with endsAt in the past resolves it, same
        mechanism used to manually resolve a test alert during the
        original alerting work."""
        self._request("POST", "/api/v2/alerts", alerts)
