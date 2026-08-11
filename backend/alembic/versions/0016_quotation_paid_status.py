"""add quotation paid status and payment audit fields

Revision ID: 0016_quotation_paid_status
Revises: 0015_asset_department
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_quotation_paid_status"
down_revision = "0015_asset_department"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotations", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("quotations", sa.Column("paid_by_id", sa.Integer(), nullable=True))
    op.add_column("quotations", sa.Column("payment_method", sa.String(), nullable=True))
    op.add_column("quotations", sa.Column("payment_reference", sa.String(), nullable=True))
    op.create_foreign_key("fk_quotations_paid_by_id_users", "quotations", "users", ["paid_by_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_quotations_paid_by_id_users", "quotations", type_="foreignkey")
    op.drop_column("quotations", "payment_reference")
    op.drop_column("quotations", "payment_method")
    op.drop_column("quotations", "paid_by_id")
    op.drop_column("quotations", "paid_at")
