"""Tests for db_pool_metrics.py and GET /api/diagnostics/db-pool.

Kept deliberately light on real PgBouncer/Postgres interaction (the test
suite runs against SQLite -- see tests/conftest.py's db_engine fixture),
so these focus on: (1) the snapshot functions never raise and degrade
gracefully when there's nothing to probe, (2) register_gauges() is safe
to call repeatedly and truly no-ops without the OTel SDK, and (3) the
admin-only diagnostics endpoint is gated correctly and returns the
expected shape.
"""

import db_pool_metrics


def test_sqlalchemy_pool_snapshot_never_raises_against_the_test_engine():
    snap = db_pool_metrics.sqlalchemy_pool_snapshot()
    assert isinstance(snap, dict)


def test_pgbouncer_pool_snapshot_is_none_when_pgbouncer_is_disabled(monkeypatch):
    monkeypatch.setattr(db_pool_metrics.settings, "USE_PGBOUNCER", False)
    assert db_pool_metrics.pgbouncer_pool_snapshot() is None



def test_pgbouncer_probe_uses_psycopg2_simple_protocol(monkeypatch):
    """The PgBouncer admin console requires the simple query protocol; the
    telemetry probe must not route SHOW commands through SQLAlchemy's extended
    protocol path. This is a structural regression guard for the live probe.
    """
    assert "psycopg2.connect" in inspect.getsource(db_pool_metrics.pgbouncer_pool_snapshot)

def test_postgres_activity_snapshot_is_none_against_sqlite():
    # tests/conftest.py's db_engine fixture points DIRECT_DATABASE_URL at
    # a SQLite file, not Postgres -- the ground-truth probe must recognize
    # that and back off cleanly rather than raising.
    assert db_pool_metrics.postgres_activity_snapshot() is None


def test_register_gauges_is_idempotent_and_never_raises():
    db_pool_metrics.reset_for_tests()
    db_pool_metrics.register_gauges()
    db_pool_metrics.register_gauges()  # second call must be a safe no-op


def test_snapshot_all_returns_the_expected_top_level_shape():
    snap = db_pool_metrics.snapshot_all()
    assert set(snap.keys()) == {
        "database_route",
        "sqlalchemy_pool",
        "pgbouncer_pool",
        "postgres_activity",
        "configured",
    }
    assert "pgbouncer_safety_margin_percent" in snap["configured"]
    assert "db_background_connection_reserve" in snap["configured"]


def test_diagnostics_endpoint_requires_true_super_admin(as_manager):
    client, headers = as_manager
    resp = client.get("/api/diagnostics/db-pool", headers=headers)
    assert resp.status_code == 403


def test_diagnostics_endpoint_rejects_unauthenticated_requests(client):
    resp = client.get("/api/diagnostics/db-pool")
    assert resp.status_code in (401, 403)


def test_diagnostics_endpoint_returns_snapshot_for_super_admin(as_super_admin):
    client, headers = as_super_admin
    resp = client.get("/api/diagnostics/db-pool", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "database_route",
        "sqlalchemy_pool",
        "pgbouncer_pool",
        "postgres_activity",
        "configured",
    }
