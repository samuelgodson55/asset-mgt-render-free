"""
logging_config.py
------------------
Sets up STRUCTURED logging for the whole backend (Operations & Observability
requirement #2), and makes sure every log line automatically carries the
current request's "correlation ID" -- without any individual `logger.info()`
call anywhere in the codebase having to remember to pass it in.

WHY STRUCTURED LOGGING?
-----------------------
The default `print()`/basic logging output is a free-form sentence like:
    "User logged in: r.adeyemi@corp.io"
That's fine to eyeball locally, but a real log aggregator (CloudWatch,
Datadog, ELK/OpenSearch, Grafana Loki, etc.) can't easily filter/alert on
free text. STRUCTURED logging means each line is instead one JSON object
with consistent field names, e.g.:
    {"timestamp": "...", "level": "INFO", "logger": "services.auth_service",
     "message": "Login succeeded", "request_id": "b3f1...", "user": "..."}
Now you can ask your log system "show me every ERROR for request_id=X" or
"alert me if failed logins for the same IP exceed 20/minute" trivially.

HOW THE REQUEST ID GETS INTO EVERY LOG LINE
--------------------------------------------
`middleware/request_context.py` generates a UUID4 "request ID" for every
incoming HTTP request (or reuses one supplied by an upstream proxy via the
`X-Request-ID` header) and stores it in a `contextvars.ContextVar`.
`contextvars` (stdlib) is what makes this safe under `async def` routes:
each concurrent request gets its own isolated value, unlike a plain global
variable which every coroutine would share and stomp on.

`RequestIdLogFilter` below reads that same ContextVar and stamps it onto
EVERY `logging.LogRecord` that flows through Python's logging system, no
matter which module logged it or how deep in the call stack it happened.
That's why `services/auth_service.py`, `services/checkout_service.py`, etc.
can just do `logger.info("Login succeeded", extra={"user": ...})` and the
request_id shows up automatically -- they never import or touch the
ContextVar themselves.

USAGE (see main.py)
--------------------
    from logging_config import configure_logging
    configure_logging(settings)  # call this ONCE, before the app starts

Then anywhere else in the codebase:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Something happened", extra={"custom_field": "value"})
"""

import json
import logging
import sys
from contextvars import ContextVar
from typing import Optional

# ---------------------------------------------------------------------------
# The shared ContextVar. `middleware/request_context.py` sets this at the
# start of every request and resets it when the request finishes;
# `RequestIdLogFilter` (below) only ever reads it.
# ---------------------------------------------------------------------------
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# A small, fixed set of attributes every standard LogRecord already has --
# used by JsonFormatter to figure out which attributes on a record are
# "extra" (custom) fields a caller passed in via `logger.info(..., extra=...)`
# so those can be included in the JSON output too.
_STANDARD_LOG_RECORD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class RequestIdLogFilter(logging.Filter):
    """
    A `logging.Filter` isn't just for deciding whether to keep/drop a log
    record (though that's its original purpose) -- it's also the
    standard, documented way to MUTATE a record before it's formatted.
    Here we use it purely to attach the current request's correlation ID.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True  # never actually filters anything out


class JsonFormatter(logging.Formatter):
    """Renders each LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        # Fold in any caller-supplied `extra={...}` fields (e.g. "user",
        # "asset_id") so they show up as their own JSON keys instead of
        # being mashed into the message string.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-friendly single-line format, handy for local development."""

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = getattr(record, "request_id", None) or "-"
        base = super().format(record)
        return base


def configure_logging(settings) -> None:
    """
    Wires up the root logger ONCE at process startup (called from main.py).
    Every `logging.getLogger(__name__)` used anywhere else in the app
    inherits this configuration automatically -- that's how Python's
    logging module works (child loggers propagate up to the root logger's
    handlers unless told otherwise).
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.LOG_LEVEL.upper())

    # Remove any handlers a previous call (or uvicorn's own default config)
    # already attached, so we never end up with duplicated log lines.
    root_logger.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(RequestIdLogFilter())

    if settings.LOG_FORMAT.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter(
            fmt="%(asctime)s %(levelname)-8s [req=%(request_id)s] %(name)s: %(message)s"
        ))

    root_logger.addHandler(handler)

    # Quiet down third-party access logs a little so they use OUR
    # formatter/handler too instead of uvicorn's default plain-text one --
    # this keeps every log line (ours and uvicorn's) consistently
    # structured in production.
    for noisy_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        noisy_logger = logging.getLogger(noisy_logger_name)
        noisy_logger.handlers.clear()
        noisy_logger.propagate = True
