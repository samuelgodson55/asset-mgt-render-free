"""Fire-and-forget FastAPI integration for ErrorBeacon.

The integration must never make application availability or request latency
depend on ErrorBeacon being reachable. Events are queued in memory and sent by
background worker threads.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import socket
import threading
import traceback
from contextvars import ContextVar
from typing import Any

import requests
from starlette.types import Scope

try:
    from logging_config import request_id_var
except Exception:  # pragma: no cover - logging_config is optional
    request_id_var = None


log = logging.getLogger("errorbeacon.client")

URL = os.getenv("ERRORBEACON_URL", "http://errorbeacon:8000")
KEY = os.getenv("ERRORBEACON_INGEST_API_KEY", "") or os.getenv("ERRORBEACON_API_KEY", "")
APP = os.getenv("ERRORBEACON_APP", "asset-inventory-quotes")
ENV = os.getenv("ENVIRONMENT", "production")
RELEASE = os.getenv("APP_RELEASE", "")
TIMEOUT = float(os.getenv("ERRORBEACON_TIMEOUT", "0.75"))
QSIZE = max(20, int(os.getenv("ERRORBEACON_QUEUE_SIZE", "200")))
WORKERS = max(1, int(os.getenv("ERRORBEACON_WORKERS", "2")))

_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QSIZE)
_started = False
_lock = threading.Lock()
component_var: ContextVar[str | None] = ContextVar(
    "errorbeacon_component",
    default=None,
)

SECRET = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|"
    r"session|reset[_-]?token|access[_-]?token|refresh[_-]?token|private[_-]?key)"
)
JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{10,}\b"
)
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
DBURL = re.compile(
    r"(?i)(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^\s]+"
)
ASSIGN = re.compile(
    r"(?i)(authorization|cookie|x-api-key|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|reset[_-]?token|password|passwd|secret|"
    r"session(?:[_-]?id)?)\s*[:=]\s*([^\s,;&]+)"
)


def clean(value: Any, key: str = "") -> Any:
    """Recursively remove credentials and other sensitive values."""

    if SECRET.search(key):
        return "[REDACTED]"

    if isinstance(value, dict):
        return {str(k): clean(v, str(k)) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [clean(item, key) for item in list(value)[:100]]

    if isinstance(value, str):
        sanitized = JWT.sub("[REDACTED_JWT]", value)
        sanitized = BEARER.sub("Bearer [REDACTED]", sanitized)
        sanitized = DBURL.sub("[REDACTED_DB_URL]", sanitized)
        sanitized = ASSIGN.sub(
            lambda match: f"{match.group(1)}=[REDACTED]",
            sanitized,
        )
        return sanitized[:12000]

    return value


def set_component(value: str | None) -> None:
    component_var.set(value)


def rid(scope: Scope | None, explicit: str | None = None) -> str | None:
    """Resolve an explicit, application-context, or incoming request ID."""

    if explicit:
        return explicit

    if request_id_var:
        try:
            current = request_id_var.get()
            if current:
                return current
        except Exception:
            pass

    for key, value in (scope or {}).get("headers", []):
        if key.lower() == b"x-request-id":
            return value.decode("latin1")

    return None


def send(payload: dict[str, Any]) -> None:
    """Send one event without ever propagating network errors."""

    try:
        headers = {"X-API-Key": KEY}
        request_id = payload.get("request_id")
        if request_id:
            headers["X-Request-ID"] = str(request_id)[:200]

        requests.post(
            f"{URL.rstrip('/')}/v1/events",
            json=payload,
            headers=headers,
            timeout=(0.1, TIMEOUT),
        )
    except Exception as exc:
        log.debug("ErrorBeacon reporting failed: %s", type(exc).__name__)


def worker() -> None:
    while True:
        payload = _q.get()
        try:
            send(payload)
        finally:
            _q.task_done()


def start() -> None:
    global _started

    if _started:
        return

    with _lock:
        if _started:
            return

        for number in range(WORKERS):
            threading.Thread(
                target=worker,
                name=f"errorbeacon-client-{number + 1}",
                daemon=True,
            ).start()

        _started = True


def enqueue(payload: dict[str, Any]) -> None:
    start()

    try:
        _q.put_nowait(payload)
    except queue.Full:
        log.warning("ErrorBeacon client queue full; dropping monitoring event")


def report_exception(
    exc: BaseException,
    scope: Scope | None = None,
    status_code: int = 500,
    context: dict[str, Any] | None = None,
    *,
    component: str | None = None,
    operation: str | None = None,
    category: str | None = None,
    severity: str = "error",
    user_id: str | None = None,
) -> None:
    if not KEY:
        return

    try:
        traceback_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )

        payload = {
            "app": APP,
            "environment": ENV,
            "severity": severity,
            "error_type": type(exc).__name__,
            "message": str(clean(str(exc)))[:5000],
            "traceback": str(clean(traceback_text))[:30000],
            "request_id": rid(scope),
            "method": scope.get("method") if scope else None,
            "path": scope.get("path") if scope else None,
            "status_code": status_code,
            "user_id": clean(user_id) if user_id else None,
            "release": RELEASE or None,
            "host": socket.gethostname(),
            "component": component or component_var.get(),
            "operation": operation,
            "category": category,
            "context": clean(context or {}),
        }
        enqueue(payload)
    except Exception as exc:
        log.debug(
            "ErrorBeacon payload construction failed: %s",
            type(exc).__name__,
        )


def report_background_exception(
    exc: BaseException,
    *,
    component: str,
    operation: str | None = None,
    context: dict[str, Any] | None = None,
    severity: str = "error",
) -> None:
    """Report Celery/startup/service exceptions that have no HTTP scope."""

    report_exception(
        exc,
        None,
        500,
        context=context,
        component=component,
        operation=operation,
        severity=severity,
    )


def report_client_event(
    message: str,
    *,
    stack: str | None = None,
    path: str | None = None,
    request_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Forward a sanitized browser-side error through the backend reporter."""

    if not KEY:
        return

    payload = {
        "app": APP,
        "environment": ENV,
        "severity": "error",
        "error_type": "ClientError",
        "message": str(clean(message))[:5000],
        "traceback": str(clean(stack or ""))[:30000],
        "request_id": request_id,
        "method": "CLIENT",
        "path": path,
        "status_code": None,
        "release": RELEASE or None,
        "host": socket.gethostname(),
        "component": "frontend",
        "category": (
            "chaos_test"
            if isinstance(context, dict) and context.get('test')
            else "client_error"
        ),
        "context": clean(context or {}),
    }
    enqueue(payload)
