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
from config import settings

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
            # Keep the wait intentionally tiny: requests should be shed
            # rather than form a second unbounded queue in front of the DB.
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=0.01)
            except asyncio.TimeoutError:
                body = json.dumps({
                    "detail": "The service is busy processing database-backed requests. Please retry shortly."
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"2"),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return

            acquired = True
            await self.app(scope, receive, send)
        finally:
            if acquired:
                self._semaphore.release()
