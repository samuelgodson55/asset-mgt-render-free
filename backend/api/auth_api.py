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

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from deps import get_current_user
from schemas.auth_schema import (
    LoginRequest, PasswordUpdateRequest, MfaSetupConfirmRequest, MfaVerifyRequest, RecoveryCodesRegenerateRequest,
    ForgotPasswordRequest, ResetPasswordRequest, IdentityUpdateRequest,
)
import services.auth_service as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


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
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    return auth_service.request_password_reset(db, req)


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    return auth_service.confirm_password_reset(db, req)


# --- Self-service identity rotation (name / username / email) --------------
@router.patch("/me")
def update_my_identity(req: IdentityUpdateRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return auth_service.update_identity(db, req, user)
