"""ASGI gate for planned maintenance.

The middleware deliberately sits at the API boundary.  Public configuration,
authentication/recovery endpoints, health checks, and the maintenance-control
API stay reachable so the login page can render the maintenance state and the
Super Admin can recover the application.  Every other API request is rejected
with HTTP 503 while maintenance mode is enabled.

Important: the maintenance decision is fail-open if the status lookup itself
fails.  A database outage must not turn this middleware into a second outage
layer that prevents health/recovery operations from running.
"""

import json
import logging

from fastapi import HTTPException
from deps import resolve_user_from_token
from starlette.types import ASGIApp, Receive, Scope, Send

import services.maintenance_service as maintenance_service

# Deliberately NOT `from database import SessionLocal` at module level: that
# binds to the sessionmaker object database.py had at import time, which is
# permanent for the life of the process in production but breaks the test
# suite's per-test DB swap (tests/conftest.py's db_engine fixture
# monkeypatches the *attribute* `database.SessionLocal`, which an
# already-bound `from ... import` name doesn't see). Importing the module
# and reading `database.SessionLocal` at call time instead picks up
# whichever engine is current -- same pattern already used by
# services/quotation_service.py's own lazy DB access, for the same reason.
import database

logger = logging.getLogger(__name__)

# These routes must remain reachable during maintenance.  In particular,
# /api/auth/ is intentionally allowed through so the login endpoint can verify
# the caller and return the appropriate maintenance response rather than the
# middleware hiding the authentication flow completely.
#
# Only prefixes that can actually appear here belong in this tuple -- the
# gate below already only runs for paths starting with "/api/" (see
# `__call__`), so a handful of non-"/api/"-prefixed entries ("/docs",
# "/openapi.json", "/health", "/healthz") used to sit in this list without
# ever being reachable. They're handled by the `not path.startswith("/api/")`
# check instead and are left out here to avoid implying this middleware
# consults them.
ALLOWLIST_PREFIXES = (
    "/api/auth/",
    "/api/config/public",
    "/api/maintenance/",
    "/api/telemetry/client-error",
    "/api/telemetry/traces",
    "/api/health",
)


class MaintenanceModeMiddleware:
    """Block protected API traffic while allowing recovery/control paths."""

    def __init__(self, app: ASGIApp):
        self.app = app

    def _super_admin(self, scope: Scope) -> bool:
        """Return True only for an active, database-backed Super Admin.

        Reuses `deps.resolve_user_from_token()` -- the exact same
        decode-JWT-then-recheck-the-database logic `get_current_user()` (and
        therefore every FastAPI route's `require_true_super_admin` gate)
        runs -- rather than a hand-rolled, easy-to-drift copy. That means
        this check now also honors the AUTH_EPOCH post-restore invalidation
        and the `is_deleted` flag, not just `is_active`, matching what the
        real `/api/maintenance/status` PUT route requires of the same token.
        """
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        token = None

        authorization = headers.get(b"authorization", b"").decode("latin1")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()

        # Browser requests normally use the access_token cookie.  Keep the
        # Authorization-header path as well because API clients may use it.
        if not token:
            cookie = headers.get(b"cookie", b"").decode("latin1")
            for part in cookie.split(";"):
                if part.strip().startswith("access_token="):
                    token = part.strip().split("=", 1)[1]
                    break

        if not token:
            return False

        db = database.SessionLocal()
        try:
            user = resolve_user_from_token(token, db)
            return user.get("role") == "super_admin"
        except HTTPException:
            # Expired, malformed, revoked, or pre-restore-epoch token: not a
            # valid Super Admin session. Maintenance protection must never
            # crash the entire API because a stale/malformed token was
            # supplied by a client, so this is deliberately swallowed here.
            return False
        finally:
            db.close()

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ):
        """Apply the maintenance gate to HTTP API requests."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Non-API traffic and explicit recovery/public routes bypass the gate.
        if not path.startswith("/api/") or any(
            path.startswith(prefix) for prefix in ALLOWLIST_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        db = None
        status = None
        try:
            db = database.SessionLocal()
            status = maintenance_service.get_status(db)
            if not status["enabled"]:
                await self.app(scope, receive, send)
                return
        except Exception:
            logger.exception("maintenance check failed; failing open")
            await self.app(scope, receive, send)
            return
        finally:
            if db is not None:
                db.close()

        # Existing Super Admin sessions must remain usable so maintenance can
        # be disabled and the application brought back online.
        if self._super_admin(scope):
            await self.app(scope, receive, send)
            return

        # Surface the admin-configured message (set via PUT
        # /api/maintenance/status) instead of a hardcoded generic string, so
        # locked-out users actually see what the Super Admin typed in.
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
