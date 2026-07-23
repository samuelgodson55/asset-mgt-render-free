"""
security.py
------------
Small shared helper module for anything auth-related: password hashing,
password complexity validation, and JSON Web Token (JWT) creation/verification.

Why a separate file? main.py needs these functions, and database.py's demo
data seeder ALSO needs hash_password() to create demo accounts. If we put
this code inside main.py, database.py would have to `import main`, and
main.py already `import`s database.py -> that's a circular import crash.
Keeping shared logic in its own tiny module avoids that problem entirely.
"""

import re
import base64
import hashlib
import secrets
import datetime
import jwt  # PyJWT package
import pyotp
from cryptography.fernet import Fernet
from pwdlib import PasswordHash
from config import settings

# ---------------------------------------------------------------------------
# JWT CONFIGURATION
# ---------------------------------------------------------------------------
# In a real production deployment this secret MUST be a long random string
# supplied via an environment variable. It now comes exclusively from
# `settings` (backend/config.py), which itself reads it from the
# git-ignored `.env` file / the container's environment -- never hardcoded.
JWT_SECRET_KEY = settings.JWT_SECRET_KEY
JWT_ALGORITHM = settings.JWT_ALGORITHM
JWT_EXPIRY_HOURS = settings.JWT_EXPIRY_HOURS  # how long a login session stays valid


# ---------------------------------------------------------------------------
# PASSWORD HASHING (Argon2id via pwdlib)
# ---------------------------------------------------------------------------
password_hash = PasswordHash.recommended()


def hash_password(plain_password: str) -> str:
    """Turn a plaintext password into a secure Argon2id hash for storage."""
    return password_hash.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a plaintext password attempt against a stored hash."""
    return password_hash.verify(plain_password, hashed_password)


# ---------------------------------------------------------------------------
# ROOT ADMINISTRATOR IDENTITY (root account)
# ---------------------------------------------------------------------------
# SECURITY CHANGE: the root account used to be a single fixed identity built
# entirely from config.py's SUPER_ADMIN_USERNAME/SUPER_ADMIN_PASSWORD
# settings and authenticated by comparing the login form directly against
# an environment variable, BEFORE the database was ever touched (see the
# old `super_admin_principal()`/`SUPER_ADMIN_PASSWORD_HASH` this replaced).
# That meant its password lived only in process environment/`.env` --
# outside the database entirely, so it couldn't be rotated through the
# app's normal password-change/reset flows and no `AuditLog` row could ever
# reference a real account for it.
#
# The root account is now a REAL `models.User` row -- exactly one, with
# role=SUPER_ADMIN_ROLE, bootstrapped once by
# `alembic/versions/0002_bootstrap_root_admin.py` during
# `alembic upgrade head` in production (see that file for the full
# rationale). What's still hardcoded/fixed is only the IDENTITY:
#   - There is always exactly one row with this role (RESERVED_ROLES in
#     services/user_service.py blocks `create_user()` from ever minting a
#     second one; the bootstrap migration itself checks for an existing
#     row before inserting).
#   - Its username/name come from config.py's SUPER_ADMIN_USERNAME/
#     SUPER_ADMIN_NAME (or the bootstrap migration's equivalent
#     environment variables), not from anything a caller can choose.
#   - It can never be deleted, or edited via PATCH /users/{id} (see
#     services/user_service.py's delete_user()/update_user() guards), and
#     it's filtered out of the User Directory and Audit Trail everywhere
#     they're listed/exported (see the `is_hidden_root_admin()` helper
#     used throughout services/user_service.py and services/audit_service.py).
#
# What's NOT hardcoded anymore is the password. It's a normal Argon2id
# hash in `password_hash`, exactly like every other account -- so it logs
# in through the exact same `services/auth_service.py -> login()` DB
# lookup as anyone else, and it can be rotated through the exact same
# self-service change-password / Admin-issued reset flows (each producing
# a normal, queryable `AuditLog` row) as any other account.
SUPER_ADMIN_ROLE = "super_admin"

# The root row's `email` column, same synthetic-mailbox convention the old
# super_admin_principal() used ("no real mailbox, just something readable
# for audit-log operator= fields"). Used by services/audit_service.py to
# recognize (and hide) this account's own audit-ledger entries in the UI.
SUPER_ADMIN_EMAIL = f"{settings.SUPER_ADMIN_USERNAME}@local"


# ---------------------------------------------------------------------------
# PASSWORD COMPLEXITY / LENGTH VALIDATION
# ---------------------------------------------------------------------------
# Data Quality & Usability requirement #3: every place a NEW password is set
# (account provisioning in schemas/users.py, password reset in
# schemas/auth.py) must reject weak passwords BEFORE they're ever hashed and
# stored. This lives here (not inline in the schema files) so both schemas
# share the exact same rule set instead of two copies drifting apart.
#
# Login itself (schemas/auth.py's LoginRequest) intentionally does NOT run
# this check -- a login attempt must always be allowed to fail with the
# generic "Invalid email/username or password" message regardless of what
# the submitted password looks like, so this validator is only ever wired
# to *setting* a password, never to *submitting* one to log in.
PASSWORD_MIN_LENGTH = 8


def validate_password_strength(password: str) -> str:
    """
    Enforces a basic password complexity/length policy. Raises `ValueError`
    on failure -- when this function is used as a Pydantic
    `@field_validator`, Pydantic automatically turns that `ValueError` into
    a clean HTTP 422 response listing exactly which rule failed, so the
    frontend can show the person a specific, actionable message instead of
    a generic "bad request".

    Policy (deliberately simple/beginner-readable rather than exhaustive):
      - at least PASSWORD_MIN_LENGTH characters long
      - at least one uppercase letter
      - at least one lowercase letter
      - at least one digit
      - at least one special (non-alphanumeric) character
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long.")
    if not re.search(r"[A-Z]", password):
        raise ValueError("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        raise ValueError("Password must contain at least one lowercase letter.")
    if not re.search(r"[0-9]", password):
        raise ValueError("Password must contain at least one digit.")
    if not re.search(r"[^A-Za-z0-9]", password):
        raise ValueError("Password must contain at least one special character (e.g. ! @ # $ % &).")
    return password


# ---------------------------------------------------------------------------
# JWT ISSUE / VERIFY
# ---------------------------------------------------------------------------
def create_access_token(user) -> str:
    """
    Build a signed JWT that encodes everything the frontend/backend need to
    know about who is logged in, without having to hit the database again on
    every request. `user` is a models.User SQLAlchemy instance.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user.id),
        "name": user.name,
        "email": user.email,
        "username": user.username,
        "role": user.role,
        "department": user.department,
        # PyJWT accepts timezone-aware datetimes for "exp"/"iat" and encodes
        # them as Unix timestamps (seconds since epoch) either way, so using
        # an aware `now` here doesn't change the token's wire format at all
        # -- it just keeps every datetime touched by this codebase
        # consistently timezone-aware end to end (see models.py's module
        # docstring for the full rationale).
        "exp": now + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Verify a JWT's signature + expiry and return its payload.
    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure,
    which main.py turns into clean 401 HTTP responses.
    """
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])


# ---------------------------------------------------------------------------
# TWO-FACTOR AUTHENTICATION (TOTP) -- currently required for SUPER_ADMIN_ROLE
# ---------------------------------------------------------------------------
# See models.py's User.totp_secret_encrypted/totp_enabled docstring for the
# full rationale on WHAT is stored and WHY it's encrypted. This section is
# HOW: secret generation/encryption, the RFC 6238 code check (via `pyotp`),
# and two short-lived, single-purpose JWTs that stand in for a real session
# cookie during the brief window between "password verified" and "TOTP code
# verified" -- see services/auth_service.py's login()/mfa_setup_confirm()/
# mfa_verify() for how these are actually used in the login flow.
MFA_SETUP_TOKEN_PURPOSE = "mfa_setup"
MFA_PENDING_TOKEN_PURPOSE = "mfa_pending"
# Deliberately much shorter than JWT_EXPIRY_HOURS -- this token is only
# ever meant to bridge "enter password" and "enter your 6-digit code" in
# the same sitting, not to be a long-lived credential of its own.
_MFA_TOKEN_EXPIRY_MINUTES = 5


def _totp_encryption_key() -> bytes:
    """
    Derive a stable Fernet key from JWT_SECRET_KEY (SHA-256 -> 32 raw bytes
    -> urlsafe-base64, exactly what Fernet requires) rather than needing a
    brand new secret -- and its own separate env var / rotation story --
    just for encrypting TOTP secrets at rest. This is the same trust
    boundary JWT_SECRET_KEY already sits on: anyone who can read it can
    already forge a valid session for any account, so deriving this from
    it doesn't introduce a new single point of failure. The one real
    consequence: rotating JWT_SECRET_KEY also makes every already-enrolled
    TOTP secret undecryptable -- see auth_service.py's login() for how
    that's handled (falls back to re-enrollment rather than a 500).
    """
    digest = hashlib.sha256(JWT_SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


_totp_fernet = Fernet(_totp_encryption_key())


def generate_totp_secret() -> str:
    """A fresh random RFC 6238 base32 secret, one per enrollment attempt."""
    return pyotp.random_base32()


def encrypt_totp_secret(secret: str) -> str:
    return _totp_fernet.encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_totp_secret(encrypted_secret: str) -> str:
    """Raises cryptography.fernet.InvalidToken if it can't be decrypted --
    e.g. JWT_SECRET_KEY was rotated since this secret was encrypted. Callers
    (auth_service.py) catch that and treat it as "needs re-enrollment"
    rather than letting it surface as an unhandled 500."""
    return _totp_fernet.decrypt(encrypted_secret.encode("utf-8")).decode("utf-8")


def totp_provisioning_uri(secret: str, account_name: str) -> str:
    """The standard `otpauth://` URI authenticator apps scan/import --
    encodes the secret, issuer, and account label together so the app
    doesn't need a QR code specifically (most authenticator apps also
    accept typing the raw base32 secret in manually)."""
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name="Snipe-IT Lite")


def verify_totp_code(secret: str, code: str) -> bool:
    """`valid_window=1` accepts the previous/current/next 30s time step
    (i.e. up to ~90s of clock drift between server and phone) -- standard
    practice for TOTP verification, since phones and servers are never
    perfectly clock-synced."""
    code = (code or "").strip().replace(" ", "")
    if not code or not code.isdigit():
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def create_mfa_token(user, purpose: str) -> str:
    """A short-lived JWT identifying `user` and carrying a `purpose` claim
    (MFA_SETUP_TOKEN_PURPOSE or MFA_PENDING_TOKEN_PURPOSE) -- returned to
    the frontend in the login response body (never as a cookie, since it
    grants no access on its own beyond "you may attempt to complete MFA as
    this specific user") in place of the real session cookie until the
    TOTP step also succeeds."""
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user.id),
        "purpose": purpose,
        "exp": now + datetime.timedelta(minutes=_MFA_TOKEN_EXPIRY_MINUTES),
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_mfa_token(token: str, expected_purpose: str) -> dict:
    """Raises jwt.ExpiredSignatureError/jwt.InvalidTokenError (same as
    decode_access_token) on a bad/expired/wrong-purpose token."""
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    if payload.get("purpose") != expected_purpose:
        raise jwt.InvalidTokenError("Unexpected MFA token purpose.")
    return payload


# ---------------------------------------------------------------------------
# 2FA RECOVERY (BACKUP) CODES
# ---------------------------------------------------------------------------
# See models.py's RecoveryCode docstring for the full storage/lifecycle
# rationale. This section is just generation + format-detection; hashing a
# generated code reuses hash_password()/verify_password() from above --
# a recovery code is treated exactly like a one-time password, not
# encrypted-and-decrypted like the TOTP secret is (nothing ever needs it
# back in plaintext, only needs to check a guess against it).
RECOVERY_CODE_COUNT = 10
# Excludes 0/O and 1/I -- the two pairs people misread most often when a
# code is handwritten or read off a screen aloud.
_RECOVERY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_CODE_PATTERN = re.compile(r"^[A-Z2-9]{5}-[A-Z2-9]{5}$")


def generate_recovery_code() -> str:
    """One code, formatted as `XXXXX-XXXXX` (10 characters from a 32-symbol
    alphabet -- roughly 49 bits of entropy, comfortably more than a TOTP
    code, while still short enough to type in by hand if needed)."""
    raw = "".join(secrets.choice(_RECOVERY_CODE_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list:
    """A fresh batch of `count` DISTINCT codes (collisions are astronomically
    unlikely at this alphabet/length, but de-duplicated defensively anyway
    since these are shown to a human as a numbered list -- a visible
    duplicate would look like a bug)."""
    codes = set()
    while len(codes) < count:
        codes.add(generate_recovery_code())
    return list(codes)


def is_recovery_code_format(code: str) -> bool:
    """True for `XXXXX-XXXXX`-shaped input -- lets mfa_verify() (auth_service.py)
    tell a recovery code apart from a 6-digit TOTP code without needing the
    caller to say up front which kind they're submitting."""
    return bool(_RECOVERY_CODE_PATTERN.match((code or "").strip().upper()))


def normalize_recovery_code(code: str) -> str:
    """Recovery codes are generated uppercase; normalize input the same
    way before hashing/verifying so a lowercase paste still matches."""
    return (code or "").strip().upper()
