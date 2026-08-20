"""
Regression tests for the PgBouncer pool-exhaustion bug found in review:

Adaptive pool sizing (_compute_pool_sizing()) used to derive
pool_size/max_overflow -- and therefore DBConcurrencyMiddleware's admission
limit, sized directly from them -- purely from raw Postgres's own
max_connections, with no awareness that PgBouncer (when USE_PGBOUNCER=true)
sits in front with its own, much smaller, fixed server-side pool. That let
the app admit more concurrent DB-touching requests than PgBouncer could
actually service, so the excess queued *inside* PgBouncer (up to its own
query_wait_timeout) instead of failing fast -- a slow hang instead of the
clean 503 DBConcurrencyMiddleware is meant to produce.

These tests exercise _compute_pool_sizing() directly with a mocked Postgres
probe (real Postgres isn't available in this test environment -- see
conftest.py, which points DATABASE_URL at SQLite) so they can assert the
PgBouncer cap applies regardless of what max_connections reports.
"""

import database
from config import settings


def test_pool_sizing_is_capped_by_pgbouncer_pool_when_enabled(monkeypatch):
    """A generous Postgres max_connections must not override PgBouncer's
    own, much smaller, server-side pool once USE_PGBOUNCER is on -- this is
    the exact scenario from the review (default_pool_size=5,
    reserve_pool_size=2 => 7 total, vs. a Postgres budget of 100)."""
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 1)
    monkeypatch.setattr(settings, "DB_CONNECTION_SAFETY_MARGIN", 0)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_DEFAULT_POOL_SIZE", 5)
    monkeypatch.setattr(settings, "PGBOUNCER_RESERVE_POOL_SIZE", 2)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)
    monkeypatch.setattr(
        database, "_probe_postgres_connection_budget", lambda url: (100, 3)
    )

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    # Total budget must be capped to PgBouncer's 7 server connections, not
    # anywhere near the 97 the raw Postgres probe would otherwise allow.
    assert pool_size + max_overflow <= 7



def test_embedded_celery_worker_does_not_double_reserve_api_pool(monkeypatch):
    """ACA's embedded Celery process uses the separate background DB reserve;
    it must not also consume a full API pool share. With an 8-connection
    PgBouncer budget, 10% headroom and one background slot, three API
    replicas should get two pooled connections each instead of one.
    """
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", None)
    monkeypatch.setattr(settings, "BACKEND_MAX_REPLICAS", 3)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 8)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 10)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONNECTION_RESERVE", 1)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONCURRENCY_LIMIT", 1)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setenv("RUN_EMBEDDED_WORKER", "true")

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert (pool_size, max_overflow) == (2, 0)
    assert (pool_size + max_overflow) * 3 + 1 <= 8

def test_small_process_share_stays_in_persistent_pool(monkeypatch):
    """When the global budget gives each API process two connections, both
    should be persistent pool slots rather than 1 pooled + 1 overflow.
    PgBouncer already handles server-side transaction reuse, so this avoids
    unnecessary SQLAlchemy connection churn during normal bursts.
    """
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 3)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 10)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 10)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONNECTION_RESERVE", 1)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONCURRENCY_LIMIT", 1)
    monkeypatch.setattr(database, "_probe_postgres_connection_budget", lambda url: None)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert (pool_size, max_overflow) == (2, 0)


def test_pool_sizing_ignores_pgbouncer_pool_when_disabled(monkeypatch):
    """Direct-to-Postgres (USE_PGBOUNCER=false, e.g. render.yaml's Free-plan
    break-glass path) must keep sizing off the real Postgres budget --
    PgBouncer's settings are irrelevant when nothing routes through it."""
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 1)
    monkeypatch.setattr(settings, "DB_CONNECTION_SAFETY_MARGIN", 0)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", False)
    monkeypatch.setattr(settings, "PGBOUNCER_DEFAULT_POOL_SIZE", 5)
    monkeypatch.setattr(settings, "PGBOUNCER_RESERVE_POOL_SIZE", 2)
    monkeypatch.setattr(
        database, "_probe_postgres_connection_budget", lambda url: (100, 3)
    )

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    # Not capped to PgBouncer's 7 -- the per-process cap (20) is the only
    # other ceiling in play here.
    assert pool_size + max_overflow > 7


def test_db_concurrency_middleware_limit_derives_from_capped_pool_size(monkeypatch):
    """DBConcurrencyMiddleware's semaphore limit is sized directly from
    database.POOL_SIZE + database.MAX_OVERFLOW (see its own docstring) --
    confirm that still holds, so a fix to _compute_pool_sizing() alone (as
    above) is sufficient to fix the middleware's admission limit too,
    without needing a second, separate change there."""
    from middleware.db_concurrency import DBConcurrencyMiddleware

    monkeypatch.setattr(database, "POOL_SIZE", 4)
    monkeypatch.setattr(database, "MAX_OVERFLOW", 3)
    monkeypatch.setattr(settings, "DB_REQUEST_CONCURRENCY_LIMIT", None)

    mw = DBConcurrencyMiddleware(app=None)

    assert mw.limit == 7


def test_pool_sizing_never_exceeds_pgbouncer_budget_across_processes(monkeypatch):
    """The minimum per-process floor must not recreate the old over-budget bug."""
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 7)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 11)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert (pool_size + max_overflow) * 7 <= 11
    assert pool_size >= 1


def test_pgbouncer_probe_failure_does_not_expand_pool(monkeypatch):
    """A failed Postgres probe must not fall back to 3+2 per process."""
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 6)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 7)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)
    monkeypatch.setattr(database, "_probe_postgres_connection_budget", lambda url: None)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert (pool_size + max_overflow) * 6 <= 7


def test_explicit_pool_override_is_capped_when_pgbouncer_enabled(monkeypatch):
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 10)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 10)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 4)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 7)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert pool_size + max_overflow <= 1


def test_background_reserve_is_removed_from_api_pgbouncer_budget(monkeypatch):
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 1)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 7)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONNECTION_RESERVE", 1)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert pool_size + max_overflow <= 6


def test_background_reserve_keeps_global_budget_safe_across_vm_processes(monkeypatch):
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 7)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 11)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONNECTION_RESERVE", 1)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    # API pools + the one reserved background slot stay within PgBouncer.
    assert (pool_size + max_overflow) * 7 + 1 <= 11


def test_pgbouncer_budget_gets_live_postgres_cap(monkeypatch):
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 50)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)
    monkeypatch.setattr(settings, "DB_CONNECTION_SAFETY_MARGIN", 5)
    monkeypatch.setattr(database, "_probe_postgres_connection_budget", lambda url: (30, 3))

    assert database._pgbouncer_server_pool_budget() == 22


def test_pgbouncer_safety_margin_is_applied_before_background_reserve(monkeypatch):
    monkeypatch.setattr(settings, "DB_POOL_SIZE", None)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", None)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 1)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 10)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 20)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONNECTION_RESERVE", 1)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONCURRENCY_LIMIT", 1)
    monkeypatch.setattr(database, "_probe_postgres_connection_budget", lambda url: None)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")
    assert pool_size + max_overflow <= 7


def test_explicit_pool_override_is_honored_exactly_when_it_fits(monkeypatch):
    """BUG FIX: when an operator sets BOTH DB_POOL_SIZE and DB_MAX_OVERFLOW
    and the requested total fits comfortably within budget, database.py must
    use those EXACT numbers (config.py: 'set both to force a fixed,
    non-adaptive size instead') -- not silently re-derive a different
    pool_size/max_overflow split (e.g. via the 50/50 _split_process_budget
    heuristic) that happens to add up to the same total."""
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 8)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 0)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 1)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", True)
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 50)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONNECTION_RESERVE", 0)
    monkeypatch.setattr(settings, "DB_BACKGROUND_CONCURRENCY_LIMIT", 0)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert (pool_size, max_overflow) == (8, 0)


def test_explicit_pool_override_applies_on_direct_postgres_path(monkeypatch):
    """BUG FIX: the DB_POOL_SIZE/DB_MAX_OVERFLOW escape hatch must also apply
    when USE_PGBOUNCER=false -- config.py never scopes it to PgBouncer-only,
    and explicitly calls out 'a database that can't be probed' as a reason
    to use it. It must not be silently ignored on the direct-Postgres path."""
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 8)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 0)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 1)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", False)
    monkeypatch.setattr(settings, "DB_CONNECTION_SAFETY_MARGIN", 0)
    monkeypatch.setattr(
        database, "_probe_postgres_connection_budget", lambda url: (100, 3)
    )

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert (pool_size, max_overflow) == (8, 0)


def test_explicit_pool_override_applies_when_probe_fails(monkeypatch):
    """BUG FIX: same escape hatch, exercised via the probe-failure fallback
    path specifically -- config.py calls this scenario out by name."""
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 2)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 1)
    monkeypatch.setattr(settings, "DB_EXPECTED_PROCESSES", 1)
    monkeypatch.setattr(settings, "USE_PGBOUNCER", False)
    monkeypatch.setattr(database, "_probe_postgres_connection_budget", lambda url: None)

    pool_size, max_overflow = database._compute_pool_sizing("postgresql://x/y")

    assert (pool_size, max_overflow) == (2, 1)


def test_zero_pgbouncer_pool_size_is_treated_as_unset(monkeypatch):
    monkeypatch.setattr(settings, "PGBOUNCER_SERVER_POOL_SIZE", 0)
    monkeypatch.setattr(settings, "PGBOUNCER_DEFAULT_POOL_SIZE", 5)
    monkeypatch.setattr(settings, "PGBOUNCER_RESERVE_POOL_SIZE", 2)
    monkeypatch.setattr(settings, "PGBOUNCER_SAFETY_MARGIN_PERCENT", 0)
    monkeypatch.setattr(database, "_probe_postgres_connection_budget", lambda url: None)
    assert database._pgbouncer_server_pool_budget() == 7
