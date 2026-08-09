"""add users.company

Revision ID: 0012_user_company
Revises: 0011_password_reset_tokens
Create Date: 2026-08-09

WHAT THIS MIGRATION DOES
-------------------------
Adds a new nullable `users.company` column -- an external, purely
descriptive contact detail (e.g. "which company does this Customer/
External Client Contact work for"), distinct from `users.department`
(an INTERNAL team within this org, which also scopes a Manager's
visibility). See models.py's `User.company` docstring for the full
rationale, and `Outsider.company` for the equivalent field this mirrors
on ad-hoc (no-login) profiles.

Nullable with no backfill needed -- every pre-existing user row predates
this feature, and NULL ("not set") is correct for all of them; nothing
about login, permissions, or any other existing behavior depends on it.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_user_company"
down_revision = "0011_password_reset_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GUARD (matches 0011_password_reset_tokens.py's own has_table check):
    # protects against the same create_all()-vs-alembic race described
    # there, here for a column add rather than a whole table.
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "company" in columns:
        return

    op.add_column("users", sa.Column("company", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "company" not in columns:
        return

    op.drop_column("users", "company")
