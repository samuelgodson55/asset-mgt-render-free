"""
api/auth.py
-----------
POST /auth/login, GET /auth/me, PATCH /auth/me, POST /auth/update-password,
POST /auth/forgot-password, POST /auth/reset-password, and the
two-factor-authentication endpoints (POST /auth/mfa/setup/confirm,
POST /auth/mfa/verify) that complete a super_admin login -- see
services/auth_service.py's module-level login()/mfa_setup_confirm()/
mfa_verify()/request_password_reset()/confirm_password_reset()/
update_identity() for the actual flow.
"""

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from deps import get_current_user
from schemas.auth_schema import (
    LoginRequest, PasswordUpdateRequest, MfaSetupConfirmRequest, MfaVerifyRequest, RecoveryCodesRegenerateRequest,
    ForgotPasswordRequest, ResetPasswordRequest, IdentityUpdateRequest,
)
import services.auth_service as auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _resolve_frontend_base_url(request: Request) -> str:
    """
    Builds the browser-facing base URL a mailed "forgot password?" link is
    built from -- e.g. "https://assets.corp.io" so the link becomes
    "https://assets.corp.io/?reset_token=...". Derived straight from THIS
    request rather than a fixed FRONTEND_BASE_URL setting (removed from
    config.py -- see that field's old docstring), so it always tracks the
    site's real, current address with nothing to fall out of sync after a
    domain change/redeploy.

    Preference order, first one present wins:
      1. The `Origin` header -- browsers attach this to every fetch/XHR
         request that isn't a plain top-level navigation (which is exactly
         what js/auth.js's requestPasswordReset() makes), whether or not
         it's cross-origin. It's the address bar the person actually has
         open right now, arrives straight from the browser with no
         reverse-proxy cooperation required, and isn't something a script
         running in that browser can override.
      2. `X-Forwarded-Proto` + `X-Forwarded-Host` -- what this app's own
         reverse proxies (see nginx/default.conf.template's
         proxy_set_header lines, Caddyfile's header_up block) set to the
         scheme/host the EDGE actually received a request on, for the rare
         caller that omits Origin.
      3. This ASGI request's own scheme/host (`request.url`) -- correct
         for local `docker compose up`/bare `uvicorn main:app` with no
         proxy in front at all.

    SECURITY: whichever candidate wins above is checked against
    settings.cors_origin_list -- the exact same trusted-origins list
    CORSMiddleware already enforces for cross-origin API calls (see
    main.py), and which every documented deployment shape (docker-compose,
    render.yaml, infra/main.bicep) sets to this app's real public
    domain(s) -- before it's trusted for anything. Skipping this check
    would let anyone hand-craft an Origin/X-Forwarded-Host header pointing
    at an attacker-controlled domain and get this app to mail a real
    user's password-reset link there instead of its own site -- a classic
    Host-header-injection attack on a "forgot password" flow. A candidate
    that isn't in the trusted list is discarded in favor of the first
    configured CORS origin, and the mismatch is logged so a misconfigured
    CORS_ORIGINS (or a genuine spoofing attempt) is visible in the logs.
    """
    def _normalize(origin: str) -> str:
        return origin.strip().rstrip("/")

    trusted = [_normalize(origin) for origin in settings.cors_origin_list]

    candidate = None
    origin_header = request.headers.get("origin")
    if origin_header:
        candidate = _normalize(origin_header)

    if not candidate:
        forwarded_proto = request.headers.get("x-forwarded-proto")
        forwarded_host = request.headers.get("x-forwarded-host")
        if forwarded_proto and forwarded_host:
            candidate = _normalize(f"{forwarded_proto}://{forwarded_host}")

    if not candidate:
        candidate = _normalize(f"{request.url.scheme}://{request.url.netloc}")

    if trusted and candidate not in trusted:
        logger.warning(
            "auth_api: resolved frontend base URL '%s' is not in CORS_ORIGINS -- "
            "using the first configured origin for this password reset link instead.",
            candidate,
        )
        return trusted[0]

    return candidate


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=60 * 60 * settings.JWT_EXPIRY_HOURS,
        path="/",
    )


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db), response: Response = None):
    result = auth_service.login(db, req)
    # SECURITY: only a result carrying a real session `token` (i.e. a
    # normal, fully-completed login) gets the HttpOnly cookie set here.
    # A super_admin login that still needs 2FA (mfa_required/
    # mfa_setup_required -- see auth_service.py's login()) returns no
    # `token` at all, so no cookie is issued yet -- the browser isn't
    # authenticated until POST /auth/mfa/verify or
    # POST /auth/mfa/setup/confirm below also succeeds.
    if "token" in result:
        _set_session_cookie(response, result["token"])
        result.pop("token", None)
    return result


@router.post("/mfa/setup/confirm")
def mfa_setup_confirm(req: MfaSetupConfirmRequest, db: Session = Depends(get_db), response: Response = None):
    result = auth_service.mfa_setup_confirm(db, req.mfa_setup_token, req.code)
    _set_session_cookie(response, result["token"])
    result.pop("token", None)
    return result


@router.post("/mfa/verify")
def mfa_verify(req: MfaVerifyRequest, db: Session = Depends(get_db), response: Response = None):
    result = auth_service.mfa_verify(db, req.mfa_pending_token, req.code)
    # SECURITY: a correct RECOVERY code doesn't grant a session here --
    # it retires the old TOTP secret and comes back as an
    # `mfa_setup_required` challenge instead (see auth_service.py's
    # mfa_verify() docstring), same "no `token` means no cookie yet"
    # shape login() above already handles for mfa_required/
    # mfa_setup_required. The browser isn't authenticated until
    # POST /auth/mfa/setup/confirm also succeeds against the NEW secret.
    if "token" in result:
        _set_session_cookie(response, result["token"])
        result.pop("token", None)
    return result


@router.post("/logout")
def logout(response: Response):
    response.set_cookie(
        key="access_token",
        value="",
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=0,
        expires=0,
        path="/",
    )
    return {"message": "Logged out successfully."}


@router.get("/me")
def get_my_profile(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Lets the frontend re-hydrate 'who am I' info -- used both on page
    load (navbar name/initials) and by the "My Profile" window, which
    needs fields (like department_role) the JWT itself doesn't carry."""
    return auth_service.get_profile(db, user)


@router.post("/update-password")
def update_password(req: PasswordUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return auth_service.update_password(db, req, user)


@router.post("/mfa/recovery-codes/regenerate")
def regenerate_recovery_codes(req: RecoveryCodesRegenerateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return auth_service.regenerate_recovery_codes(db, user, req.password)


# --- "Forgot password?" self-recovery (no session required) ----------------
# See services/auth_service.py's request_password_reset()/
# confirm_password_reset() docstrings for the full rationale -- mainly
# gives SUPER_ADMIN_ROLE a real recovery path, since nothing else exists
# for it if it forgets its password. No `get_current_user` dependency on
# either route below: by definition, whoever is calling these doesn't
# have (or has lost) a way to log in yet.
@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    return auth_service.request_password_reset(db, req, frontend_base_url=_resolve_frontend_base_url(request))


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    return auth_service.confirm_password_reset(db, req)


# --- Self-service identity rotation (name / username / email) --------------
@router.patch("/me")
def update_my_identity(req: IdentityUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return auth_service.update_identity(db, req, user)
