"""baseline schema

Revision ID: 0001_baseline_schema
Revises:
Create Date: 2026-07-08

This is the baseline schema migration: it creates every table exactly as
defined in models.py, in its final, current shape. It's still a single
squashed baseline for the SCHEMA itself (no incremental
0002/0003/... chain of table/column changes on top of it) -- the one file
that now sits after it,
alembic/versions/0002_bootstrap_root_admin.py, is a pure DATA migration
(inserting the one hardcoded root admin row in production) and never
touches this file's table shapes. A fresh install (or a fresh
`docker compose up --build` against an empty Postgres volume) still only
ever needs a single `alembic upgrade head` to run both.

WHY A SINGLE SQUASHED BASELINE INSTEAD OF INCREMENTAL MIGRATIONS?
This project previously had a short-lived chain of incremental migrations
(account lockout fields, timezone-aware datetimes, username backfill,
asset_types soft delete, then later a separate "quotations + app
settings" migration and a "quotation submission workflow" migration on
top of that) written as models.py evolved. That's the normal/correct way
to evolve a schema that already has real data in it -- but for a project
that hasn't shipped real production data yet, carrying that history
forward mostly adds friction (several files to read instead of one, more
surface area for the exact "migration file referenced but missing" bug
that motivated squashing them in the first place). So this file is kept
rewritten to directly match models.py's CURRENT shape end-to-end
(including the Equipment Quotation feature's tables AND its later
submission-workflow columns), and every incremental file that used to sit
on top of it was deleted.

IF YOU HAVE AN EXISTING DATABASE that was already migrated with an older,
now-deleted chain (i.e. `alembic_version` contains anything other than
`0001_baseline_schema`), do NOT just run `alembic upgrade head` -- Alembic
will look for a migration it no longer has and error out. Instead:
  - If your tables already match this file's schema exactly (you're
    caught up on everything this file creates), just re-point Alembic's
    own bookkeeping at this revision without touching any table:
        alembic stamp 0001_baseline_schema
  - If your tables predate the Quotation submission workflow (no
    `status`/`reference_number`/`submitted_at`/`assigned_to_id`/`notes`
    columns on `quotations` yet, and `quotations.user_id` is still
    UNIQUE), run the equivalent of this file's upgrade() by hand for just
    those columns, or drop and recreate that one table if it has no rows
    you care about yet, then stamp as above.
  - If your tables predate the Quote-to-Checkout workflow (no
    `approved_at`/`approved_by_id`/`fulfilled_at`/`fulfilled_by_id`
    columns on `quotations`, and no `quotation_id` column on
    `asset_checkouts`), add those columns by hand (see this file's
    upgrade() for their exact types/FKs), then stamp as above.
  - If your tables predate Ad-Hoc quote assignment (no
    `assigned_outsider_id` column on `quotations`), add
    `sa.Column("assigned_outsider_id", sa.Integer(), sa.ForeignKey("outsiders.id"), nullable=True)`
    by hand, then stamp as above.
  - If your tables predate the Category rename (asset_types still has a
    `department` column, not `category`), run
    `ALTER TABLE asset_types RENAME COLUMN department TO category;` by
    hand, then stamp as above. (Note: `users.department` is a SEPARATE
    column -- the person's own team -- and is NOT renamed by this; only
    the asset pool's own column changed name.)
  - If your tables predate the per-quote Discount field (no
    `discount_percent` column on `quotations`), run
    `ALTER TABLE quotations ADD COLUMN discount_percent NUMERIC(5, 2) NOT NULL DEFAULT 0;`
    by hand, then stamp as above.
Fresh installs (`asset_db` doesn't exist yet, or is fully empty) can
ignore all of this and just run `alembic upgrade head` as normal.

Going forward, every schema change should be a NEW migration generated
with `alembic revision --autogenerate -m "description"` -- do not keep
hand-editing this baseline file once real data exists anywhere.
"""
from alembic import op
import sqlalchemy as sa

# Alembic identifiers, used by Alembic itself -- do not edit by hand.
revision = "0001_baseline_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_types",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("total_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("custom_fields", sa.JSON(), nullable=True),
        # --- Category (optional) -- see models.py's AssetType docstring ---
        sa.Column("category", sa.String(), nullable=True),
        # --- Per-unit price (optional) -- see models.py's AssetType docstring ---
        sa.Column("price", sa.Numeric(10, 2), nullable=True),
        # --- Soft delete -- see models.py's AssetType docstring ---
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False, unique=True, index=True),
        # --- Username login -- see models.py's User.username docstring ---
        sa.Column("username", sa.String(), nullable=True, unique=True, index=True),
        sa.Column("role", sa.String(), server_default="staff"),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # --- Per-account brute-force lockout -- see models.py's User docstring ---
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        # --- Soft delete ---
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("department_role", sa.String(), nullable=True),
    )

    op.create_table(
        "outsiders",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("contact_details", sa.String(), nullable=False),
        sa.Column("company", sa.String(), nullable=True),
    )

    op.create_table(
        "asset_exceptions",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("asset_type_id", sa.Integer(), sa.ForeignKey("asset_types.id"), nullable=False),
        sa.Column("serial_number", sa.String(), nullable=False, unique=True),
        sa.Column("status_label", sa.String(), nullable=False, server_default="Undeployable"),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("isolation_status", sa.String(), nullable=False, server_default="isolated"),
        sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("operator", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("details", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "asset_checkouts",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        # Nullable -- see models.py's AssetCheckout.asset_id comment: an
        # OUTSOURCED checkout (is_outsourced=True below) has no real
        # AssetType pool to point at.
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("asset_types.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("outsider_id", sa.Integer(), sa.ForeignKey("outsiders.id"), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("quantity_returned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkout_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), server_default="active"),
        # Traceability link back to the Quotation this checkout was
        # created from by the Fulfillment Drawer's bulk physical checkout
        # (NULL for a normal, directly-dispatched checkout). No FK
        # constraint added here -- the "quotations" table doesn't exist
        # yet at this point in the script -- see the
        # "ix_asset_checkouts_quotation_id" FK added further down, once
        # "quotations" has been created.
        sa.Column("quotation_id", sa.Integer(), nullable=True),
        # --- Outsourced (not-in-inventory) items -- see models.py's
        # AssetCheckout docstring/comment for the full rationale. ---
        sa.Column("is_outsourced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("outsourced_item_name", sa.String(), nullable=True),
        sa.Column("outsourced_unit_price", sa.Numeric(10, 2), nullable=True),
        sa.Column("outsourced_source", sa.String(), nullable=True),
    )

    op.create_table(
        "extension_requests",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("checkout_id", sa.Integer(), sa.ForeignKey("asset_checkouts.id"), nullable=False),
        sa.Column("requested_by_label", sa.String(), nullable=False),
        sa.Column("previous_due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_new_due_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- Equipment Quotation feature ---
    # app_settings: generic runtime-editable key/value store. Currently
    # holds one row, key="vat_percent", which backs the admin-only
    # configurable VAT applied to every quotation (see
    # services/quotation_service.py).
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # quotations: each account's "My Order" cart (status="draft") plus
    # every quote they've formally submitted (status="submitted", each
    # with its own permanent, human-shareable `reference_number` an
    # Admin/Manager can look up in the "Quotes" tab, adjust, and assign
    # to a user via `assigned_to_id`). See models.py's Quotation
    # docstring for the full lifecycle. Deliberately NOT unique on
    # `user_id` -- one account accumulates one row per submitted quote
    # plus at most one open draft, not a single row forever.
    op.create_table(
        "quotations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("reference_number", sa.String(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_to_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        # Ad-Hoc (unlinked) counterpart to assigned_to_id above -- set
        # instead of assigned_to_id when an Admin/Manager creates or
        # reassigns a Quotation to an Ad-Hoc Individual rather than a
        # linked Staff/Customer account, exactly like AssetCheckout's own
        # user_id/outsider_id split. Always at most one of the two is set
        # -- see services/quotation_service.py's admin_create_quotation()/
        # assign_quotation().
        sa.Column("assigned_outsider_id", sa.Integer(), sa.ForeignKey("outsiders.id"), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        # --- Quote-to-Checkout workflow: approve/fulfill bookkeeping --
        # see models.py's Quotation docstring for the full lifecycle.
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfilled_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        # --- Discount (Admin/Manager-only, per-quote) -- see models.py's
        # Quotation.discount_percent docstring ---
        sa.Column("discount_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
    )
    op.create_index("ix_quotations_reference_number", "quotations", ["reference_number"], unique=True)
    op.create_index("ix_quotations_status", "quotations", ["status"])

    # Now that "quotations" exists, wire up asset_checkouts.quotation_id's
    # FK constraint (the column itself was created earlier, above, before
    # this table existed -- see that column's own comment).
    op.create_foreign_key(
        "fk_asset_checkouts_quotation_id", "asset_checkouts", "quotations", ["quotation_id"], ["id"],
    )
    op.create_index("ix_asset_checkouts_quotation_id", "asset_checkouts", ["quotation_id"])

    op.create_table(
        "quotation_items",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("quotations.id"), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("asset_types.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )

    # quotation_outsourced_items: Manager/Admin-only "not currently in
    # inventory" lines -- see models.py's QuotationOutsourcedItem
    # docstring for the full rationale.
    op.create_table(
        "quotation_outsourced_items",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("quotations.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sourced_from", sa.String(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("added_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    # Drop in reverse dependency order so foreign keys don't block the drop.
    op.drop_table("quotation_outsourced_items")
    op.drop_index("ix_asset_checkouts_quotation_id", table_name="asset_checkouts")
    op.drop_constraint("fk_asset_checkouts_quotation_id", "asset_checkouts", type_="foreignkey")
    op.drop_table("quotation_items")
    op.drop_index("ix_quotations_status", table_name="quotations")
    op.drop_index("ix_quotations_reference_number", table_name="quotations")
    op.drop_table("quotations")
    op.drop_table("app_settings")
    op.drop_table("extension_requests")
    op.drop_table("asset_checkouts")
    op.drop_table("audit_logs")
    op.drop_table("asset_exceptions")
    op.drop_table("outsiders")
    op.drop_table("users")
    op.drop_table("asset_types")
