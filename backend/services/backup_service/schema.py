"""
services/backup/schema.py
---------------------------
Pre-restore Alembic-revision detection: given a live DB connection, figure
out which migration revision the schema is actually AT by inspecting real
tables/columns, rather than trusting (or ignoring) any alembic_version row.
See _detect_schema_revision's own docstring for the restored-old-backup bug
this exists to fix.
"""

from __future__ import annotations


def _detect_schema_revision(conn) -> str:
    """
    BUG FIX -- "restore an old backup -> app comes back up looking fine,
    but the very next super_admin login throws an unhandled 500
    (`UndefinedColumn: users.totp_enabled does not exist`), and recovery
    codes don't work either because `recovery_codes` doesn't even exist
    as a table." This is the gap in the "AUTO_INIT_DB vs genuinely-older-
    backup" reconciliation below (_restore_backup_impl's caller): that
    code correctly distinguishes "no alembic_version row because this
    is a still-Alembic-tracked-just-behind backup" (upgrade head is
    right) from "no alembic_version row AND real tables already exist"
    -- but for that second case it used to ASSUME the existing tables
    always match THIS build's models.py (i.e. always safe to `stamp
    head`, recording "already current" without changing a single
    column). That assumption only holds for a database that was
    bootstrapped via AUTO_INIT_DB's create_all() against the CURRENT
    models.py. It does NOT hold for a restored backup that is itself
    simply old -- taken from a real deployment running an earlier
    version of this app, from back before Alembic was introduced into
    this project at all (so its dump never had an `alembic_version`
    table to carry forward), and before totp_enabled/recovery_codes/the
    partitioned audit_logs existed in models.py. Both situations look
    IDENTICAL to the old check ("tables exist, no alembic_version row"),
    but only one of them is actually safe to stamp straight to head --
    stamping the other just lies to Alembic about the schema being
    current, and this running code goes on to query columns/tables that
    were never actually created, exactly the corrupted-looking state a
    person restoring an old backup would see.

    Fix: don't assume -- LOOK. Every migration since the baseline
    (0003 onward) adds a column, table, or shape change that's directly
    inspectable, and they're in a single linear chain (0001 -> 0002 ->
    ... -> 0016, see each file's own `down_revision`), so walking that
    chain in order and checking for each migration's own marker finds
    exactly how far this restored schema's DDL actually got -- whether
    that's "all the way to head" (AUTO_INIT_DB-from-current-code, or a
    recent-enough old backup) or somewhere earlier (a genuinely old
    backup). The caller stamps whatever revision this returns and then
    still runs `alembic upgrade head` on top of it -- so anything this
    function finds missing gets its real DDL applied for real, instead
    of being papered over. 0002 (bootstrap root admin) has no schema
    marker of its own -- it's a guarded, idempotent DATA migration (see
    its own "already_bootstrapped" check), so re-running it against a
    database that already has a root admin row is always a safe no-op;
    it doesn't need its own detection step here.
    """
    from sqlalchemy import inspect as sa_inspect, text as sa_text

    inspector = sa_inspect(conn)

    def has_column(table: str, column: str) -> bool:
        try:
            return column in {c["name"] for c in inspector.get_columns(table)}
        except Exception:
            # Table itself doesn't exist -- column can't either.
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

    # 0007 renamed outsiders.contact_details -> outsiders.email in place --
    # presence of the new name is a reliable marker either way.
    if not has_column("outsiders", "email"):
        return revision
    revision = "0007_split_contact_details"

    if not has_column("users", "totp_enabled"):
        return revision
    revision = "0008_super_admin_totp"

    if not inspector.has_table("recovery_codes"):
        return revision
    revision = "0009_recovery_codes"

    # 0010 doesn't add a plain column -- it converts `audit_logs` itself
    # into a partitioned parent table. pg_class.relkind == 'p' is exactly
    # what distinguishes that from an ordinary ('r') table, straight from
    # Postgres's own catalog rather than guessing from SQLAlchemy's
    # generic (non-partition-aware) inspector.
    is_partitioned = conn.execute(
        sa_text("SELECT relkind = 'p' FROM pg_class WHERE relname = 'audit_logs'")
    ).scalar()
    if not is_partitioned:
        return revision
    revision = "0010_partition_audit_logs"

    # 0011 adds the password_reset_tokens table -- same "table presence
    # is its own marker" pattern as 0009_recovery_codes above. Missing
    # this check is exactly what caused a restore to fail with
    # psycopg2.errors.DuplicateTable on password_reset_tokens: a
    # database that ALREADY has the table (either a fresh AUTO_INIT_DB
    # build against current models.py, or a backup taken after 0011
    # shipped) was still detected as stuck at "0010_partition_audit_logs"
    # -- the last revision this function used to know about -- because
    # nothing here ever looked past it. The caller then stamped
    # "0010_partition_audit_logs" and ran `alembic upgrade head`, which
    # replayed 0011's `CREATE TABLE password_reset_tokens` against a
    # database where that table already existed. Checking for the table
    # here, the same way 0009 does, lets detection walk all the way to
    # head in that case instead of stopping one migration short.
    if not inspector.has_table("password_reset_tokens"):
        return revision
    revision = "0011_password_reset_tokens"

    # 0012 adds users.company. create_all()-bootstrapped databases have this
    # column even though they have no alembic_version row, so it must be part
    # of detection or reconciliation will replay the migration unnecessarily.
    if not has_column("users", "company"):
        return revision
    revision = "0012_user_company"

    # 0013 adds the quotation_notifications table.
    if not inspector.has_table("quotation_notifications"):
        return revision
    revision = "0013_quotation_notifications"

    # 0014 adds the SLA reminder timestamp to both parent tables.
    if not (
        has_column("extension_requests", "sla_last_reminded_at")
        and has_column("quotations", "sla_last_reminded_at")
    ):
        return revision
    revision = "0014_pending_approval_sla_nudges"

    # 0015 adds the asset-pool department dimension. The column is distinct
    # from users.department and from asset_types.category. Check the index as
    # well because models.py declares index=True and a create_all() schema has
    # both pieces already.
    if not has_column("asset_types", "department"):
        return revision
    asset_type_indexes = {
        idx.get("name") for idx in inspector.get_indexes("asset_types")
    }
    if "ix_asset_types_department" not in asset_type_indexes:
        return revision
    revision = "0015_asset_department"

    # 0016 adds the quotation payment fields. All four are nullable, so their
    # presence is a safe shape marker for old backups and create_all() schemas.
    if not all(
        has_column("quotations", column)
        for column in ("paid_at", "paid_by_id", "payment_method", "payment_reference")
    ):
        return revision
    revision = "0016_quotation_paid_status"

    # 0017 enforces the single-Super-Admin invariant with a partial unique
    # index. create_all()/legacy backups may already have the invariant even
    # when alembic_version is absent, so detect the physical index before
    # deciding that the schema is safe to stamp.
    index_names = {idx.get("name") for idx in inspector.get_indexes("users")}
    if "uq_users_single_super_admin" not in index_names:
        return revision
    revision = "0017_single_super_admin"

    # 0018 adds per-user credential revocation timestamps.
    if not has_column("users", "credentials_changed_at"):
        return revision
    revision = "0018_credentials_changed_at"

    return revision
