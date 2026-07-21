"""
deps.py
-------
Shared FastAPI dependencies used across every router in `api/`: decoding and
validating the bearer JWT (`get_current_user`), and the two role gates
(`require_super_admin`, `require_privileged_role`) built on top of it.

Kept as one small standalone module (rather than living inside main.py or
any single router) specifically so every `api/*.py` file can import from
here without creating a dependency on main.py itself.
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import jwt

import models
from database import get_db
from security import decode_access_token, SUPER_ADMIN_ROLE

security = HTTPBearer(auto_error=False)

# Roles that carry full Super-Admin-equivalent privileges. `admin` is a
# normal, DB-backed, deletable account (see database.py's seed_db() and
# services/user_service.py's create_user()); `super_admin` is the single
# hardcoded-IDENTITY root account (see security.py's module docstring) --
# it now IS a real `users` row too, just one that's bootstrapped by
# migration instead of provisioned through the app, and hidden from
# directory/audit listings. Both are treated identically by every
# permission check below -- the only difference between them is *how the
# account exists*, never what it's allowed to do.
_FULL_ADMIN_ROLES = ("super_admin", "admin")


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> dict:
    """
    Decodes and validates the bearer JWT from either the Authorization header
    or an HttpOnly session cookie. Returns a small dict describing who's
    logged in: {sub, name, email, role, department}. Any route that depends
    on this simply requires "you must be logged in".
    """
    token = None
    if credentials is not None:
        token = credentials.credentials
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Invalid authentication token.")

    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid authentication token.")

    # Requirement #4 (Auth/User routes must exclude soft-deleted records):
    # the JWT itself is stateless and stays cryptographically valid until it
    # naturally expires (up to JWT_EXPIRY_HOURS later). Without re-checking
    # the database here, an Admin soft-deleting or deactivating a user
    # would NOT actually revoke that user's access -- their existing token
    # would keep working on every protected route until it happened to
    # expire. Re-querying on every request makes revocation immediate.
    db_user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()
    if not db_user or db_user.is_deleted or not db_user.is_active:
        raise HTTPException(status_code=401, detail="This account is no longer active. Please log in again.")

    return payload


def require_super_admin(user: dict = Depends(get_current_user)) -> dict:
    """Gate for actions only the Super Admin or an Admin may perform."""
    if user["role"] not in _FULL_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Forbidden: Operation requires Super Admin privileges.")
    return user


def require_privileged_role(user: dict = Depends(get_current_user)) -> dict:
    """Gate for actions the Super Admin, an Admin, OR a Manager may perform."""
    if user["role"] not in (*_FULL_ADMIN_ROLES, "manager"):
        raise HTTPException(status_code=403, detail="Forbidden: View permission requires elevated administrative rights.")
    return user


def require_true_super_admin(user: dict = Depends(get_current_user)) -> dict:
    """
    Gate for the handful of actions reserved for the root Super Admin
    account ONLY -- a regular `admin` account, despite being treated as
    fully equivalent to Super Admin everywhere else in this app (see
    `_FULL_ADMIN_ROLES` above), is deliberately excluded here.

    Currently used for every `/backup/*` route (view/create/download/
    delete/restore -- see api/backup_api.py): a backup contains literally
    everything, including every `admin` account's own row, and restoring
    one wholesale replaces the entire database with it. Letting an `admin`
    view, download, or (especially) restore backups would let that same
    action expose or tamper with the very accounts meant to be holding it
    accountable. Super Admin is the single hardcoded-IDENTITY root account
    (see security.py's module docstring), so this stays gated to it alone.
    """
    if user["role"] != SUPER_ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Forbidden: This action is restricted to the Super Admin account.")
    return user
