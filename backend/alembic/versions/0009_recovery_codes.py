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
    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_recovery_codes_user_id", table_name="recovery_codes")
    op.drop_table("recovery_codes")
