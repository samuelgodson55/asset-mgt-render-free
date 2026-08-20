"""
db_pool_metrics.py
-------------------
Real, sampled numbers for the two knobs database.py's adaptive pool
sizing (_compute_pool_sizing()) currently asks an operator to set from
intuition rather than evidence:

  - PGBOUNCER_SAFETY_MARGIN_PERCENT (config.py) -- how much of the
    PgBouncer server pool to deliberately leave unused as headroom.
  - DB_BACKGROUND_CONNECTION_RESERVE (config.py) -- how many of those
    connections to set aside for Celery/Beat background work rather
    than the request-handling API pool.

Both are currently picked once and left alone because there's been no
easy way to see whether they're too generous (wasted capacity) or too
thin (PgBouncer clients queueing, Postgres connections maxed out) under
real traffic. This module provides three independent, best-effort
snapshots so an operator can actually look:

  1. sqlalchemy_pool_snapshot() -- THIS process's own SQLAlchemy pool
     state (checked_out/checked_in/overflow/size). Free, in-memory, no
     network call. Shows whether the API's *share* of the PgBouncer
     budget (api_budget / total_processes, see database.py) is actually
     being saturated.

  2. pgbouncer_pool_snapshot() -- PgBouncer's own SHOW POOLS/SHOW STATS
     view (client connections waiting, server connections active/idle,
     average time a client spends waiting for a server connection). The
     PgBouncer admin database only supports the PostgreSQL simple query
     protocol, so this probe uses a short-lived psycopg2/libpq connection
     rather than SQLAlchemy's normal extended-protocol path.
     `cl_waiting` > 0 and a rising `avg_wait_time_us` are the clearest
     "the server pool itself is undersized" signal -- direct evidence
     for whether the safety margin can be tightened. Requires
     USE_PGBOUNCER; returns None otherwise.

  3. postgres_activity_snapshot() -- ground truth from Postgres's own
     pg_stat_activity, independent of PgBouncer's accounting. Catches
     anything eating into the server's connection budget that PgBouncer
     doesn't know about (the `migrate` job, a stray psql session, a
     direct-connection break-glass) -- the real denominator
     DB_CONNECTION_SAFETY_MARGIN needs to be sized against, not the
     PgBouncer-only view.

Two ways these numbers reach an operator:

  - register_gauges() wires all three up as OpenTelemetry
    ObservableGauges, sampled on whatever interval telemetry.py's
    PeriodicExportingMetricReader was configured with, and exported
    wherever traces already go (console/OTLP/Application Insights) when
    OTEL_ENABLED=true -- a trend an operator can actually chart over
    time under real load, not just a point-in-time reading.
  - snapshot_all() is the same three snapshots taken on demand, served
    by GET /api/diagnostics/db-pool (api/diagnostics_api.py) -- works
    with no APM backend configured at all, for a quick "what's happening
    right now" check.

None of this ever holds a connection open outside the sampling window
itself -- same short-lived NullPool + tight connect_timeout pattern
database.py's own _probe_postgres_connection_budget() uses, deliberately
duplicated here rather than imported so this module stays independent
and can never perturb pool-sizing-at-startup behavior.
"""

import logging
import threading

import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from config import settings
from database import DIRECT_DATABASE_URL, engine as _app_engine

logger = logging.getLogger(__name__)

_PROBE_CONNECT_TIMEOUT_SECONDS = 3

_gauges_lock = threading.Lock()
_gauges_registered = False


def sqlalchemy_pool_snapshot() -> dict:
    """This process's own SQLAlchemy pool state. No network call --
    these read the pool's own internal counters. Fields present depend
    on the pool implementation actually in use (e.g. NullPool, used in
    tests and for the `migrate` job, doesn't track checked_in/overflow
    the same way a QueuePool does) -- missing a field here just means
    "not applicable to this pool type", not a probe failure.
    """
    pool = _app_engine.pool
    snapshot = {}
    for field, method in (
        ("pool_size", "size"),
        ("checked_out", "checkedout"),
        ("checked_in", "checkedin"),
        ("overflow", "overflow"),
    ):
        fn = getattr(pool, method, None)
        if not callable(fn):
            continue
        try:
            snapshot[field] = fn()
        except NotImplementedError:
            # NullPool and a few other pool classes intentionally don't
            # implement every gauge method -- expected, not an error.
            continue
        except Exception:
            logger.debug("db_pool_metrics: SQLAlchemy pool field %s failed", field, exc_info=True)
    return snapshot


def _database_route_snapshot() -> dict:
    """Describe the endpoint the running SQLAlchemy engine is actually using.

    This is intentionally derived from the resolved runtime DATABASE_URL, not
    merely from USE_PGBOUNCER. It lets the admin dashboard distinguish
    "configured" from "the API is actually pointed at the pooler".
    Passwords and query secrets are never returned.
    """
    try:
        url = make_url(settings.DATABASE_URL)
        host = url.host
        port = url.port or (6432 if settings.USE_PGBOUNCER else 5432)
        expected_host = settings.PGBOUNCER_HOST or make_url(settings.DIRECT_DATABASE_URL or settings.DATABASE_URL).host
        expected_port = settings.PGBOUNCER_PORT
        route_in_use = bool(settings.USE_PGBOUNCER and host and port == expected_port and (not settings.PGBOUNCER_HOST or host == settings.PGBOUNCER_HOST))
        return {
            "configured": bool(settings.USE_PGBOUNCER),
            "in_use": route_in_use,
            "host": host,
            "port": port,
            "expected_pooler_host": expected_host if settings.USE_PGBOUNCER else None,
            "expected_pooler_port": expected_port if settings.USE_PGBOUNCER else None,
        }
    except Exception:
        return {"configured": bool(settings.USE_PGBOUNCER), "in_use": False, "host": None, "port": None, "expected_pooler_host": None, "expected_pooler_port": None}


def pgbouncer_pool_snapshot() -> dict | None:
    """Best-effort PgBouncer admin-console snapshot.

    PgBouncer's special ``pgbouncer`` database only supports PostgreSQL's
    *simple query protocol*. SQLAlchemy/psycopg2 can otherwise use the
    extended protocol, which is why the old SQLAlchemy ``SHOW POOLS`` probe
    could report "unavailable" even though the application route itself was
    healthy. Use a short-lived psycopg2/libpq connection and execute the SHOW
    commands without parameters so libpq uses the simple protocol.

    This is telemetry only: failure returns None and never affects request
    handling.
    """
    if not settings.USE_PGBOUNCER:
        return None

    try:
        admin_url = make_url(settings.DATABASE_URL).set(database="pgbouncer")
        # psycopg2/libpq's PQexec path is the simple query protocol when no
        # parameters are supplied. PgBouncer explicitly requires that for
        # its admin console. Keep the connection unpooled and autocommit so
        # SHOW commands never leave an admin-console transaction open.
        conn = psycopg2.connect(
            admin_url.render_as_string(hide_password=False),
            connect_timeout=_PROBE_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            conn.set_session(autocommit=True)
            with conn.cursor() as cur:
                cur.execute("SHOW POOLS")
                pool_columns = [d.name for d in cur.description]
                pools = [dict(zip(pool_columns, row)) for row in cur.fetchall()]

                cur.execute("SHOW STATS")
                stats_columns = [d.name for d in cur.description]
                stats = [dict(zip(stats_columns, row)) for row in cur.fetchall()]

                # SHOW CONFIG is optional telemetry. If the account is only a
                # stats user, the two load snapshots above still remain useful.
                try:
                    cur.execute("SHOW CONFIG")
                    config_columns = [d.name for d in cur.description]
                    config_rows = [dict(zip(config_columns, row)) for row in cur.fetchall()]
                except Exception:
                    config_rows = []
        finally:
            conn.close()
    except Exception:
        logger.debug("db_pool_metrics: PgBouncer admin console probe failed", exc_info=True)
        return None

    totals = {
        "cl_active": 0,
        "cl_waiting": 0,
        "sv_active": 0,
        "sv_idle": 0,
        "sv_used": 0,
        "maxwait_seconds": 0,
    }
    for row in pools:
        for key in ("cl_active", "cl_waiting", "sv_active", "sv_idle", "sv_used"):
            totals[key] += int(row.get(key, 0) or 0)
        totals["maxwait_seconds"] = max(
            totals["maxwait_seconds"], int(row.get("maxwait", 0) or 0)
        )

    if stats:
        row = stats[0]
        totals["avg_query_time_us"] = int(row.get("avg_query_time", 0) or 0)
        totals["avg_wait_time_us"] = int(row.get("avg_wait_time", 0) or 0)

    config = {str(r.get("key")): r.get("value") for r in config_rows}
    totals["reachable"] = True
    totals["in_use"] = True
    totals["pool_mode"] = config.get("pool_mode")
    totals["max_client_conn"] = int(config["max_client_conn"]) if config.get("max_client_conn") is not None else None
    totals["default_pool_size"] = int(config["default_pool_size"]) if config.get("default_pool_size") is not None else None
    totals["reserve_pool_size"] = int(config["reserve_pool_size"]) if config.get("reserve_pool_size") is not None else None
    return totals


def postgres_activity_snapshot() -> dict | None:
    """Best-effort ground-truth connection count straight from
    pg_stat_activity, bypassing PgBouncer's own accounting entirely.
    Returns None (never raises) if this isn't Postgres or the probe
    fails for any reason -- same contract as
    database.py's _probe_postgres_connection_budget().
    """
    try:
        url = make_url(DIRECT_DATABASE_URL)
        if not url.get_backend_name().startswith("postgresql"):
            return None
        probe_engine = create_engine(
            url, poolclass=NullPool,
            connect_args={"connect_timeout": _PROBE_CONNECT_TIMEOUT_SECONDS},
        )
        try:
            with probe_engine.connect() as conn:
                max_connections = conn.execute(
                    text("SELECT current_setting('max_connections')::int")
                ).scalar()
                rows = conn.execute(
                    text(
                        "SELECT state, count(*) AS n FROM pg_stat_activity "
                        "WHERE datname = current_database() GROUP BY state"
                    )
                ).all()
        finally:
            probe_engine.dispose()
    except Exception:
        logger.debug("db_pool_metrics: pg_stat_activity probe failed", exc_info=True)
        return None

    by_state = {(state or "unknown"): count for state, count in rows}
    idle_in_txn = by_state.get("idle in transaction", 0) + by_state.get(
        "idle in transaction (aborted)", 0
    )
    return {
        "max_connections": max_connections,
        "total_connections": sum(by_state.values()),
        "active": by_state.get("active", 0),
        "idle": by_state.get("idle", 0),
        "idle_in_transaction": idle_in_txn,
    }


def register_gauges() -> None:
    """Register the three ObservableGauges described in this module's
    docstring. MUST be called AFTER telemetry.setup_tracing() (see
    main.py) -- unlike overload_monitor.py's Counter (created lazily on
    first 503, well after startup), an ObservableGauge's callback is
    wired up ONCE here and re-invoked by the SDK on every collection
    tick, so `metrics.get_meter()` needs to already resolve to the real
    configured MeterProvider at call time, not the API's no-op default.

    Safe to call unconditionally regardless of OTEL_ENABLED: when
    telemetry hasn't been configured (or the SDK isn't importable), the
    OTel API's own no-op MeterProvider serves an ObservableGauge whose
    callback is simply never invoked by anything -- none of the DB/
    PgBouncer probing in this module ever runs, so there's zero added
    load when metrics export is off. Idempotent -- safe to call more
    than once (e.g. from tests); only registers once per process.
    """
    global _gauges_registered
    with _gauges_lock:
        if _gauges_registered:
            return
        try:
            from opentelemetry import metrics
            from opentelemetry.metrics import Observation
        except ImportError:
            return

        meter = metrics.get_meter("asset-mgt-render-free/db-pool")

        def _sqlalchemy_callback(_options):
            for field, value in sqlalchemy_pool_snapshot().items():
                yield Observation(value, {"pool": "sqlalchemy", "metric": field})

        def _pgbouncer_callback(_options):
            snap = pgbouncer_pool_snapshot()
            if not snap:
                return
            for field, value in snap.items():
                yield Observation(value, {"pool": "pgbouncer", "metric": field})

        def _postgres_callback(_options):
            snap = postgres_activity_snapshot()
            if not snap:
                return
            for field, value in snap.items():
                yield Observation(value, {"pool": "postgres", "metric": field})

        meter.create_observable_gauge(
            name="db.pool.sqlalchemy",
            callbacks=[_sqlalchemy_callback],
            unit="1",
            description=(
                "This process's SQLAlchemy connection pool state "
                "(pool_size/checked_out/checked_in/overflow)."
            ),
        )
        meter.create_observable_gauge(
            name="db.pool.pgbouncer",
            callbacks=[_pgbouncer_callback],
            unit="1",
            description=(
                "PgBouncer's own SHOW POOLS/SHOW STATS view of client/server "
                "connection usage and client wait times."
            ),
        )
        meter.create_observable_gauge(
            name="db.pool.postgres",
            callbacks=[_postgres_callback],
            unit="1",
            description=(
                "Ground-truth pg_stat_activity connection counts on the "
                "target Postgres server, independent of PgBouncer."
            ),
        )

        _gauges_registered = True


def snapshot_all() -> dict:
    """Combined, on-demand snapshot of all three -- used by
    GET /api/diagnostics/db-pool so an operator can read real numbers
    without needing OTEL_ENABLED or an APM backend configured at all.
    Also echoes the currently-configured tuning knobs alongside the live
    numbers, so a single response answers both "what's configured" and
    "what's actually happening" without a second lookup.
    """
    route = _database_route_snapshot()
    pgb = pgbouncer_pool_snapshot()
    if pgb is None and route["configured"]:
        pgb = {"reachable": False, "in_use": route["in_use"], "cl_active": 0, "cl_waiting": 0, "sv_active": 0, "sv_idle": 0, "sv_used": 0}
    return {
        "database_route": route,
        "sqlalchemy_pool": sqlalchemy_pool_snapshot(),
        "pgbouncer_pool": pgb,
        "postgres_activity": postgres_activity_snapshot(),
        "configured": {
            "use_pgbouncer": settings.USE_PGBOUNCER,
            "pgbouncer_server_pool_size": settings.PGBOUNCER_SERVER_POOL_SIZE,
            "pgbouncer_safety_margin_percent": settings.PGBOUNCER_SAFETY_MARGIN_PERCENT,
            "db_background_connection_reserve": settings.DB_BACKGROUND_CONNECTION_RESERVE,
            "db_background_concurrency_limit": settings.DB_BACKGROUND_CONCURRENCY_LIMIT,
            "db_connection_safety_margin": settings.DB_CONNECTION_SAFETY_MARGIN,
        },
    }


def reset_for_tests() -> None:
    """Test-only: allow register_gauges() to run again in a fresh test
    that wants to verify registration behavior."""
    global _gauges_registered
    with _gauges_lock:
        _gauges_registered = False
