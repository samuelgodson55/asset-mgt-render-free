"""
services/backup/reconcile_audit.py
-------------------------------------
Post-restore audit-log reconciliation: a short-lived, connection-pool-free
"snapshot" engine used to read audit_logs immediately before the
destructive restore, plus the comparison that re-applies any audit rows
written after the backup was taken but lost by restoring it.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger(__name__)


def _create_restore_snapshot_engine():
    """Create a short-lived, unpooled engine for restore preflight snapshots.

    The restore endpoint is authenticated through ``get_db()``, so the
    request can already be holding the application's ONLY SQLAlchemy pool
    connection while this function runs. With a small production pool (the
    live dashboard showed ``pool_size=1``), opening another connection from
    ``database.engine`` can block forever waiting for the very request that is
    trying to create the snapshot.

    Restore preflight is intentionally independent of the request pool: it
    uses the authoritative DIRECT_DATABASE_URL with NullPool, just like the
    existing pg_dump/psql maintenance path. This connection is read-only in
    practice and is disposed before the destructive schema reset begins.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    url = make_url(settings.DIRECT_DATABASE_URL or settings.DATABASE_URL)
    kwargs = {"poolclass": NullPool, "pool_pre_ping": True}
    if url.get_backend_name().startswith("postgresql"):
        kwargs["connect_args"] = {"connect_timeout": 5}
    return create_engine(url, **kwargs)


def _snapshot_audit_logs(conn) -> list[dict]:
    """Capture the live audit ledger before the destructive restore.

    AuditLog is append-only and therefore unlike users/checkouts/quotations
    should never lose post-backup history merely because the database was
    rolled back to an older snapshot. The restore merge below compares exact
    immutable row content against the restored ledger and re-inserts only
    rows that are missing, letting Postgres assign fresh ids safely.

    ``conn`` is deliberately supplied by the dedicated restore-snapshot
    engine, never the application's request pool. A genuinely brand-new
    database may not have an audit table yet; in that case there is no audit
    history to preserve and the later Alembic reconciliation will create it.
    Any other snapshot failure is fatal: silently proceeding could erase
    audit history.
    """
    from sqlalchemy import inspect as sa_inspect, text as sa_text

    try:
        if not sa_inspect(conn).has_table("audit_logs"):
            logger.info("backup_service: no existing 'audit_logs' table to snapshot before restore.")
            return []
        return [
            dict(row)
            for row in conn.execute(
                sa_text(
                    'SELECT operator, action, target_type, target_id, details, "timestamp" '
                    'FROM audit_logs ORDER BY "timestamp", id'
                )
            ).mappings().all()
        ]
    except Exception as exc:
        logger.exception("backup_service: could not snapshot audit logs before restore")
        raise RuntimeError(
            "Restore aborted because the current audit ledger could not be snapshotted. "
            "The restore will not proceed when doing so could erase audit history. "
            f"Snapshot error: {exc}"
        ) from exc


def _reconcile_post_restore_audit_logs(engine, pre_restore_audit_logs: list[dict]) -> dict:
    """Preserve the immutable audit history that existed before restore.

    Restore replaces the whole schema with the selected backup. That is valid
    for mutable business state, but it must not erase audit history created
    after the backup was taken. Existing backup rows are retained as-is; any
    pre-restore row whose immutable content is absent from the restored
    ledger is inserted after restore. The audit id is intentionally NOT
    restored because ids are internal bookkeeping fields and the restored
    database may already contain the same id for a different row.
    """
    if not pre_restore_audit_logs:
        return {"audit_logs_preserved": 0, "audit_logs_skipped": 0}

    from sqlalchemy import text as sa_text

    # A row's identity for this merge is its immutable business content. The
    # timestamp is included because the same operator/action/detail can be a
    # legitimate repeated action at different times.
    def fingerprint(row: dict) -> tuple:
        return (
            row.get("operator"),
            row.get("action"),
            row.get("target_type"),
            row.get("target_id"),
            row.get("details"),
            row.get("timestamp"),
        )

    preserved = 0
    skipped = 0
    with engine.begin() as conn:
        restored = {
            fingerprint(dict(row))
            for row in conn.execute(
                sa_text(
                    'SELECT operator, action, target_type, target_id, details, "timestamp" '
                    'FROM audit_logs'
                )
            ).mappings().all()
        }

        for row in pre_restore_audit_logs:
            fp = fingerprint(row)
            if fp in restored:
                continue
            try:
                conn.execute(
                    sa_text(
                        'INSERT INTO audit_logs '
                        '(operator, action, target_type, target_id, details, "timestamp") '
                        'VALUES (:operator, :action, :target_type, :target_id, :details, :timestamp)'
                    ),
                    {
                        "operator": row.get("operator"),
                        "action": row.get("action"),
                        "target_type": row.get("target_type"),
                        "target_id": row.get("target_id"),
                        "details": row.get("details"),
                        "timestamp": row.get("timestamp"),
                    },
                )
                restored.add(fp)
                preserved += 1
            except Exception:
                logger.exception(
                    "backup_service: failed to preserve one pre-restore audit row "
                    "(%s, %s, %s, %s)",
                    row.get("operator"), row.get("action"), row.get("target_type"), row.get("target_id"),
                )
                skipped += 1

        # The sequence may have existed in the restored backup at a lower
        # value than the rows we just inserted. Make the next generated id
        # strictly greater than the current maximum so the next business
        # action can always append another audit row.
        conn.execute(
            sa_text(
                "SELECT setval('audit_logs_id_seq', "
                "COALESCE((SELECT MAX(id) FROM audit_logs), 1), "
                "(SELECT MAX(id) FROM audit_logs) IS NOT NULL)"
            )
        )

    return {"audit_logs_preserved": preserved, "audit_logs_skipped": skipped}
