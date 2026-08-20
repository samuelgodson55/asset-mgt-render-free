"""
services/auth_service.py
-------------------------
Login and password-update business logic, used by api/auth.py.
"""

import datetime
import logging

import jwt
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from cryptography.fernet import InvalidToken

import models
from models import utc_now
from config import settings
from security import (
    hash_password, verify_password, create_access_token,
    SUPER_ADMIN_ROLE, generate_totp_secret, encrypt_totp_secret, decrypt_totp_secret,
    totp_provisioning_uri, verify_totp_code, create_mfa_token, decode_mfa_token,
    MFA_SETUP_TOKEN_PURPOSE, MFA_PENDING_TOKEN_PURPOSE,
    generate_recovery_codes, is_recovery_code_format, normalize_recovery_code,
    generate_password_reset_token,
)
from schemas.auth_schema import (
    LoginRequest, PasswordUpdateRequest, ForgotPasswordRequest, ResetPasswordRequest,
    IdentityUpdateRequest,
)
import services.notification_service as notification_service

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

    # SECURITY: two-factor authentication, currently REQUIRED for the
    # single SUPER_ADMIN_ROLE account -- it's the one identity in the
    # system that can't be deleted/demoted and that every other
    # permission check treats as fully trusted, so it's the account an
    # attacker who obtained its password would get the most value from,
    # and the one worth the extra login friction for every other role
    # doesn't (yet) carry. Password verification above already succeeded
    # by this point -- what happens next is which SECOND factor response
    # we hand back instead of a real session cookie.
    if user.role == SUPER_ADMIN_ROLE:
        if not user.totp_enabled:
            # Not enrolled yet (first login ever, or a previous enrollment
            # attempt was started but never confirmed) -- (re)generate a
            # fresh secret every time this branch runs rather than reusing
            # a possibly-stale unconfirmed one, and hand back everything
            # needed to enroll. This secret is shown to the caller exactly
            # once, here -- it's never retrievable again after this
            # response (see mfa_setup_confirm() below for how enrollment
            # is completed, and GET /auth/me / the User model for why
            # there's no "view my current TOTP secret" endpoint at all).
            secret = generate_totp_secret()
            user.totp_secret_encrypted = encrypt_totp_secret(secret)
            db.commit()
            setup_token = create_mfa_token(user, MFA_SETUP_TOKEN_PURPOSE)
            logger.info("2FA enrollment started", extra={"user_id": user.id, "email": user.email})
            return {
                "role": user.role,
                "mfa_setup_required": True,
                "message": "Two-factor authentication setup is required for this account.",
                "mfa_setup_token": setup_token,
                "totp_secret": secret,
                "otpauth_uri": totp_provisioning_uri(secret, user.email),
            }
        # Already enrolled -- hand back a short-lived MFA-pending token
        # instead of a real session; POST /auth/mfa/verify (mfa_verify()
        # below) exchanges a correct code for the actual session cookie.
        pending_token = create_mfa_token(user, MFA_PENDING_TOKEN_PURPOSE)
        logger.info("Password verified, awaiting 2FA code", extra={"user_id": user.id, "email": user.email})
        return {
            "role": user.role,
            "mfa_required": True,
            "message": "Enter your two-factor authentication code to continue.",
            "mfa_pending_token": pending_token,
        }

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


def _issue_recovery_codes(db: Session, user: models.User) -> list:
    """
    Replaces this user's ENTIRE set of recovery codes with a fresh batch --
    see models.py's RecoveryCode docstring. Deletes every existing row
    (used AND unused) rather than just topping up, so there's never a mix
    of "codes from batch N still valid" and "codes from batch N+1 also
    valid" to reason about -- each call to this function is a hard reset.
    Returns the PLAINTEXT codes (only ever available here, at the moment
    of generation -- see RecoveryCode.code_hash's docstring for why
    there's no "view my codes again" anywhere else in the app).
    """
    db.query(models.RecoveryCode).filter(models.RecoveryCode.user_id == user.id).delete()
    plaintext_codes = generate_recovery_codes()
    for code in plaintext_codes:
        db.add(models.RecoveryCode(user_id=user.id, code_hash=hash_password(code)))
    db.commit()
    return plaintext_codes


def _load_mfa_target_user(db: Session, token: str, expected_purpose: str) -> models.User:
    """Shared decode+lookup for both MFA endpoints below. Raises the same
    401s `get_current_user` (deps.py) raises for a bad/expired/tampered
    JWT, since this token is exactly that -- just a narrower-purpose one."""
    try:
        payload = decode_mfa_token(token, expected_purpose)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Login session expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid login session. Please log in again.")

    user = db.query(models.User).filter(
        models.User.id == int(payload["sub"]), models.User.is_active, ~models.User.is_deleted,
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid login session. Please log in again.")
    return user


def mfa_setup_confirm(db: Session, mfa_setup_token: str, code: str) -> dict:
    """
    Completes 2FA enrollment: verifies the FIRST live code generated
    against the secret handed back by login()'s mfa_setup_required
    response, and only THEN flips totp_enabled=True and issues the real
    session cookie -- see models.py's User.totp_enabled docstring for why
    a generated-but-never-confirmed secret intentionally doesn't count as
    "enrolled" (protects against someone locking themselves out by saving
    a secret they mistyped into their authenticator app and never actually
    verifying it works).
    """
    user = _load_mfa_target_user(db, mfa_setup_token, MFA_SETUP_TOKEN_PURPOSE)
    if not user.totp_secret_encrypted:
        raise HTTPException(status_code=401, detail="Invalid or expired setup session. Please log in again.")

    try:
        secret = decrypt_totp_secret(user.totp_secret_encrypted)
    except InvalidToken:
        # JWT_SECRET_KEY was rotated since this secret was encrypted (see
        # security.py's _totp_encryption_key() docstring) -- there's no
        # recovering it, so force a clean re-enrollment on the next login
        # rather than surfacing an unhandled 500.
        logger.error("TOTP secret undecryptable during setup confirm -- forcing re-enrollment", extra={"user_id": user.id})
        user.totp_secret_encrypted = None
        db.commit()
        raise HTTPException(status_code=401, detail="Your two-factor setup session is no longer valid. Please log in again to restart setup.")

    if not verify_totp_code(secret, code):
        logger.warning("2FA enrollment confirmation failed: incorrect code", extra={"user_id": user.id})
        raise HTTPException(status_code=400, detail="Incorrect code. Please try again.")

    user.totp_enabled = True
    db.commit()
    recovery_codes = _issue_recovery_codes(db, user)
    token = create_access_token(user)
    logger.info("2FA enrollment completed", extra={"user_id": user.id, "email": user.email})
    result = _build_login_result(
        {"user_id": user.id, "name": user.name, "username": user.username, "role": user.role, "department": user.department},
        token,
        not user.is_verified,
    )
    # Shown here, in this response, EXACTLY ONCE -- see
    # RecoveryCode.code_hash's docstring for why there's no endpoint
    # anywhere that can hand these back out again after this.
    result["recovery_codes"] = recovery_codes
    return result


def mfa_verify(db: Session, mfa_pending_token: str, code: str) -> dict:
    """
    Completes login for an already-enrolled account: verifies EITHER a
    live 6-digit TOTP code OR one of the account's unused recovery codes
    (see models.py's RecoveryCode docstring -- format-detected via
    security.py's is_recovery_code_format(), so the caller doesn't need
    to say up front which kind it's submitting).

    A live TOTP code, on success, issues the real session cookie exactly
    like a normal password-only login() would.

    A recovery code is different: recovery codes exist for exactly one
    scenario -- the device holding the TOTP secret (phone lost, wiped,
    traded in, app uninstalled, whatever) is no longer available, so the
    account's *only* other route back in is used. Simply logging the
    person in at that point, still trusting the same TOTP secret, would
    leave them right back where they started: enrolled against a secret
    that lives on a device they no longer have, with no way to ever see
    that secret again (see mfa_setup_confirm()'s docstring -- it's
    intentionally never re-shown) and one fewer recovery code before
    they're locked out for good. Instead, a correct recovery code here
    ends the account's trust in the old secret immediately (totp_enabled
    reset to False, the encrypted secret cleared) and hands back the
    exact same `mfa_setup_required` shape login() returns for a
    brand-new account -- a fresh secret to enroll on whatever device is
    at hand right now. The real session (and a fresh batch of recovery
    codes, replacing every remaining old one) is only granted once
    mfa_setup_confirm() below verifies a live code from that new
    enrollment, same as any other first-time setup.

    SECURITY: wrong-code attempts of EITHER kind increment/consult the
    SAME per-account `failed_login_attempts`/`locked_until` columns a
    wrong PASSWORD does (see login() above) -- brute-forcing either a
    6-digit TOTP code or a recovery code is exactly the kind of
    repeated-guessing attack that lockout already exists to slow down,
    so it's reused rather than building parallel counters per code type.
    """

    user = _load_mfa_target_user(db, mfa_pending_token, MFA_PENDING_TOKEN_PURPOSE)
    if not user.totp_enabled or not user.totp_secret_encrypted:
        raise HTTPException(status_code=401, detail="Invalid login session. Please log in again.")

    now = utc_now()
    locked_until = user.locked_until
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=datetime.timezone.utc)
    if locked_until and locked_until > now:
        remaining_seconds = int((locked_until - now).total_seconds())
        remaining_minutes = max(1, (remaining_seconds + 59) // 60)
        logger.warning("2FA verification blocked: account temporarily locked", extra={"user_id": user.id, "remaining_minutes": remaining_minutes})
        raise HTTPException(
            status_code=423,
            detail=f"Account temporarily locked due to repeated failed attempts. Try again in {remaining_minutes} minute(s).",
        )

    used_recovery_code = False
    if is_recovery_code_format(code):
        normalized = normalize_recovery_code(code)
        matched_row = None
        for row in db.query(models.RecoveryCode).filter(
            models.RecoveryCode.user_id == user.id, models.RecoveryCode.used_at.is_(None),
        ).all():
            if verify_password(normalized, row.code_hash):
                matched_row = row
                break
        code_is_valid = matched_row is not None
        if code_is_valid:
            matched_row.used_at = now
            used_recovery_code = True
    else:
        try:
            secret = decrypt_totp_secret(user.totp_secret_encrypted)
        except InvalidToken:
            logger.error("TOTP secret undecryptable during verify -- forcing re-enrollment", extra={"user_id": user.id})
            user.totp_secret_encrypted = None
            user.totp_enabled = False
            db.commit()
            raise HTTPException(status_code=401, detail="Your two-factor setup is no longer valid. Please log in again to re-enroll.")
        code_is_valid = verify_totp_code(secret, code)

    if not code_is_valid:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.ACCOUNT_LOCKOUT_MAX_ATTEMPTS:
            user.locked_until = now + datetime.timedelta(minutes=settings.ACCOUNT_LOCKOUT_DURATION_MINUTES)
            logger.warning("Account locked after repeated failed 2FA attempts", extra={"user_id": user.id, "attempts": user.failed_login_attempts})
        db.commit()
        logger.warning("2FA verification failed: incorrect code", extra={"user_id": user.id})
        raise HTTPException(status_code=401, detail="Incorrect code.")

    if user.failed_login_attempts or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None

    if used_recovery_code:
        # The device that held the old TOTP secret is, by definition,
        # unavailable right now -- that's the only reason a recovery code
        # was needed at all. Stop trusting that secret immediately and
        # fall back into the exact same "not enrolled yet" shape login()
        # uses for a first-ever super_admin login, so this device can
        # enroll a fresh one. Nothing here grants a session: that still
        # only happens once mfa_setup_confirm() verifies a live code
        # against the NEW secret below (which also reissues a full,
        # fresh batch of recovery codes -- see _issue_recovery_codes()
        # -- invalidating whatever was left of the old batch too, since
        # a lost device is reason enough to treat the old codes as
        # potentially compromised right along with the old secret).
        user.totp_enabled = False
        user.totp_secret_encrypted = None
        secret = generate_totp_secret()
        user.totp_secret_encrypted = encrypt_totp_secret(secret)
        db.commit()
        setup_token = create_mfa_token(user, MFA_SETUP_TOKEN_PURPOSE)
        logger.info(
            "2FA re-enrollment started via recovery code (original device unavailable)",
            extra={"user_id": user.id, "email": user.email},
        )
        return {
            "mfa_setup_required": True,
            "recovery_code_used": True,
            "message": (
                "Recovery code accepted. Your previous authenticator is no longer trusted -- "
                "set up two-factor authentication on this device to finish signing in."
            ),
            "mfa_setup_token": setup_token,
            "totp_secret": secret,
            "otpauth_uri": totp_provisioning_uri(secret, user.email),
        }

    db.commit()
    token = create_access_token(user)
    logger.info("2FA verification succeeded, login complete", extra={"user_id": user.id, "email": user.email})
    return _build_login_result(
        {"user_id": user.id, "name": user.name, "username": user.username, "role": user.role, "department": user.department},
        token,
        not user.is_verified,
    )


def regenerate_recovery_codes(db: Session, current_user: dict, password: str) -> dict:
    """
    Lets an already-logged-in, already-2FA-enrolled account holder
    invalidate every existing recovery code and get a fresh batch --
    covers "I used most of them", "I think mine leaked", or just routine
    hygiene. Requires re-entering the CURRENT password first (same
    re-confirmation pattern as update_password()) since this is a
    security-sensitive action taken from an already-authenticated
    session, not a login itself -- there's no TOTP/recovery-code check
    here on purpose, since the person is already inside an authenticated
    session by definition.
    """
    user = db.query(models.User).filter(models.User.id == int(current_user["sub"])).first()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")
    if user.role != SUPER_ADMIN_ROLE:
        # Mirrors the same enforcement point as login()'s SECURITY note --
        # 2FA (and therefore recovery codes) only exists for this role
        # today, so there's nothing to regenerate for anyone else.
        raise HTTPException(status_code=403, detail="Two-factor authentication is not enabled for this account.")
    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="Two-factor authentication is not set up on this account yet.")
    if not verify_password(password, user.password_hash):
        logger.warning("Recovery code regeneration blocked: incorrect password", extra={"user_id": user.id})
        raise HTTPException(status_code=401, detail="Incorrect password.")

    codes = _issue_recovery_codes(db, user)
    logger.info("2FA recovery codes regenerated", extra={"user_id": user.id, "email": user.email})
    return {"message": "Recovery codes regenerated. Save these somewhere safe -- they won't be shown again.", "recovery_codes": codes}


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
        "phone_number": user.phone_number,
        "company": user.company,
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
    # SECURITY: revoke every session already issued for this account --
    # see models.py's User.credentials_changed_at docstring and deps.py's
    # resolve_user_from_token() for how this is enforced. Without this, a
    # still-valid stolen cookie/token would keep working right through
    # this password change until it happened to naturally expire.
    target.credentials_changed_at = utc_now()
    db.commit()
    logger.info("Password updated", extra={"target_user_id": target.id, "changed_by": current_user["email"]})
    return {"message": "Password updated successfully."}


# ---------------------------------------------------------------------------
# "FORGOT PASSWORD?" SELF-RECOVERY (email-based, no session required)
# ---------------------------------------------------------------------------
# Fills the one gap update_password() above can't: it requires either the
# CURRENT password (self-service) or a Super Admin/Admin acting on someone
# ELSE's account (services/user_service.py -> reset_user_password()) --
# neither works when SUPER_ADMIN_ROLE itself forgets its password, since
# there's no admin "above" it and it obviously doesn't have its own current
# password anymore. See models.PasswordResetToken's docstring for the
# storage rationale; not restricted to that role specifically -- any
# account can use this, same self-service reasoning as update_password().

# Generic response for POST /auth/forgot-password, returned identically
# whether or not `identifier` matched a real account -- SECURITY: the
# alternative ("no account with that email/username") would let anyone
# enumerate which emails/usernames exist in the system just by trying
# them here, the exact same anti-enumeration reasoning login() already
# documents for "Invalid email/username or password.".
_FORGOT_PASSWORD_GENERIC_MESSAGE = (
    "If an account matches that email or username, a password reset link has been sent to its registered email address."
)


def request_password_reset(db: Session, req: ForgotPasswordRequest, frontend_base_url: str) -> dict:
    """
    POST /auth/forgot-password. Looks `req.identifier` up the exact same
    case-insensitive, email-OR-username way login() does, and -- ONLY if a
    match is found -- issues a fresh, single-use reset token and emails it
    to that account's REGISTERED email address (never anywhere the caller
    typed, which matters here specifically: an attacker who knows a valid
    username but not its email can't redirect the link anywhere they
    control).

    `frontend_base_url` is resolved by the caller (api/auth_api.py's
    _resolve_frontend_base_url()) from the actual incoming request rather
    than read from a settings.FRONTEND_BASE_URL env var -- see that
    function's docstring for the full "why" and the CORS_ORIGINS-based
    safety check that keeps it from being spoofed. Keeping that
    HTTP-request-shaped logic out of this module preserves the same
    route-does-HTTP-things/service-does-business-logic split every other
    function here already follows.

    Always returns the same generic message either way (see
    _FORGOT_PASSWORD_GENERIC_MESSAGE above) -- callers must never be able
    to distinguish "sent" from "no such account" from the response alone.
    """
    identifier = req.identifier.strip().lower()
    user = db.query(models.User).filter(
        or_(func.lower(models.User.email) == identifier, func.lower(models.User.username) == identifier),
        models.User.is_active,
        ~models.User.is_deleted,
    ).first()

    if not user:
        logger.info("Password reset requested for unknown identifier", extra={"identifier": req.identifier.strip()})
        return {"message": _FORGOT_PASSWORD_GENERIC_MESSAGE}

    # A fresh request supersedes any still-pending one -- same "hard
    # reset, never a mix of old-batch/new-batch validity" reasoning as
    # _issue_recovery_codes() above. Also means at most one row per user
    # ever needs checking when a reset link is later redeemed.
    db.query(models.PasswordResetToken).filter(models.PasswordResetToken.user_id == user.id).delete()

    plaintext_token = generate_password_reset_token()
    expires_at = utc_now() + datetime.timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES)
    db.add(models.PasswordResetToken(
        user_id=user.id, token_hash=hash_password(plaintext_token), expires_at=expires_at,
    ))
    db.commit()

    reset_link = f"{frontend_base_url.rstrip('/')}/?reset_token={plaintext_token}"
    notification_service.enqueue_email_after_commit(
        db=db,
        to=user.email,
        subject=f"Reset your {settings.SITE_NAME} password",
        body=(
            f"Hi {user.name},\n\n"
            "A password reset was requested for your account. If this was you, "
            f"click the link below within {settings.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES} minutes to choose a new password:\n\n"
            f"{reset_link}\n\n"
            "If you didn't request this, you can safely ignore this email -- your password will not be changed.\n"
        ),
    )
    # Delivery is now queued after the commit; a broker/email outage cannot
    # hold this request's PostgreSQL connection open.
    sent = None
    # send_email() is fail-soft by design (see notification_service.py's
    # module docstring) -- a misconfigured/unreachable SMTP server here
    # must not turn into a 500, and must not reveal anything different to
    # the caller than the "no such account" path above either. The token
    # row above still exists and is still redeemable if delivery genuinely
    # failed but the operator later fixes SMTP and resends manually via
    # the logged plaintext (DEBUG level only, see send_email()) in a local
    # development environment where NOTIFICATIONS_ENABLED is explicitly set to false.
    logger.info("Password reset requested", extra={"user_id": user.id, "email_sent": sent})
    return {"message": _FORGOT_PASSWORD_GENERIC_MESSAGE}


def confirm_password_reset(db: Session, req: ResetPasswordRequest) -> dict:
    """
    POST /auth/reset-password. Unlike update_password(), there's no
    logged-in session and no known user_id up front -- only the plaintext
    `req.token` from the emailed link. Since PasswordResetToken.token_hash
    is a one-way Argon2id hash (same as a password -- never reversible),
    the matching row can't be looked up by an indexed equality query; it's
    found the same way a password itself is checked, by verifying the
    candidate against each still-live hash. This table only ever holds
    one row per user with a pending request (see request_password_reset()
    above, which clears out any previous one first), so in practice this
    is checking at most a small handful of rows system-wide at any moment.
    """
    now = utc_now()
    candidates = db.query(models.PasswordResetToken).filter(
        models.PasswordResetToken.used_at.is_(None),
        models.PasswordResetToken.expires_at > now,
    ).all()

    matched = next((row for row in candidates if verify_password(req.token, row.token_hash)), None)
    if not matched:
        logger.warning("Password reset failed: invalid or expired token")
        raise HTTPException(status_code=400, detail="This password reset link is invalid or has expired. Request a new one.")

    user = db.query(models.User).filter(models.User.id == matched.user_id, ~models.User.is_deleted).first()
    if not user:
        raise HTTPException(status_code=400, detail="This password reset link is invalid or has expired. Request a new one.")

    # Password complexity/length is already enforced up front by
    # schemas.auth.ResetPasswordRequest's field_validator.
    user.password_hash = hash_password(req.new_password)
    user.is_verified = True
    # SECURITY: a successful reset is a legitimate way to recover a
    # locked-out account -- same reasoning as update_password() above.
    user.failed_login_attempts = 0
    user.locked_until = None
    # SECURITY: same session-revocation reasoning as update_password()
    # above -- a "forgot password" reset must invalidate whatever session
    # was live before it, e.g. one belonging to whoever locked the real
    # owner out in the first place.
    user.credentials_changed_at = now
    matched.used_at = now
    db.commit()
    logger.info("Password reset completed", extra={"user_id": user.id})
    return {"message": "Password updated successfully. You can now sign in with your new password."}


# ---------------------------------------------------------------------------
# SELF-SERVICE IDENTITY ROTATION (name / username / email)
# ---------------------------------------------------------------------------
def update_identity(db: Session, req: IdentityUpdateRequest, current_user: dict) -> dict:
    """
    PATCH /auth/me. Lets the CURRENTLY LOGGED-IN account rotate its own
    name/username/email/phone_number/company -- the same self-service shape update_password()
    already established for the password itself. Available to EVERY role
    (Super Admin, Admin, Manager, Staff, Customer alike), not just
    SUPER_ADMIN_ROLE: that role is simply the one account with no admin
    "above" it who could otherwise fix these values via
    services/user_service.py's update_user() (see is_hidden_root_admin()'s
    guard, which blocks PATCH /users/{id} from ever reaching that row), so
    it's the one account that would have NO path to correct these values
    at all without this endpoint -- every other role already has this same
    self-service path available on top of an Admin/Super Admin being able
    to fix it for them too. Deliberately SELF-ONLY either way: this never
    accepts a target user_id and always acts on `current_user`'s own row,
    so it can't be used as a side-door around is_hidden_root_admin() to
    edit anyone ELSE's account.

    Every present field requires `req.current_password` to have already
    been re-verified below FIRST -- same reasoning as update_password()'s
    self-service branch: a leaked/still-valid session cookie alone must
    never be enough to quietly change what a person logs in as.

    DUPLICATE-USERNAME/EMAIL PROTECTION: both fields are checked
    case-insensitively against every OTHER account before being applied
    (see the two `clash` queries below) -- same rationale as
    user_service.py's create_user()/update_user(): login already matches
    email/username case-insensitively, so two accounts differing only by
    case must never both exist. A clash raises a 400 the frontend surfaces
    inline on the form (js/components/profile.js's
    submitUpdateIdentityForm()) instead of silently overwriting anything.

    NOTIFICATION: once a change is actually committed, a summary email is
    sent to the account's PRE-CHANGE registered address (see
    _notify_identity_change() below) -- so if an attacker with a stolen,
    still-valid session tries to quietly change the login details on an
    account they don't otherwise control, the real owner still finds out,
    even though the current-password re-check above already makes that a
    fairly narrow attack window. If email itself was one of the changed
    fields, the NEW address is also sent its own copy, so a "did I just
    correctly update my own email" mistake is confirmed at the new inbox
    too.
    """
    target = db.query(models.User).filter(models.User.id == int(current_user["sub"]), ~models.User.is_deleted).first()
    if not target:
        raise HTTPException(status_code=404, detail="Account not found.")

    if not verify_password(req.current_password, target.password_hash):
        logger.warning("Identity update rejected: current password mismatch", extra={"user_id": target.id})
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    updates = req.model_dump(exclude_unset=True, exclude={"current_password"})

    # Snapshot of what this account looked like BEFORE any field below is
    # touched -- needed both to build a readable "X changed from A to B"
    # notification afterward, and (for email specifically) to know where
    # the pre-change notification copy should be sent, since target.email
    # itself may be overwritten by the time notify runs.
    previous_email = target.email
    changes: list[str] = []

    if "email" in updates and updates["email"].strip().lower() != target.email.strip().lower():
        # Case-insensitive clash check -- same rationale as
        # user_service.py's create_user()/update_user(): login matches
        # email/username case-insensitively, so two accounts differing
        # only by case must never both exist.
        new_email = updates["email"].strip()
        clash = db.query(models.User).filter(
            func.lower(models.User.email) == new_email.lower(), models.User.id != target.id,
        ).first()
        if clash:
            raise HTTPException(status_code=400, detail="A user with this email already exists.")
        changes.append(f"Email changed from {target.email} to {new_email}.")
        target.email = new_email

    if "username" in updates and updates["username"] != target.username:
        candidate = updates["username"].strip().lower()
        # Same reserved-username guard user_service.py's
        # _derive_username()/update_user() apply -- keeps the
        # CONFIGURED default (settings.SUPER_ADMIN_USERNAME) permanently
        # blocked as a matter of policy (nobody should be able to register
        # the well-known default root login name to impersonate it), fully
        # independent of whatever this account's username is rotated to.
        # The plain uniqueness check just below is what protects THIS
        # account's actual current/new username from being claimed by
        # anyone else -- a real `users` row with a unique constraint on
        # `username`, exactly like every other account.
        reserved = settings.SUPER_ADMIN_USERNAME.strip().lower()
        if candidate == reserved and target.role != SUPER_ADMIN_ROLE:
            raise HTTPException(status_code=400, detail="That username is reserved.")
        clash = db.query(models.User).filter(models.User.username == candidate, models.User.id != target.id).first()
        if clash:
            raise HTTPException(status_code=400, detail="That username is already taken.")
        changes.append(f"Username changed from {target.username} to {candidate}.")
        target.username = candidate

    if "name" in updates and updates["name"].strip() != target.name:
        changes.append(f"Name changed from {target.name} to {updates['name'].strip()}.")
        target.name = updates["name"].strip()

    # phone_number/company are plain contact details, not authentication
    # material -- unlike name/username/email above, changing either can't
    # let anyone impersonate this account or hijack its login, so neither
    # is added to `changes`/triggers the security notification email
    # below. An explicit empty string clears the field (same "blank is a
    # legitimate value, not a no-op" handling user_service.py's
    # update_user() already gives phone_number).
    if "phone_number" in updates:
        target.phone_number = updates["phone_number"] or None

    if "company" in updates:
        target.company = updates["company"] or None

    # NOTE: `operator` uses `target.email` (the row's own, just-committed
    # value) rather than `current_user["email"]` (the JWT's snapshot at
    # login time) -- this action is always self-service (target IS the
    # caller), and if email was one of the fields just rotated, the JWT's
    # snapshot is already stale by definition. Using the fresh value keeps
    # audit_service.py's live super-admin-email lookup matching this row
    # immediately, instead of it briefly (and only for this one row)
    # appearing to belong to a different operator than the account
    # actually is right now.
    db.add(models.AuditLog(
        operator=target.email, action="IDENTITY_UPDATED", target_type="User", target_id=target.id,
        details="Updated own account details.",
    ))
    db.commit()
    logger.info("Identity updated", extra={"user_id": target.id})

    if changes:
        _notify_identity_change(target, previous_email, changes, db=db)

    return {
        "message": "Profile updated successfully.",
        "name": target.name, "username": target.username, "email": target.email,
        "phone_number": target.phone_number, "company": target.company,
    }


def _notify_identity_change(user: "models.User", previous_email: str, changes: list[str], db: Session | None = None) -> None:
    """
    Fires after update_identity() commits a real change to name/username/
    email -- a plain security notification, same fail-soft pattern as
    every other email this app sends (see notification_service.py's
    module docstring: NOTIFICATIONS_ENABLED off, or SMTP misconfigured,
    just logs and returns False; never raises, never turns into a 500 on
    an update that already succeeded).

    Always sent to `previous_email` -- the account's registered address
    BEFORE this update -- so the real owner is told even in the narrow
    case a still-valid stolen session was used to change these fields out
    from under them. If email itself changed, `user.email` (the NEW
    address, already committed by the caller) also gets its own copy, so
    the person confirms the new inbox is correct and reachable.
    """
    body = (
        f"Hi {user.name},\n\n"
        f"Your {settings.SITE_NAME} account details were just updated:\n\n"
        + "\n".join(f"- {line}" for line in changes)
        + "\n\nIf this was you, no action is needed. If you didn't make this change, "
        "reset your password immediately using the \"Forgot password?\" link on the "
        "login page and contact your administrator.\n"
    )
    recipients = {previous_email, user.email}
    if db is not None:
        notification_service.enqueue_email_after_commit(
            db=db, to=list(recipients),
            subject=f"Your {settings.SITE_NAME} account details were updated", body=body,
        )
        sent = None
    else:
        sent = notification_service.send_email(
            to=list(recipients), subject=f"Your {settings.SITE_NAME} account details were updated", body=body,
        )
    logger.info("Identity change notification queued", extra={"user_id": user.id, "email_sent": sent})
