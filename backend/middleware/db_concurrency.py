"""
DB-side concurrency guard.

This is deliberately a small, per-process gate in front of the API.  It does
not replace SQLAlchemy's pool timeout: it prevents a burst of API requests from
all reaching the database at once, while the pool remains the final safety
net.

The default limit is derived from this process's SQLAlchemy pool capacity, so
it follows the existing adaptive pool sizing rather than introducing a second
hard-coded connection budget.  Set DB_REQUEST_CONCURRENCY_LIMIT=0 to disable
it, or set a positive value to override the derived limit.
"""

import asyncio
import json
import logging
from starlette.types import ASGIApp, Receive, Scope, Send

import database
import overload_monitor
from config import settings
from logging_config import request_id_var

logger = logging.getLogger(__name__)

_EXCLUDED_PREFIXES = ("/healthz", "/api/health")


class DBConcurrencyMiddleware:
    def __init__(self, app: ASGIApp, limit: int | None = None) -> None:
        self.app = app
        configured = settings.DB_REQUEST_CONCURRENCY_LIMIT if limit is None else limit
        if configured == 0:
            self.limit = 0
            self._semaphore = None
            return
        if configured is None:
            configured = database.POOL_SIZE + database.MAX_OVERFLOW
        if configured < 0:
            raise ValueError("DB_REQUEST_CONCURRENCY_LIMIT must be 0 or greater")
        self.limit = max(int(configured), 1)
        self._semaphore = asyncio.Semaphore(self.limit)
        logger.info("database: API concurrency guard enabled at %d in-flight requests per process", self.limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self._semaphore is None
            or scope.get("type") != "http"
            or not scope.get("path", "").startswith("/api/")
            or any(scope.get("path", "").startswith(prefix) for prefix in _EXCLUDED_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return

        acquired = False
        try:
            # Allow a short, bounded queue so normal browser bursts are
            # serialized instead of rejected. The queue is still bounded by
            # this timeout, so only a genuinely sustained overload can recover
            # with a controlled 503 rather than an indefinitely growing
            # backlog. The product goal is successful requests first, while
            # retaining a last-resort overload brake.
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=8.0)
            except asyncio.TimeoutError:
                # Structured cause for logs/metrics/ErrorBeacon; the body the
                # CALLER sees stays generic (see overload_monitor.user_message)
                # -- see overload_monitor.py's module docstring for the full
                # split between "what the client is told" and "what
                # operators can actually see".
                reason = overload_monitor.OverloadReason.ADMISSION_QUEUE_FULL
                path = scope.get("path", "")
                request_id = request_id_var.get()
                overload_monitor.record_503(route=path, reason=reason, scope=scope, request_id=request_id)

                body = json.dumps({
                    "detail": overload_monitor.user_message(reason),
                    "reason": reason,
                    "request_id": request_id,
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"1"),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return

            acquired = True
            await self.app(scope, receive, send)
        finally:
            if acquired:
                self._semaphore.release()
