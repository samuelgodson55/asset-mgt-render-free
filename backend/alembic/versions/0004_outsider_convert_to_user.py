"""add converted_to_user_id to outsiders (Outsider -> real User migration)

Revision ID: 0004_outsider_convert_to_user
Revises: 0003_outsider_soft_delete
Create Date: 2026-07-19

WHAT THIS MIGRATION DOES
-------------------------
Adds `converted_to_user_id` to the `outsiders` table -- a nullable FK to
`users.id`, populated by services/outsider_service.py's brand-new
`convert_outsider_to_user()` the moment an Ad-Hoc Individual (someone who
was dispatched equipment without ever holding a login) decides they want
a real account after all.

This is a permanent, explicit traceability link, not just a line in the
Audit Trail: it lets any future listing/report answer "which ad-hoc
profile did this real user account originally come from?" (or the
reverse) with a plain join -- the same reasoning as
AssetCheckout.quotation_id, which exists purely so a checkout can be
traced back to the Quotation that produced it (see that column's own
comment in models.py's baseline migration).

Nullable, with no server_default needed: every existing outsider row
predates this feature and was never converted, so NULL ("never
converted") is the correct value for all of them, both for rows that
already exist and for the ones create_all() (AUTO_INIT_DB=true) would
produce on a fresh dev database.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_outsider_convert_to_user"
down_revision = "0003_outsider_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outsiders", sa.Column("converted_to_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_outsiders_converted_to_user_id_users",
        "outsiders", "users",
        ["converted_to_user_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_outsiders_converted_to_user_id_users", "outsiders", type_="foreignkey")
    op.drop_column("outsiders", "converted_to_user_id")
