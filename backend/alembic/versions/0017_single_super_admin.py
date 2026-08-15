"""enforce exactly one super admin account

Revision ID: 0017_single_super_admin
Revises: 0016_quotation_paid_status
Create Date: 2026-08-15

The application treats ``super_admin`` as the single root account. A restore
can otherwise reintroduce an older backup copy of that account alongside the
current account because normal user reconciliation intentionally preserves
restore-only users.

This migration repairs an already-corrupted database before adding the DB-
level invariant: choose the configured/root row when it is still identifiable,
otherwise use the oldest super_admin row as the safest legacy fallback; revoke
all other super_admin rows, then enforce a partial unique index so PostgreSQL
cannot accept a second super_admin ever again.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0017_single_super_admin"
down_revision = "0016_quotation_paid_status"
branch_labels = None
depends_on = None

ROOT_ROLE = "super_admin"
REVOKED_ROLE = "staff"
INDEX_NAME = "uq_users_single_super_admin"


def _configured_root_username() -> str:
    return os.environ.get("SUPER_ADMIN_USERNAME", "superadmin").strip().lower()


def upgrade() -> None:
    bind = op.get_bind()

    users = sa.table(
        "users",
        sa.column("id", sa.Integer),
        sa.column("username", sa.String),
        sa.column("email", sa.String),
        sa.column("role", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("is_deleted", sa.Boolean),
        sa.column("deleted_at", sa.DateTime),
        sa.column("totp_secret_encrypted", sa.String),
        sa.column("totp_enabled", sa.Boolean),
    )

    rows = bind.execute(
        sa.select(users.c.id, users.c.username, users.c.email)
        .where(users.c.role == ROOT_ROLE)
        .order_by(users.c.id.asc())
    ).mappings().all()

    if rows:
        configured = _configured_root_username()
        survivor = next(
            (row for row in rows if (row["username"] or "").strip().lower() == configured),
            rows[0],
        )

        duplicates = [row for row in rows if row["id"] != survivor["id"]]
        for duplicate in duplicates:
            bind.execute(
                sa.update(users)
                .where(users.c.id == duplicate["id"])
                .values(
                    role=REVOKED_ROLE,
                    is_active=False,
                    is_deleted=True,
                    deleted_at=sa.func.now(),
                    totp_secret_encrypted=None,
                    totp_enabled=False,
                )
            )

            # Recovery codes belong to the account, so revoke them as well.
            bind.execute(
                sa.text("DELETE FROM recovery_codes WHERE user_id = :uid"),
                {"uid": duplicate["id"]},
            )

    # PostgreSQL partial unique index: only one row can ever carry the root role.
    bind.execute(
        sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON users (role) WHERE role = 'super_admin'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
