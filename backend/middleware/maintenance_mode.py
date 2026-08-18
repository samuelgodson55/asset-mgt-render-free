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

import jwt
from database import SessionLocal
from security import decode_access_token
from starlette.types import ASGIApp, Receive, Scope, Send

import models
import services.maintenance_service as maintenance_service

logger = logging.getLogger(__name__)

# These routes must remain reachable during maintenance.  In particular,
# /api/auth/ is intentionally allowed through so the login endpoint can verify
# the caller and return the appropriate maintenance response rather than the
# middleware hiding the authentication flow completely.
ALLOWLIST_PREFIXES = (
    "/api/auth/",
    "/api/config/public",
    "/api/maintenance/",
    "/api/telemetry/client-error",
    "/api/health",
    "/health",
    "/healthz",
    "/docs",
    "/openapi.json",
)


class MaintenanceModeMiddleware:
    """Block protected API traffic while allowing recovery/control paths."""

    def __init__(self, app: ASGIApp):
        self.app = app

    def _super_admin(self, scope: Scope) -> bool:
        """Return True only for an active, database-backed Super Admin.

        The JWT role is treated as a hint, not as the final authorization
        decision.  Looking up the user in the database prevents an old token
        from retaining maintenance access after the account is disabled or
        its role is changed.
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

        try:
            payload = decode_access_token(token)
            if payload.get("role") != "super_admin":
                return False

            email = payload.get("email") or payload.get("sub")
            if not email:
                return False

            db = SessionLocal()
            try:
                user = (
                    db.query(models.User)
                    .filter(models.User.email == email)
                    .first()
                )
                return bool(
                    user
                    and user.is_active
                    and user.role == "super_admin"
                )
            finally:
                db.close()
        except (jwt.InvalidTokenError, Exception):
            # Maintenance protection must never crash the entire API because
            # a stale/malformed token was supplied by a client.
            return False

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
        try:
            db = SessionLocal()
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

        body = json.dumps(
            {
                "detail": "The application is currently undergoing maintenance.",
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
