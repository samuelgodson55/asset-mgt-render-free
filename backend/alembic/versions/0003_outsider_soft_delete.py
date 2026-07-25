"""add soft-delete columns to outsiders

Revision ID: 0003_outsider_soft_delete
Revises: 0002_bootstrap_root_admin
Create Date: 2026-07-18

WHAT THIS MIGRATION DOES
-------------------------
Adds `is_deleted`/`deleted_at` to the `outsiders` table, mirroring the
`users` table's own soft-delete columns (see 0001_baseline_schema.py's
`users` table def). This backs the new "Ad-Hoc individuals are deletable
by an Admin/Manager" feature (services/outsider_service.py ->
delete_outsider()) -- a models.Outsider row is what
AssetCheckout.outsider_id / Quotation.assigned_outsider_id actually point
at, so it's never hard-deleted; "deleting" one just flips these two flags,
same reasoning as the `users` table has always used.

Both columns are added NOT NULL with a server_default so this is a safe,
zero-downtime migration against a table that may already have rows --
every existing ad-hoc profile is implicitly "not deleted" the moment this
migration lands, which is exactly correct (none of them were deletable
before this feature existed).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_outsider_soft_delete"
down_revision = "0002_bootstrap_root_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outsiders", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("outsiders", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("outsiders", "deleted_at")
    op.drop_column("outsiders", "is_deleted")
