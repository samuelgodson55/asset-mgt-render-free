"""add quotation_notifications table (in-app "assigned"/"updated" alerts)

Revision ID: 0013_quotation_notifications
Revises: 0012_user_company
Create Date: 2026-08-10

WHAT THIS MIGRATION DOES
-------------------------
Adds `quotation_notifications` -- see models.py's QuotationNotification
docstring for the full feature rationale. One row per (quotation,
notify-worthy change) FOR a specific linked-user recipient:
  - `quotation_id`        -- FK to `quotations.id`
  - `recipient_user_id`   -- FK to `users.id` (who this notification is for)
  - `kind`                -- "assigned" | "updated"
  - `message`             -- pre-rendered, human-readable notification text
  - `created_by`          -- email of the Admin/Manager who made the change
  - `created_at`          -- when the event happened
  - `read_at`             -- NULL until the recipient views it in-app

No backfill needed -- this is a brand new table with no equivalent prior
data (existing Quotation assignments/edits before this feature shipped
simply never produced a notification, same as any other net-new alert
type added to this app).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_quotation_notifications"
down_revision = "0012_user_company"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GUARD: same idempotency check as 0011_password_reset_tokens.py's
    # identical guard -- protects against AUTO_INIT_DB's create_all()
    # racing this migration and creating the table first (see that
    # migration's own comment for the full explanation).
    bind = op.get_bind()
    if sa.inspect(bind).has_table("quotation_notifications"):
        return

    # `quotation_id`/`recipient_user_id` are both declared with
    # index=True below, so Alembic's create_table() already emits their
    # indexes as part of the table DDL -- do NOT also call
    # op.create_index() for either column (see 0009_recovery_codes.py's
    # note for why that double-creates and fails).
    op.create_table(
        "quotation_notifications",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("quotations.id"), nullable=False, index=True),
        sa.Column("recipient_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Symmetric guard -- same reasoning as 0011_password_reset_tokens.py's
    # identical downgrade guard.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("quotation_notifications"):
        return

    op.drop_table("quotation_notifications")
