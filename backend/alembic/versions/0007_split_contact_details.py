"""split outsider contact_details into email/phone_number; add user.phone_number

Revision ID: 0007_split_contact_details
Revises: 0006_purge_deleted
Create Date: 2026-07-19

WHAT THIS MIGRATION DOES
-------------------------
1. `outsiders.contact_details` (a single required free-text column that
   held either an email or a phone number, ambiguously) is renamed to
   `outsiders.email` and relaxed to nullable=True.
2. A new nullable `outsiders.phone_number` column is added.
3. A new nullable `users.phone_number` column is added (Users never had
   any phone field before).

WHY A RENAME RATHER THAN A NEW COLUMN + DROP
----------------------------------------------
Every existing ad-hoc profile's `contact_details` value is real data --
historically it was always populated either from a typed-in email/phone
at ad-hoc-creation time, or (via services/user_service.py's
convert_user_to_outsider()) defaulted straight from the source account's
`email`. In practice the column overwhelmingly held email addresses (the
only place a phone number could get in was a manually-typed ad-hoc
dispatch/quote-assignment field), so renaming it directly into the new
`email` column preserves that data with zero loss and no separate
backfill step needed. `phone_number` starts NULL for every pre-existing
row, which is exactly correct -- there's no way to retroactively know
which of them were actually phone numbers, and it's a purely optional
field going forward (the app-level check only requires at least one of
email/phone to be present, and a pre-existing row already has its
(renamed) email, so that check is still satisfied for all of them).

nullable=True on the renamed column matches models.py's new
`Outsider.email` definition -- the app no longer enforces "must have
contact_details" as a blanket rule, only "must have email OR phone" at
ad-hoc-creation time.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_split_contact_details"
down_revision = "0006_purge_deleted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "outsiders", "contact_details",
        new_column_name="email",
        existing_type=sa.String(),
        nullable=True,
    )
    op.add_column("outsiders", sa.Column("phone_number", sa.String(), nullable=True))
    op.add_column("users", sa.Column("phone_number", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "phone_number")
    op.drop_column("outsiders", "phone_number")
    op.alter_column(
        "outsiders", "email",
        new_column_name="contact_details",
        existing_type=sa.String(),
        nullable=False,
    )
