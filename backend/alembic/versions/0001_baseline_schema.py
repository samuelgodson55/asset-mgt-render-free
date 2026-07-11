"""baseline schema

Revision ID: 0001_baseline_schema
Revises:
Create Date: 2026-07-08

This is the ONLY migration in the project: it creates every table exactly
as defined in models.py, in its final, current shape -- there is no
0002/0003/... chain to apply on top of it. A fresh install (or a fresh
`docker compose up --build` against an empty Postgres volume) only ever
needs `alembic upgrade head` to run this one file.

WHY A SINGLE SQUASHED BASELINE INSTEAD OF INCREMENTAL MIGRATIONS?
This project previously had a short-lived chain of incremental migrations
(account lockout fields, timezone-aware datetimes, username backfill,
asset_types soft delete) written as models.py evolved. That's the normal/
correct way to evolve a schema that already has real data in it -- but for
a project that hasn't shipped real production data yet, carrying that
history forward mostly adds friction (five files to read instead of one,
more surface area for the exact "migration file referenced but missing"
bug that motivated squashing them). So this file was rewritten to
directly match models.py's CURRENT shape, and the incremental files were
deleted.

IF YOU HAVE AN EXISTING DATABASE that was already migrated with the old
0001-0005 chain (i.e. `alembic_version` contains anything other than
`0001_baseline_schema`), do NOT just run `alembic upgrade head` -- Alembic
will look for a migration named `0002_...`/etc. that no longer exists and
error out. Instead, since your tables already match this file's schema
exactly, just re-point Alembic's own bookkeeping at the new single
revision without touching any table:
    alembic stamp 0001_baseline_schema
Fresh installs (`asset_db` doesn't exist yet, or is fully empty) can
ignore this and just run `alembic upgrade head` as normal.

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
        # --- Originating department (optional) -- see models.py's AssetType docstring ---
        sa.Column("department", sa.String(), nullable=True),
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
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("asset_types.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("outsider_id", sa.Integer(), sa.ForeignKey("outsiders.id"), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("quantity_returned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkout_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), server_default="active"),
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


def downgrade() -> None:
    # Drop in reverse dependency order so foreign keys don't block the drop.
    op.drop_table("extension_requests")
    op.drop_table("asset_checkouts")
    op.drop_table("audit_logs")
    op.drop_table("asset_exceptions")
    op.drop_table("outsiders")
    op.drop_table("users")
    op.drop_table("asset_types")
