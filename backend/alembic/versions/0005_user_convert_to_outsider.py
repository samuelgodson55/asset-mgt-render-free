"""add converted_to_outsider_id to users (User -> Outsider revoke-login migration)

Revision ID: 0005_user_convert_to_outsider
Revises: 0004_outsider_convert_to_user
Create Date: 2026-07-19

WHAT THIS MIGRATION DOES
-------------------------
Adds `converted_to_outsider_id` to the `users` table -- a nullable FK to
`outsiders.id`, populated by services/user_service.py's brand-new
`convert_user_to_outsider()` the moment a Super Admin/Admin or Manager
revokes a real login's access, turning that account back into an ad-hoc
(no-login) profile.

This is the exact reverse of 0004_outsider_convert_to_user.py's
`outsiders.converted_to_user_id`: a permanent, explicit traceability
link, not just a line in the Audit Trail, so "which ad-hoc profile did
this now-revoked account become?" (or the reverse) can be answered with a
plain join.

Nullable, with no server_default needed: every existing user row predates
this feature and was never converted, so NULL ("never converted") is the
correct value for all of them, both for rows that already exist and for
the ones create_all() (AUTO_INIT_DB=true) would produce on a fresh dev
database.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_user_convert_to_outsider"
down_revision = "0004_outsider_convert_to_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("converted_to_outsider_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_users_converted_to_outsider_id_outsiders",
        "users", "outsiders",
        ["converted_to_outsider_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_converted_to_outsider_id_outsiders", "users", type_="foreignkey")
    op.drop_column("users", "converted_to_outsider_id")
