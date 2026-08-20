"""ASGI gate for planned maintenance.

The middleware deliberately sits at the API boundary. Public configuration,
authentication/recovery endpoints, health checks, and the maintenance-control
API stay reachable so the login page can render the maintenance state and the
Super Admin can recover the application. Every other API request is rejected
with HTTP 503 while maintenance mode is enabled.

Important: the maintenance decision is fail-open if the status lookup itself
fails. A database outage must not turn this middleware into a second outage
layer that prevents health/recovery operations from running.

All synchronous SQLAlchemy work is dispatched to a worker thread. The
DBConcurrencyMiddleware is registered outside this layer in main.py, so the
maintenance lookup is itself covered by the per-process DB admission gate.
"""

import json
import logging

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from deps import resolve_user_from_token
from starlette.types import ASGIApp, Receive, Scope, Send

import services.maintenance_service as maintenance_service
import database

logger = logging.getLogger(__name__)

ALLOWLIST_PREFIXES = (
    "/api/auth/",
    "/api/config/public",
    "/api/maintenance/",
    "/api/telemetry/client-error",
    "/api/telemetry/traces",
    "/api/health",
)


def _extract_token(scope: Scope) -> str | None:
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    authorization = headers.get(b"authorization", b"").decode("latin1")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None

    cookie = headers.get(b"cookie", b"").decode("latin1")
    for part in cookie.split(";"):
        if part.strip().startswith("access_token="):
            return part.strip().split("=", 1)[1] or None
    return None


def _check_super_admin(token: str | None) -> bool:
    if not token:
        return False
    db = database.SessionLocal()
    try:
        user = resolve_user_from_token(token, db)
        return user.get("role") == "super_admin"
    except HTTPException:
        return False
    finally:
        db.close()


class MaintenanceModeMiddleware:
    """Block protected API traffic while allowing recovery/control paths."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Non-API traffic and explicit recovery/public routes bypass the gate.
        # The actual route remains responsible for any DB work it performs,
        # and DBConcurrencyMiddleware is outside this layer for all API paths.
        if not path.startswith("/api/") or any(
            path.startswith(prefix) for prefix in ALLOWLIST_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        status = None
        try:
            # get_cached_status owns its synchronous Session lifecycle; the
            # whole operation runs in a worker thread so no SQLAlchemy Session
            # crosses the ASGI event-loop/thread boundary. This is intentionally
            # still inside DBConcurrencyMiddleware in the effective stack.
            status = await run_in_threadpool(maintenance_service.get_cached_status)
            if not status["enabled"]:
                await self.app(scope, receive, send)
                return
        except Exception:
            logger.exception("maintenance check failed; failing open")
            await self.app(scope, receive, send)
            return

        # Existing Super Admin sessions must remain usable so maintenance can
        # be disabled and the application brought back online. This check is
        # also synchronous DB work and therefore runs off the event loop.
        try:
            if await run_in_threadpool(_check_super_admin, _extract_token(scope)):
                await self.app(scope, receive, send)
                return
        except Exception:
            logger.exception("maintenance super-admin check failed; treating caller as non-admin")

        message = (
            status["message"]
            if status and status.get("message")
            else "The application is currently undergoing maintenance."
        )
        body = json.dumps(
            {
                "detail": message,
                "code": "MAINTENANCE_MODE",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
