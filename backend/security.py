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
import types
import datetime
import jwt  # PyJWT package
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
# HARDCODED SUPER ADMIN (root account)
# ---------------------------------------------------------------------------
# The Super Admin is deliberately NOT a `models.User` row -- it's a single
# fixed identity built entirely from config.py's SUPER_ADMIN_* settings.
# That's what makes it possible to guarantee, structurally, that:
#   1. There is always EXACTLY one Super Admin (it's one constant, not a
#      queryable/creatable table row).
#   2. It can never be deleted -- `DELETE /users/{id}` (see
#      services/user_service.py -> delete_user()) only ever operates on
#      real `users` table rows, and this account isn't one.
#   3. It never appears in the User Directory or any other listing --
#      those all come from `SELECT ... FROM users`, which this identity
#      never touches.
#
# SUPER_ADMIN_ID is a sentinel used as this account's JWT "sub" (subject)
# claim. Postgres SERIAL primary keys always start at 1 and only ever go
# up, so a negative id can never collide with a genuine `users.id` value --
# deps.py's get_current_user() uses this to recognize a Super Admin token
# and skip the (otherwise mandatory) "look this user up in the database"
# step entirely.
SUPER_ADMIN_ID = -1
SUPER_ADMIN_ROLE = "super_admin"


def super_admin_password_hash() -> str | None:
    """
    Hashes `settings.SUPER_ADMIN_PASSWORD` once. Returns None when that
    setting is empty, which fully and deliberately disables the Super
    Admin login path (see services/auth_service.py -> login()) rather than
    ever accepting a blank password.
    """
    if not settings.SUPER_ADMIN_PASSWORD:
        return None
    return hash_password(settings.SUPER_ADMIN_PASSWORD)


# Computed once at process startup, exactly like _DUMMY_PASSWORD_HASH in
# auth_service.py -- hashing is deliberately expensive, so we never want to
# repeat it on every single login attempt.
SUPER_ADMIN_PASSWORD_HASH = super_admin_password_hash()


def super_admin_principal():
    """
    A `models.User`-shaped stand-in for the hardcoded Super Admin, so
    `create_access_token()` below can treat it exactly like a real user
    when issuing a JWT. `email` is synthetic (there's no real mailbox) --
    it only exists so audit-log entries and `operator=` fields have
    something readable to display for actions this account performs.
    """
    return types.SimpleNamespace(
        id=SUPER_ADMIN_ID,
        name=settings.SUPER_ADMIN_NAME,
        email=f"{settings.SUPER_ADMIN_USERNAME}@local",
        username=settings.SUPER_ADMIN_USERNAME,
        role=SUPER_ADMIN_ROLE,
        department=None,
    )


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
