"""add asset department

Revision ID: 0015_asset_department
Revises: 0014_pending_approval_sla_nudges
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_asset_department"
down_revision = "0014_pending_approval_sla_nudges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asset_types", sa.Column("department", sa.String(), nullable=True))
    op.create_index("ix_asset_types_department", "asset_types", ["department"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_asset_types_department", table_name="asset_types")
    op.drop_column("asset_types", "department")
