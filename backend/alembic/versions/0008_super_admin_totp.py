"""add users.totp_secret_encrypted / users.totp_enabled (2FA)

Revision ID: 0008_super_admin_totp
Revises: 0007_split_contact_details
Create Date: 2026-07-20

WHAT THIS MIGRATION DOES
-------------------------
Adds two nullable-safe columns to `users`:
  - `totp_secret_encrypted` (String, nullable) -- a Fernet-encrypted TOTP
    secret (never plaintext -- see security.py's encrypt_totp_secret()).
    NULL for every account that hasn't gone through 2FA enrollment.
  - `totp_enabled` (Boolean, NOT NULL, default False) -- flips to True only
    once a live code has actually been confirmed against the secret above
    (see services/auth_service.py's mfa_setup_confirm()). Every
    pre-existing row backfills to False, which is exactly correct: nobody
    had 2FA configured before this column existed.

Enforcement (which accounts are actually REQUIRED to have 2FA configured,
currently just role == SUPER_ADMIN_ROLE) lives entirely in application
code (auth_service.py's login()), not the schema -- these columns alone
don't force anything; they just give the app somewhere to persist the
state once it decides to ask.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_super_admin_totp"
down_revision = "0007_split_contact_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret_encrypted", sa.String(), nullable=True))
    op.add_column(
        "users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    # Drop the server_default once existing rows are backfilled -- new rows
    # go through the ORM's Column(default=False) instead from here on,
    # same pattern as every other Boolean column in this schema (see
    # 0001_baseline_schema.py's equivalent columns for precedent).
    op.alter_column("users", "totp_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret_encrypted")
