"""
Structured JSON logging.

One log line per event, JSON-encoded, written to stdout. Designed to be
shipped to Loki / similar without further processing.

We log application events (uploads, fetches, deletes, rate-limit hits)
through this. We do NOT pipe uvicorn's per-request access log through
JSON — uvicorn's own log is fine for low-level HTTP traffic, and trying
to JSONify access logs cleanly is more work than it's worth.
"""
import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Format LogRecord as a single-line JSON object."""

    # Standard LogRecord attributes we never want to copy into the payload.
    # We synthesize timestamp/level/message ourselves and drop the rest.
    _SKIP = {
        "args", "msg", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno",
        "funcName", "created", "msecs", "relativeCreated", "thread",
        "threadName", "processName", "process", "name", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
                  .isoformat().replace("+00:00", "Z"),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed via `extra={...}` lands as an attribute on the
        # record. We pull those into the payload at the top level.
        for k, v in record.__dict__.items():
            if k in self._SKIP or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure(debug: bool = False) -> None:
    """Wire up the root logger with JSON output."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # uvicorn has its own loggers; let them through but route to our handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


# Convenience: a logger pre-named for app events.
event_log = logging.getLogger("drop_pensa.event")


# Field names that are RESERVED on a stdlib LogRecord. Passing any of these
# via `extra={...}` raises KeyError at log time (and the request 500s).
# We prefix-rename them at the boundary so callers don't have to remember.
_RESERVED_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


def _safe_extra(action: str, fields: dict) -> dict:
    """Rename any field that would collide with LogRecord built-ins."""
    safe: dict = {"action": action}
    for k, v in fields.items():
        if k in _RESERVED_LOGRECORD_KEYS:
            safe["f_" + k] = v
        else:
            safe[k] = v
    return safe


def log_event(action: str, **fields) -> None:
    """Emit an INFO-level structured event."""
    event_log.info(action, extra=_safe_extra(action, fields))


def log_warning(action: str, **fields) -> None:
    """Emit a WARNING-level structured event (e.g. rate-limited, rejected)."""
    event_log.warning(action, extra=_safe_extra(action, fields))
