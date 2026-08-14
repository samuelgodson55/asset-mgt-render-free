#!/usr/bin/env python3
"""
Render deployment database bootstrap/migration.

Render's free plan does not provide a pre-deploy command, so the Docker
service runs this script before uvicorn. It deliberately keeps the existing
Render demo behavior (AUTO_INIT_DB/AUTO_SEED_DEMO_DATA) while making Alembic
the recorded schema version on every deployment.

For an existing database created by SQLAlchemy create_all() before Alembic was
introduced, the script detects the actual schema revision, stamps that
revision, and then upgrades to head. It never blindly stamps head.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool


BACKEND_DIR = Path("/app")
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _detect_schema_revision(conn) -> str:
    """Detect the furthest schema revision represented by the live DDL."""
    inspector = inspect(conn)

    def has_column(table: str, column: str) -> bool:
        try:
            return column in {c["name"] for c in inspector.get_columns(table)}
        except Exception:
            return False

    revision = "0001_baseline_schema"

    if not has_column("outsiders", "is_deleted"):
        return revision
    revision = "0003_outsider_soft_delete"

    if not has_column("outsiders", "converted_to_user_id"):
        return revision
    revision = "0004_outsider_convert_to_user"

    if not has_column("users", "converted_to_outsider_id"):
        return revision
    revision = "0005_user_convert_to_outsider"

    if not (has_column("users", "purged_at") and has_column("asset_types", "purged_at")):
        return revision
    revision = "0006_purge_deleted"

    if not has_column("outsiders", "email"):
        return revision
    revision = "0007_split_contact_details"

    if not has_column("users", "totp_enabled"):
        return revision
    revision = "0008_super_admin_totp"

    if not inspector.has_table("recovery_codes"):
        return revision
    revision = "0009_recovery_codes"

    is_partitioned = conn.execute(
        text(
            "SELECT relkind = 'p' FROM pg_class "
            "WHERE relname = 'audit_logs'"
        )
    ).scalar()
    if not is_partitioned:
        return revision
    revision = "0010_partition_audit_logs"

    if not inspector.has_table("password_reset_tokens"):
        return revision
    revision = "0011_password_reset_tokens"

    if not has_column("users", "company"):
        return revision
    revision = "0012_user_company"

    if not inspector.has_table("quotation_notifications"):
        return revision
    revision = "0013_quotation_notifications"

    if not (
        has_column("extension_requests", "sla_last_reminded_at")
        and has_column("quotations", "sla_last_reminded_at")
    ):
        return revision
    revision = "0014_pending_approval_sla_nudges"

    if not has_column("asset_types", "department"):
        return revision

    indexes = {idx.get("name") for idx in inspector.get_indexes("asset_types")}
    if "ix_asset_types_department" not in indexes:
        return revision
    revision = "0015_asset_department"

    if not all(
        has_column("quotations", column)
        for column in ("paid_at", "paid_by_id", "payment_method", "payment_reference")
    ):
        return revision
    return "0016_quotation_paid_status"


def _alembic_config() -> AlembicConfig:
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def _prepare_demo_database(database_url: str) -> None:
    """Preserve the existing Render demo/bootstrap behavior before stamping."""
    if not (_env_bool("AUTO_INIT_DB", True) or _env_bool("AUTO_SEED_DEMO_DATA", True)):
        return

    # These imports intentionally happen only after the database URL is known.
    sys.path.insert(0, str(BACKEND_DIR))
    from database import init_db, seed_db

    if _env_bool("AUTO_INIT_DB", True):
        print("render-migrate: AUTO_INIT_DB=true -- ensuring tables exist.", flush=True)
        init_db()

    if _env_bool("AUTO_SEED_DEMO_DATA", True):
        print("render-migrate: AUTO_SEED_DEMO_DATA=true -- ensuring demo data exists.", flush=True)
        seed_db()


def _migrate(database_url: str) -> None:
    engine = create_engine(database_url, poolclass=NullPool, connect_args={"connect_timeout": 10})

    # Render can bring the web container up slightly before the managed
    # database accepts connections. Retry here instead of letting a transient
    # startup race fail an otherwise valid deployment.
    last_error: Exception | None = None
    for attempt in range(1, 31):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except SQLAlchemyError as exc:
            last_error = exc
            print(
                f"render-migrate: database not ready (attempt {attempt}/30); retrying...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(2)
    else:
        raise RuntimeError(f"Database did not become reachable: {last_error}") from last_error

    # Preserve the current Render free-plan behavior first. On a brand-new
    # database this creates the schema and demo rows; on an existing database
    # create_all()/seed_db() are harmless no-ops.
    _prepare_demo_database(database_url)

    with engine.connect() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names()) - {"alembic_version"}

        current_revisions = []
        if inspector.has_table("alembic_version"):
            current_revisions = [
                row[0]
                for row in conn.execute(text("SELECT version_num FROM alembic_version"))
                if row[0]
            ]

    cfg = _alembic_config()

    if not tables:
        print("render-migrate: database is empty -- running 'alembic upgrade head'.", flush=True)
        command.upgrade(cfg, "head")
    elif current_revisions:
        print(
            f"render-migrate: Alembic revision(s) {current_revisions} found -- "
            "running 'alembic upgrade head'.",
            flush=True,
        )
        command.upgrade(cfg, "head")
    else:
        # Existing Render databases created by AUTO_INIT_DB/create_all() have
        # real tables but no alembic_version row. Detect their actual shape,
        # stamp exactly that revision, then apply anything still missing.
        with engine.connect() as conn:
            detected = _detect_schema_revision(conn)

        print(
            f"render-migrate: existing schema has no Alembic revision; "
            f"detected {detected}, stamping it, then upgrading to head.",
            flush=True,
        )
        command.stamp(cfg, detected)
        command.upgrade(cfg, "head")

    with engine.connect() as conn:
        final_revisions = [
            row[0]
            for row in conn.execute(text("SELECT version_num FROM alembic_version"))
            if row[0]
        ]
    print(f"render-migrate: migration complete; current revision(s): {final_revisions}", flush=True)
    engine.dispose()


if __name__ == "__main__":
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("render-migrate: DATABASE_URL is required.")

    _migrate(database_url)
