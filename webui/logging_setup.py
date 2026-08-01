"""Structured (JSON) logging with a request/correlation ID (ROADMAP 0.4),
so a single command run - or any request - can be traced end to end across
every log line it touches (the route handler, ssh_client.py's connect/run
logging, etc.) without threading an id parameter through every function
signature.

Request scope is carried via a contextvar (`request_id_var`), set once per
request by app.py's middleware. Starlette dispatches sync route handlers
(this app's are all `def`, not `async def`) through anyio's
`to_thread.run_sync`, which copies the calling context into the worker
thread - confirmed this means the contextvar set in the (async) middleware
is still readable from synchronous code deep inside a route, including
ssh_client.py's logging, with no extra plumbing. Background poll threads
(status_poller.py) are NOT part of any request and never had the
contextvar set, so they correctly log request_id "-" - that's accurate,
not a bug.
"""
import contextvars
import json
import logging
import os

request_id_var = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging():
    """JSON is the default (this is what the ticket asked for, and this
    fleet already ships its switches' own syslog through a JSON pipeline -
    see syslog/ - so a log aggregator parsing this app's own logs the same
    way is the consistent choice). `LOG_FORMAT=text` switches to a plain
    human-readable line for local/interactive debugging, where "docker
    logs" output being JSON-per-line is more friction than help."""
    level = os.environ.get("LOG_LEVEL", "INFO")
    fmt = os.environ.get("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    if fmt == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"))
    else:
        handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
