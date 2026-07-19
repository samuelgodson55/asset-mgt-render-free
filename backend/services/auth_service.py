"""
services/auth_service.py
-------------------------
Login and password-update business logic, used by api/auth.py.
"""

import datetime
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

import models
from models import utc_now
from config import settings
from security import hash_password, verify_password, create_access_token
from schemas.auth import LoginRequest, PasswordUpdateRequest

logger = logging.getLogger(__name__)

# SECURITY: timing-attack mitigation. `verify_password()` (Argon2id) takes a
# deliberately non-trivial, roughly-constant amount of time to run. If we
# only ever called it when a matching account was found, an attacker could
# distinguish "no such account" (fast response) from "account exists, wrong
# password" (slower response) purely by measuring response time -- a classic
# username-enumeration side channel. This precomputed hash is verified
# against on the "no such account" path too (see login() below) so both
# paths do the same amount of work no matter what.
_DUMMY_PASSWORD_HASH = hash_password("this-is-not-a-real-account-timing-safety-only")


def _build_login_result(user_payload: dict, token: str, needs_password_reset: bool) -> dict:
    expires_at = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=settings.JWT_EXPIRY_HOURS)).timestamp())
    return {
        "message": "Authentication successful.",
        "user_id": user_payload["user_id"],
        "name": user_payload["name"],
        "username": user_payload["username"],
        "role": user_payload["role"],
        "department": user_payload["department"],
        "token": token,
        "expires_at": expires_at,
        "needs_password_reset": needs_password_reset,
    }


def login(db: Session, req: LoginRequest) -> dict:
    """
    Verify credentials and, on success, issue a signed JWT session token.

    Data Quality & Usability requirement #6: `req.identifier` is matched
    against EITHER `email` OR `username` (whichever one the person actually
    typed) via a single `OR` clause -- we don't ask them to specify which
    kind of value it is. Matching is CASE-INSENSITIVE on both `email` and
    `username` (so "T.Okafor@corp.io" and "t.okafor@corp.io" are treated
    the same at login) via `func.lower()` on both sides of the comparison.
    This deliberately doesn't need a DB migration or a case-insensitive
    index -- `func.lower(column)` works against the existing schema as-is;
    it just can't use a plain b-tree index on `email`/`username` for the
    lookup, which is a non-issue at this app's scale. Account-creation/
    -update paths (see user_service.py's create_user()/update_user()) now
    also check case-insensitively for clashes, so two accounts differing
    only by case can no longer be created going forward, keeping this
    lookup unambiguous.
    """
    # Rate limiting for repeated failed attempts is handled one layer up,
    # in ASGI middleware -- see backend/middleware/rate_limit.py, wired
    # onto this exact route in main.py (Operations & Observability
    # requirement #3). That's IP-based and coarse ("slow down whoever's
    # hammering this endpoint"); the per-account `failed_login_attempts` /
    # `locked_until` check further down is the finer-grained,
    # account-specific complement to it -- it stops an attacker who spreads
    # guesses across many IPs from ever brute-forcing one specific account.
    identifier = req.identifier.strip()

    # SECURITY CHANGE: there used to be a hardcoded Super Admin login path
    # checked HERE, before the `users` table was ever touched -- it
    # compared `identifier`/`password` directly against the
    # SUPER_ADMIN_USERNAME/SUPER_ADMIN_PASSWORD environment variables and
    # never queried the database at all. That's gone: the root admin
    # account (role=SUPER_ADMIN_ROLE) is now a real `users` row,
    # bootstrapped once by `alembic/versions/0002_bootstrap_root_admin.py`
    # (see that file, and security.py's module docstring, for the full
    # rationale). It authenticates through the exact same DB-backed
    # lookup/lockout/password-verification logic below as every other
    # account -- no special-casing needed here anymore.
    identifier_lower = identifier.lower()
    user = db.query(models.User).filter(
        or_(func.lower(models.User.email) == identifier_lower, func.lower(models.User.username) == identifier_lower),
        models.User.is_active,
        ~models.User.is_deleted,
    ).first()

    if not user:
        # No matching account -- still run a full password hash comparison
        # against the dummy hash above so this branch takes about as long
        # as the "wrong password" branch below (see _DUMMY_PASSWORD_HASH).
        verify_password(req.password, _DUMMY_PASSWORD_HASH)
        logger.warning("Login failed: no matching account", extra={"identifier": identifier})
        raise HTTPException(status_code=401, detail="Invalid email/username or password.")

    # SECURITY: per-account lockout check, BEFORE touching the password at
    # all -- once locked, further guesses shouldn't even cost a hash
    # comparison. `locked_until` is cleared automatically on the next
    # successful login, or early by a Super Admin resetting the account's
    # password (see update_password() below).
    now = utc_now()
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        # Defensive normalization: `DateTime(timezone=True)` always round-trips
        # as timezone-AWARE UTC under this project's supported production
        # backend (Postgres), but some other backends (e.g. SQLite, sometimes
        # used for quick local testing) silently drop the offset on the way
        # back out. Treat a naive value as UTC rather than letting the
        # comparison below raise -- see models.py's utc_now() docstring for
        # why UTC is always the intended timezone everywhere in this project.
        locked_until = locked_until.replace(tzinfo=datetime.timezone.utc)
    if locked_until and locked_until > now:
        remaining_seconds = int((locked_until - now).total_seconds())
        remaining_minutes = max(1, (remaining_seconds + 59) // 60)  # round UP to the next whole minute
        logger.warning(
            "Login blocked: account temporarily locked",
            extra={"user_id": user.id, "email": user.email, "remaining_minutes": remaining_minutes},
        )
        raise HTTPException(
            status_code=423,  # 423 Locked
            detail=f"Account temporarily locked due to repeated failed login attempts. Try again in {remaining_minutes} minute(s).",
        )

    if not verify_password(req.password, user.password_hash):
        # SECURITY: never log the submitted password (correct or not) --
        # only that an attempt failed and for which identifier, so ops can
        # spot credential-stuffing patterns in the logs without the log
        # file itself becoming a list of attempted passwords.
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.ACCOUNT_LOCKOUT_MAX_ATTEMPTS:
            user.locked_until = now + datetime.timedelta(minutes=settings.ACCOUNT_LOCKOUT_DURATION_MINUTES)
            logger.warning(
                "Account locked after repeated failed login attempts",
                extra={"user_id": user.id, "email": user.email, "attempts": user.failed_login_attempts},
            )
        db.commit()
        logger.warning("Login failed", extra={"identifier": identifier})
        raise HTTPException(status_code=401, detail="Invalid email/username or password.")

    # Successful login -- clear any accumulated lockout state.
    if user.failed_login_attempts or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()

    token = create_access_token(user)
    logger.info("Login succeeded", extra={"user": user.email, "role": user.role, "user_id": user.id})
    return _build_login_result(
        {
            "user_id": user.id,
            "name": user.name,
            "username": user.username,
            "role": user.role,
            "department": user.department,
        },
        token,
        not user.is_verified,
    )


def get_profile(db: Session, current_user: dict) -> dict:
    """
    Powers `GET /auth/me` for the new "My Profile" window. Deliberately
    re-queries the database for the CURRENT row instead of just returning
    the JWT's own decoded payload (which is what this endpoint used to do)
    -- the token is a point-in-time snapshot taken at login and doesn't
    reflect anything changed since (e.g. `department_role`, which isn't
    even stored in the JWT at all -- see security.py's create_access_token
    -- or a `department` a Super Admin edited after this session started).
    """
    user = db.query(models.User).filter(models.User.id == int(current_user["sub"])).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "username": user.username,
        "role": user.role,
        "department": user.department,
        "department_role": user.department_role,
    }


def update_password(db: Session, req: PasswordUpdateRequest, current_user: dict) -> dict:
    # SECURITY CHANGE: the root admin's password used to live only in the
    # SUPER_ADMIN_PASSWORD environment variable and could never be changed
    # from inside the app. It's now a real `password_hash` column like any
    # other account (see security.py's module docstring), so it goes
    # through this exact same self-service flow -- current-password
    # re-confirmation, complexity validation, and an audited update --
    # rather than being permanently un-rotatable from here.

    # A user may only reset their own password unless they are an Admin or
    # the Super Admin.
    is_self_service = str(req.user_id) == current_user["sub"]
    if not is_self_service and current_user["role"] not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="You may only update your own password.")

    target = db.query(models.User).filter(
        models.User.id == req.user_id, ~models.User.is_deleted
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # SECURITY: when someone is changing their OWN password (the common
    # "My Profile -> Change Password" case), require them to re-confirm
    # their CURRENT password first -- otherwise anyone who got hold of a
    # still-valid JWT (e.g. an unattended logged-in browser tab, or a
    # token leaked some other way) could silently change the password and
    # lock the real account owner out, without ever having to know the
    # existing password. This check is intentionally SKIPPED when a Super
    # Admin resets a DIFFERENT user's password (`is_self_service` is
    # False) -- that's precisely the escape hatch needed to recover a
    # genuinely locked-out account, and the Super Admin can't be expected
    # to know a stranger's current password.
    if is_self_service:
        if not req.current_password or not verify_password(req.current_password, target.password_hash):
            logger.warning("Password change rejected: current password mismatch", extra={"user_id": target.id})
            raise HTTPException(status_code=400, detail="Current password is incorrect.")

    # Password complexity/length is already enforced up front by
    # schemas.auth.PasswordUpdateRequest's field_validator -- by the time
    # execution reaches here, req.new_password is guaranteed to meet policy.
    target.password_hash = hash_password(req.new_password)
    target.is_verified = True
    # SECURITY: a successful password change/reset is also a legitimate way
    # to recover a locked-out account early -- whether the person finally
    # remembered their own current password (self-service path) or a Super
    # Admin reset it for them (recovery path) -- so clear any accumulated
    # lockout state here too, same as a successful login does.
    target.failed_login_attempts = 0
    target.locked_until = None
    db.commit()
    logger.info("Password updated", extra={"target_user_id": target.id, "changed_by": current_user["email"]})
    return {"message": "Password updated successfully."}
