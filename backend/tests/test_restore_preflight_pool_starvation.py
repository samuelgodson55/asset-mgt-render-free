"""Regression tests for restore preflight using a dedicated DB connection.

The restore endpoint authenticates through the normal SQLAlchemy request pool.
When that pool has one slot, the authenticated request can already own that
slot while restore preflight tries to snapshot users/audit history. Reusing the
same pool can therefore wait on itself. The restore preflight engine must be
unpooled and independent of the request pool.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

import services.backup_service as backup_service


def test_restore_snapshot_engine_is_independent_of_a_one_slot_request_pool(tmp_path, monkeypatch):
    db_path = tmp_path / "restore-preflight.db"
    url = f"sqlite:///{db_path}"

    # Simulate the production shape: one normal request-pool slot is already
    # checked out by the authenticated restore request.
    request_engine = create_engine(url, pool_size=1, max_overflow=0)
    held = request_engine.connect()
    try:
        held.execute(text("CREATE TABLE audit_logs (operator TEXT, action TEXT, target_type TEXT, target_id INTEGER, details TEXT, timestamp TEXT, id INTEGER)"))
        held.commit()

        monkeypatch.setattr(backup_service.settings, "DIRECT_DATABASE_URL", url)
        monkeypatch.setattr(backup_service.settings, "DATABASE_URL", url)

        snapshot_engine = backup_service._create_restore_snapshot_engine()
        try:
            assert isinstance(snapshot_engine.pool, NullPool)
            with snapshot_engine.connect() as snapshot_conn:
                snapshot_conn.execute(text("INSERT INTO audit_logs VALUES ('root', 'TEST', 'Test', 1, 'ok', '2026-08-20T20:00:00Z', 1)"))
                snapshot_conn.commit()
                rows = backup_service._snapshot_audit_logs(snapshot_conn)
                assert len(rows) == 1
                assert rows[0]["operator"] == "root"
        finally:
            snapshot_engine.dispose()
    finally:
        held.close()
        request_engine.dispose()


def test_missing_audit_table_is_an_empty_snapshot(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setattr(backup_service.settings, "DIRECT_DATABASE_URL", url)
    monkeypatch.setattr(backup_service.settings, "DATABASE_URL", url)

    snapshot_engine = backup_service._create_restore_snapshot_engine()
    try:
        with snapshot_engine.connect() as conn:
            assert backup_service._snapshot_audit_logs(conn) == []
    finally:
        snapshot_engine.dispose()
