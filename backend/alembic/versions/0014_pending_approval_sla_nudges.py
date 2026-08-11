"""add extension_requests.sla_last_reminded_at + quotations.sla_last_reminded_at

Revision ID: 0014_pending_approval_sla_nudges
Revises: 0013_quotation_notifications
Create Date: 2026-08-11

WHAT THIS MIGRATION DOES
-------------------------
Adds one new nullable `sla_last_reminded_at` column to each of
`extension_requests` and `quotations` -- see models.py's
`ExtensionRequest.sla_last_reminded_at` / `Quotation.sla_last_reminded_at`
docstrings for the full rationale. Both back the new SLA-nudge Celery Beat
jobs in tasks/sla_tasks.py: a still-`pending` ExtensionRequest / still-
`submitted` Quotation that's sat unanswered past
`settings.EXTENSION_REQUEST_SLA_HOURS` / `settings.QUOTATION_SLA_HOURS`
gets escalated to the notification-recipients audience, and this column
records when that last happened so the same row isn't re-escalated more
often than `settings.APPROVAL_SLA_ESCALATION_REPEAT_HOURS`.

Nullable with no backfill needed -- every pre-existing row predates this
feature, and NULL ("never nudged yet") is the correct starting value for
all of them, including rows that are no longer pending/submitted (those
simply never match the SLA task's query regardless of this column).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_pending_approval_sla_nudges"
down_revision = "0013_quotation_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GUARD: same idempotency pattern as 0012_user_company.py's own
    # add_column guard -- protects against AUTO_INIT_DB's create_all()
    # racing this migration and creating the column first.
    bind = op.get_bind()

    extension_request_columns = {col["name"] for col in sa.inspect(bind).get_columns("extension_requests")}
    if "sla_last_reminded_at" not in extension_request_columns:
        op.add_column("extension_requests", sa.Column("sla_last_reminded_at", sa.DateTime(timezone=True), nullable=True))

    quotation_columns = {col["name"] for col in sa.inspect(bind).get_columns("quotations")}
    if "sla_last_reminded_at" not in quotation_columns:
        op.add_column("quotations", sa.Column("sla_last_reminded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    extension_request_columns = {col["name"] for col in sa.inspect(bind).get_columns("extension_requests")}
    if "sla_last_reminded_at" in extension_request_columns:
        op.drop_column("extension_requests", "sla_last_reminded_at")

    quotation_columns = {col["name"] for col in sa.inspect(bind).get_columns("quotations")}
    if "sla_last_reminded_at" in quotation_columns:
        op.drop_column("quotations", "sla_last_reminded_at")
