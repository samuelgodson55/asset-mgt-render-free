"""Tests for db_pool_metrics.py and GET /api/diagnostics/db-pool.

Kept deliberately light on real PgBouncer/Postgres interaction (the test
suite runs against SQLite -- see tests/conftest.py's db_engine fixture),
so these focus on: (1) the snapshot functions never raise and degrade
gracefully when there's nothing to probe, (2) register_gauges() is safe
to call repeatedly and truly no-ops without the OTel SDK, and (3) the
admin-only diagnostics endpoint is gated correctly and returns the
expected shape.
"""

import inspect

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


class _FakeResult:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar(self):
        return self._value

    def all(self):
        return self._rows


class _FakeConn:
    """Stands in for a real psycopg2/SQLAlchemy connection so the
    limited-visibility branch of postgres_activity_snapshot() can be
    exercised without a real Postgres server (this suite otherwise runs
    against SQLite -- see this file's module docstring)."""

    def __init__(self, full_visibility: bool, rows):
        self._full_visibility = full_visibility
        self._rows = rows
        self._n = 0

    def execute(self, stmt, *args, **kwargs):
        sql = str(stmt)
        self._n += 1
        if "max_connections" in sql:
            return _FakeResult(value=100)
        if "rolsuper" in sql:
            return _FakeResult(value=self._full_visibility)
        return _FakeResult(rows=self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn

    def dispose(self):
        pass


def test_postgres_activity_snapshot_flags_limited_visibility(monkeypatch):
    """Without superuser/pg_read_all_stats (the default on Render, Azure
    Database for PostgreSQL, RDS, Cloud SQL, ...), pg_stat_activity nulls
    out `state` for every session but the probe's own -- see
    _pg_stat_activity_full_visibility()'s docstring. The snapshot must
    surface that as `limited_visibility: true` rather than silently
    reporting a healthy-looking active=0/idle_in_transaction=0.
    """
    monkeypatch.setattr(db_pool_metrics, "DIRECT_DATABASE_URL", "postgresql://u:p@h:5432/d")
    fake_conn = _FakeConn(full_visibility=False, rows=[("active", 1), (None, 4)])
    monkeypatch.setattr(db_pool_metrics, "create_engine", lambda *a, **k: _FakeEngine(fake_conn))

    snap = db_pool_metrics.postgres_activity_snapshot()
    assert snap is not None
    assert snap["limited_visibility"] is True
    # The probe's own session (real "active") is still visible; the other
    # 4 sessions collapse into the NULL/"unknown" bucket and are NOT
    # miscounted as active/idle/idle_in_transaction=0-implies-healthy.
    assert snap["active"] == 1
    assert snap["total_connections"] == 5


def test_postgres_activity_snapshot_full_visibility_reports_real_breakdown(monkeypatch):
    monkeypatch.setattr(db_pool_metrics, "DIRECT_DATABASE_URL", "postgresql://u:p@h:5432/d")
    fake_conn = _FakeConn(
        full_visibility=True,
        rows=[("active", 2), ("idle", 3), ("idle in transaction", 1)],
    )
    monkeypatch.setattr(db_pool_metrics, "create_engine", lambda *a, **k: _FakeEngine(fake_conn))

    snap = db_pool_metrics.postgres_activity_snapshot()
    assert snap is not None
    assert snap["limited_visibility"] is False
    assert snap["active"] == 2
    assert snap["idle"] == 3
    assert snap["idle_in_transaction"] == 1
    assert snap["total_connections"] == 6


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
