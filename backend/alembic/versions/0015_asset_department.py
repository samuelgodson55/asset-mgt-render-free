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
    # AUTO_INIT_DB/create_all() can legitimately create the current model
    # shape before Alembic gets a chance to stamp the database. In that case
    # the department column and its index already exist; make this migration
    # idempotent so restore reconciliation can safely stamp/upgrade past it.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("asset_types")}

    if "department" not in columns:
        op.add_column("asset_types", sa.Column("department", sa.String(), nullable=True))

    indexes = {idx.get("name") for idx in inspector.get_indexes("asset_types")}
    if "ix_asset_types_department" not in indexes:
        op.create_index("ix_asset_types_department", "asset_types", ["department"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx.get("name") for idx in inspector.get_indexes("asset_types")}
    if "ix_asset_types_department" in indexes:
        op.drop_index("ix_asset_types_department", table_name="asset_types")

    columns = {col["name"] for col in inspector.get_columns("asset_types")}
    if "department" in columns:
        op.drop_column("asset_types", "department")
