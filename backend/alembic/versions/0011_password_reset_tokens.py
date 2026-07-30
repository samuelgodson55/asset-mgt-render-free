"""add password_reset_tokens table ("forgot password?" recovery)

Revision ID: 0011_password_reset_tokens
Revises: 0010_partition_audit_logs
Create Date: 2026-07-29

WHAT THIS MIGRATION DOES
-------------------------
Adds `password_reset_tokens`, a child table of `users` -- see models.py's
PasswordResetToken docstring for the full feature rationale. One row per
issued "forgot password?" email link:
  - `user_id`      -- FK to `users.id` (which account this token belongs to)
  - `token_hash`   -- Argon2id hash of the mailed token (same hashing as
                      passwords/recovery codes -- never the plaintext
                      token, never reversible)
  - `created_at`   -- when the reset was requested
  - `expires_at`   -- when this token stops being redeemable (see
                      config.py's PASSWORD_RESET_TOKEN_EXPIRY_MINUTES)
  - `used_at`      -- NULL until consumed; stamped (not deleted) on use,
                      so there's an audit trail, same as recovery_codes

WHY THIS EXISTS
----------------
Before this, the ONLY ways to recover a forgotten password were:
  (a) already knowing it well enough to re-type it (self-service
      update_password() requires the CURRENT password), or
  (b) a Super Admin/Admin resetting a DIFFERENT user's password for them
      (services/user_service.py -> reset_user_password()).
Neither covers the one account with nobody "above" it: SUPER_ADMIN_ROLE.
This table is what request_password_reset()/confirm_password_reset()
(services/auth_service.py) use to give every account -- SUPER_ADMIN_ROLE
included -- a genuine, email-based self-recovery path.

No backfill needed -- this is a brand new table with no equivalent prior
data.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_password_reset_tokens"
down_revision = "0010_partition_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GUARD (matches 0002_bootstrap_root_admin.py's own
    # already_bootstrapped idempotency check): a VM deploy of this exact
    # migration once failed with psycopg2.errors.DuplicateTable because
    # this container's own FastAPI startup (create_all(), via
    # AUTO_INIT_DB) raced this workflow's separate, explicit "alembic
    # upgrade head" step and created `password_reset_tokens` first --
    # see config.py's apply_environment_defaults() and
    # docker-compose.vm.yml's AUTO_INIT_DB comment for the fix that stops
    # that race from happening again on any FUTURE deploy. That fix does
    # nothing for a database that already got hit by the race BEFORE it
    # shipped, though: create_all() doesn't stamp alembic_version, so
    # such a database is left with the table already present but this
    # migration still un-applied as far as Alembic is concerned --
    # meaning every subsequent "alembic upgrade head" run keeps retrying
    # the same CREATE TABLE and keeps dying the same way, forever,
    # without ever being able to stamp past it. Checking for the table
    # first makes this migration safe to run either way: a genuinely
    # fresh database still gets the table created here as before, while
    # a database where it already exists (whatever the reason) just gets
    # the revision stamped forward, exactly like re-running this
    # migration against a database already at head is supposed to
    # behave.
    #
    # NOTE: `user_id` is declared with index=True below, so Alembic's
    # create_table() already emits `CREATE INDEX
    # ix_password_reset_tokens_user_id` right after the table DDL -- do
    # NOT also call op.create_index() for this same column (see
    # 0009_recovery_codes.py's identical note for why that double-creates
    # and fails).
    bind = op.get_bind()
    if sa.inspect(bind).has_table("password_reset_tokens"):
        return

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Symmetric guard: a downgrade run against a database where this
    # table was never actually created by upgrade() here (e.g. the
    # upgrade() guard above found it already present and just stamped
    # past it, then a later downgrade to before 0011 is requested twice)
    # should be a safe no-op the second time, not an error.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("password_reset_tokens"):
        return

    # drop_table() removes the table's indexes (including
    # ix_password_reset_tokens_user_id) along with it -- no separate
    # drop_index() call needed/wanted.
    op.drop_table("password_reset_tokens")
