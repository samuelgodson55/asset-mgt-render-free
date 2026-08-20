"""add users.credentials_changed_at

Revision ID: 0018_credentials_changed_at
Revises: 0017_single_super_admin
Create Date: 2026-08-19

WHAT THIS MIGRATION DOES
-------------------------
Adds a new nullable `users.credentials_changed_at` column -- the per-user
counterpart to security.py's AUTH_EPOCH_SETTING_KEY. See models.py's
`User.credentials_changed_at` docstring for the full rationale: it's
stamped with the current time whenever this row's `password_hash`
changes, and deps.py's resolve_user_from_token() rejects any JWT issued
before that timestamp, so a password change now actually revokes any
session that was already live -- not just future logins with the old
password.

Nullable with no backfill needed -- every pre-existing user row predates
this feature and simply has no revocation point yet, which is correct:
nothing about their currently-live sessions should change retroactively
just because this column was added.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018_credentials_changed_at"
down_revision = "0017_single_super_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GUARD (matches 0012_user_company.py's own has_column check):
    # protects against the same create_all()-vs-alembic race described
    # there.
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "credentials_changed_at" in columns:
        return

    op.add_column("users", sa.Column("credentials_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "credentials_changed_at" not in columns:
        return

    op.drop_column("users", "credentials_changed_at")
