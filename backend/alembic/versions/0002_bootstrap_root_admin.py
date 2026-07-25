"""bootstrap root admin account

Revision ID: 0002_bootstrap_root_admin
Revises: 0001_baseline_schema
Create Date: 2026-07-18

SECURITY CHANGE THIS MIGRATION IMPLEMENTS
------------------------------------------
The root admin ("super_admin") used to be a single fixed identity checked
directly against the SUPER_ADMIN_USERNAME/SUPER_ADMIN_PASSWORD environment
variables in services/auth_service.py's login(), BEFORE the `users` table
was ever touched -- it had no row of its own, so its credential lived
entirely outside the database and couldn't be rotated, reset, or audited
through any of the app's normal account-management flows.

This migration inserts that account as a REAL, single, `users` table row
instead, the FIRST time `alembic upgrade head` runs against a PRODUCTION
database (see _IS_PRODUCTION below -- it's a deliberate no-op everywhere
else; local/dev/test get an equivalent row from database.py's seed_db(),
with a well-known demo password, since they never run this migration
path with a production database behind them). From this point on:
  - Its password is a normal Argon2id hash in `password_hash`, exactly
    like every other account -- rotatable via the same self-service
    change-password / Admin-issued reset flows (see services/auth_service.py
    and services/user_service.py), each producing a normal, queryable
    `AuditLog` row. There is no more "change it by editing the server
    environment and restarting" escape hatch.
  - Its IDENTITY (username/display name) is still fixed/hardcoded -- not
    by an env-var comparison in login() anymore, but structurally: this
    migration only ever inserts ONE row with role="super_admin" (see the
    existing-row guard below), services/user_service.py's create_user()
    permanently reserves that role so nothing else can ever be provisioned
    with it, and it can never be edited/deleted through the app (see
    services/user_service.py's is_hidden_root_admin() guard).
  - It's excluded from the User Directory, bulk exports, and the Audit
    Trail everywhere those are listed (see services/user_service.py and
    services/audit_service.py) -- a real, fully-auditable database row
    that nonetheless never appears in the ordinary admin-facing UI.

WHY THIS MIGRATION READS `os.environ` DIRECTLY INSTEAD OF `from config
import settings`
------------------------------------------------------------------------
Same reasoning as alembic/env.py's `_MigrationSettings` class: importing
the real `config.settings` runs `_enforce_prod_jwt_secret` at import time,
which is exactly the right check for the running app but an unnecessary
coupling for a migration that only needs a couple of plain, non-secret
values. Reading `os.environ` directly here keeps this migration runnable
with nothing more than DATABASE_URL set, same as every other migration.

ROOT_ADMIN_BOOTSTRAP_PASSWORD IS OPTIONAL AND ONLY EVER READ HERE
------------------------------------------------------------------
If set, its value seeds the root admin's initial password hash and is
never referenced again by any other part of the app (there is no runtime
code path that reads it -- see services/auth_service.py's login()). If
left unset, this migration generates a random one, hashes it into the
row, and prints the plaintext to stderr exactly once, right now, with
instructions to save it and rotate it immediately via the normal Change
Password flow. Either way, after this migration finishes, the ONLY place
this credential lives is the `password_hash` column -- there's nothing to
"unset and restart the backend" to revoke, and no copy of it sits in an
environment variable or a `.env` file going forward.
"""
import os
import secrets
import sys

import sqlalchemy as sa
from alembic import op
from pwdlib import PasswordHash

# revision identifiers, used by Alembic.
revision = "0002_bootstrap_root_admin"
down_revision = "0001_baseline_schema"
branch_labels = None
depends_on = None

_ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").strip().lower()
_IS_PRODUCTION = _ENVIRONMENT in ("production", "prod")

# Same env var names config.py's SUPER_ADMIN_USERNAME/SUPER_ADMIN_NAME
# settings use, on purpose -- set them once in your `.env` and both this
# migration and the running app agree on the root account's identity.
_ROOT_ADMIN_USERNAME = os.environ.get("SUPER_ADMIN_USERNAME", "superadmin").strip().lower()
_ROOT_ADMIN_NAME = os.environ.get("SUPER_ADMIN_NAME", "Super Admin").strip()
_ROOT_ADMIN_EMAIL = f"{_ROOT_ADMIN_USERNAME}@local"
_ROOT_ADMIN_ROLE = "super_admin"

# Lightweight, ORM-independent view of just the columns this migration
# touches -- migrations should never import the live `models.User` class
# (its shape is expected to keep changing over time; this file must stay
# correct exactly as written, forever, regardless of future model edits).
_users_table = sa.table(
    "users",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("email", sa.String),
    sa.column("username", sa.String),
    sa.column("role", sa.String),
    sa.column("password_hash", sa.String),
    sa.column("is_verified", sa.Boolean),
    sa.column("is_active", sa.Boolean),
    sa.column("failed_login_attempts", sa.Integer),
    sa.column("is_deleted", sa.Boolean),
    sa.column("department", sa.String),
    sa.column("department_role", sa.String),
)


def upgrade() -> None:
    if not _IS_PRODUCTION:
        # Local/dev/test databases get an equivalent row from
        # database.py's seed_db() instead (with a well-known demo
        # password) -- this migration is a deliberate no-op everywhere
        # ENVIRONMENT isn't "production"/"prod", so running the full
        # migration chain against a throwaway dev database never prints a
        # one-time password nobody asked for.
        return

    bind = op.get_bind()

    already_bootstrapped = bind.execute(
        sa.select(_users_table.c.id).where(_users_table.c.role == _ROOT_ADMIN_ROLE)
    ).first()
    if already_bootstrapped:
        # Re-running `alembic upgrade head` (or a downgrade/upgrade cycle)
        # against a database that already has the root admin row must
        # never insert a second one.
        return

    bootstrap_password = os.environ.get("ROOT_ADMIN_BOOTSTRAP_PASSWORD", "").strip()
    password_was_generated = False
    if not bootstrap_password:
        bootstrap_password = secrets.token_urlsafe(18)
        password_was_generated = True

    password_hash = PasswordHash.recommended().hash(bootstrap_password)

    bind.execute(
        _users_table.insert().values(
            name=_ROOT_ADMIN_NAME,
            email=_ROOT_ADMIN_EMAIL,
            username=_ROOT_ADMIN_USERNAME,
            role=_ROOT_ADMIN_ROLE,
            password_hash=password_hash,
            is_verified=True,
            is_active=True,
            failed_login_attempts=0,
            is_deleted=False,
            department=None,
            department_role=None,
        )
    )

    if password_was_generated:
        print("=" * 78, file=sys.stderr)
        print("ROOT ADMIN ACCOUNT BOOTSTRAPPED", file=sys.stderr)
        print(f"  username: {_ROOT_ADMIN_USERNAME}", file=sys.stderr)
        print(f"  password: {bootstrap_password}", file=sys.stderr)
        print("", file=sys.stderr)
        print("This password is shown ONLY ONCE, right now. It is stored as a salted", file=sys.stderr)
        print("Argon2id hash in the `users` table -- there is no other copy of it", file=sys.stderr)
        print("anywhere (not in an environment variable, not in this repo, not in any", file=sys.stderr)
        print("log after this line). Save it somewhere safe immediately, then log in", file=sys.stderr)
        print("and rotate it via the normal Change Password flow as soon as possible.", file=sys.stderr)
        print("=" * 78, file=sys.stderr)
    else:
        print(
            "Root admin account bootstrapped using ROOT_ADMIN_BOOTSTRAP_PASSWORD "
            "from the environment. Unset that variable now -- it is never read again "
            "after this migration.",
            file=sys.stderr,
        )


def downgrade() -> None:
    if not _IS_PRODUCTION:
        return
    bind = op.get_bind()
    bind.execute(_users_table.delete().where(_users_table.c.role == _ROOT_ADMIN_ROLE))
