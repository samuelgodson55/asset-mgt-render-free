"""add purged_at to users and asset_types (Purge Deleted Users/Assets)

Revision ID: 0006_purge_deleted
Revises: 0005_user_convert_to_outsider
Create Date: 2026-07-19

WHAT THIS MIGRATION DOES
-------------------------
Adds a single nullable `purged_at` column to both `users` and
`asset_types`, backing the new "Purge" button on the Restore Deleted
Users / Restore Deleted Assets panels (services/user_service.py's
purge_user() and services/asset_service.py's purge_asset_type()).

WHY THIS IS NEEDED
------------------
`users.email`/`users.username` and `asset_types.name` all carry
DB-level `unique=True` constraints. A soft-deleted row (is_deleted=True)
still occupies its original email/username/name forever, so that value
can never be reused by a brand-new account or pool -- by design, so a
restore can't collide with anything created in the meantime. There was
previously no way to deliberately free that value back up short of
restoring the old row first (re-enabling its login/reappearing in
inventory) purely to rename it, which defeats the point of having
deleted it.

Purging solves this WITHOUT hard-deleting the row (a hard delete would
either violate the foreign keys from AssetCheckout.user_id/asset_id,
Quotation.assigned_to_id, AssetException.asset_type_id, etc., or -- if
those were CASCADE -- silently erase historical custody/audit records,
exactly the thing every other soft-delete in this app is designed to
prevent). Instead, purging overwrites just the unique-constrained
field(s) (email + username for a user; name for an asset pool) with a
guaranteed-unique placeholder and stamps `purged_at`, so:
  - the original email/username/name is free for a brand-new row to use
    immediately, and
  - the purged row, and every historical checkout/quotation/exception
    that still points at it, remains fully intact and queryable.

Nullable, with no server_default needed: every existing row predates
this feature and was never purged, so NULL ("not purged") is the
correct value for all of them, both for rows that already exist and for
the ones create_all() (AUTO_INIT_DB=true) would produce on a fresh dev
database.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_purge_deleted"
down_revision = "0005_user_convert_to_outsider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("asset_types", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("asset_types", "purged_at")
    op.drop_column("users", "purged_at")
