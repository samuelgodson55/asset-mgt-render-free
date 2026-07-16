"""
api/auth.py
-----------
POST /auth/login, GET /auth/me, POST /auth/update-password.
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from deps import get_current_user
from schemas.auth import LoginRequest, PasswordUpdateRequest
import services.auth_service as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db), response: Response = None):
    result = auth_service.login(db, req)
    response.set_cookie(
        key="access_token",
        value=result["token"],
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        max_age=60 * 60 * settings.JWT_EXPIRY_HOURS,
        path="/",
    )
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
