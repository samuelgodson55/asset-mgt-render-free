"""add recovery_codes table (2FA backup codes)

Revision ID: 0009_recovery_codes
Revises: 0008_super_admin_totp
Create Date: 2026-07-21

WHAT THIS MIGRATION DOES
-------------------------
Adds `recovery_codes`, a child table of `users` -- see models.py's
RecoveryCode docstring for the full feature rationale. One row per
issued single-use backup code:
  - `user_id`      -- FK to `users.id` (which account this code belongs to)
  - `code_hash`     -- Argon2id hash of the code (same hashing as
                       passwords -- never the plaintext code, never
                       reversible)
  - `created_at`    -- when this batch was issued
  - `used_at`       -- NULL until consumed; stamped (not deleted) on use,
                       so there's an audit trail

No backfill needed -- this is a brand new table with no equivalent prior
data (2FA itself, and therefore recovery codes, didn't exist before
0008_super_admin_totp.py).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_recovery_codes"
down_revision = "0008_super_admin_totp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOTE: `user_id` is declared with index=True below, so Alembic's
    # create_table() already emits `CREATE INDEX ix_recovery_codes_user_id`
    # right after the table DDL (it walks table.indexes internally --
    # see alembic/ddl/impl.py's create_table()). Do NOT also call
    # op.create_index() for this same column here -- a previous revision
    # of this migration did both, and the second, explicit create_index()
    # call collided with the one Alembic had just auto-created, failing
    # with psycopg2.errors.DuplicateTable on `ix_recovery_codes_user_id`.
    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # drop_table() removes the table's indexes (including
    # ix_recovery_codes_user_id) along with it -- no separate
    # drop_index() call needed/wanted.
    op.drop_table("recovery_codes")
