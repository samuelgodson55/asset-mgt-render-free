"""ASGI gate for planned maintenance. Never blocks the recovery/auth paths."""
import json, logging
from starlette.types import ASGIApp, Scope, Receive, Send, Message
import jwt
from database import SessionLocal
import models
from security import decode_access_token
import services.maintenance_service as maintenance_service

logger = logging.getLogger(__name__)
ALLOWLIST_PREFIXES = (
    "/api/auth/", "/api/config/public", "/api/maintenance/",
    "/api/telemetry/client-error", "/api/health", "/health", "/healthz", "/docs", "/openapi.json",
)

class MaintenanceModeMiddleware:
    def __init__(self, app: ASGIApp): self.app = app

    def _super_admin(self, scope: Scope) -> bool:
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        token = None
        auth = headers.get(b"authorization", b"").decode("latin1")
        if auth.lower().startswith("bearer " ): token = auth[7:].strip()
        if not token:
            cookie = headers.get(b"cookie", b"").decode("latin1")
            for part in cookie.split(";"):
                if part.strip().startswith("access_token="):
                    token = part.strip().split("=",1)[1]; break
        if not token: return False
        try:
            payload = decode_access_token(token)
            if payload.get("role") != "super_admin": return False
            email = payload.get("email") or payload.get("sub")
            db = SessionLocal()
            try:
                user = db.query(models.User).filter(models.User.email == email).first()
                return bool(user and user.is_active and user.role == "super_admin")
            finally: db.close()
        except (jwt.InvalidTokenError, Exception):
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send); return
        path = scope.get("path", "")
        if not path.startswith("/api/") or any(path.startswith(p) for p in ALLOWLIST_PREFIXES):
            await self.app(scope, receive, send); return
        db = None
        try:
            db = SessionLocal()
            if not maintenance_service.get_status(db)["enabled"]:
                await self.app(scope, receive, send); return
        except Exception:
            logger.exception("maintenance check failed; failing open")
            await self.app(scope, receive, send); return
        finally:
            if db is not None: db.close()
        if self._super_admin(scope):
            await self.app(scope, receive, send); return
        body = json.dumps({"detail": "The application is currently undergoing maintenance.", "code": "MAINTENANCE_MODE"}).encode()
        await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
