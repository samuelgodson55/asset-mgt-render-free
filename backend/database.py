"""
database.py
-----------
Owns the SQLAlchemy engine/session setup, table creation, and a small
"seed" routine that populates a handful of demo records the very first
time the app boots against an empty database. That way, after
`docker compose up`, you can log straight in and see a working dashboard
instead of an empty one.
"""

import datetime
import logging
from functools import lru_cache
from integrations.fastapi_errorbeacon import report_background_exception
import os
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from models import Base, utc_now
import models
from security import hash_password, SUPER_ADMIN_ROLE
from config import settings

logger = logging.getLogger(__name__)

# Retrieve the connection string from the central `settings` object
# (backend/config.py), which itself reads DATABASE_URL from the
# environment (injected by docker-compose.yml from your git-ignored
# `.env` file) or falls back to a safe local-dev default.
DATABASE_URL = settings.DATABASE_URL
DIRECT_DATABASE_URL = settings.DIRECT_DATABASE_URL or settings.DATABASE_URL


def _probe_postgres_connection_budget(url) -> "tuple[int, int] | None":
    """
    One-off, best-effort look at the TARGET Postgres server's actual
    connection budget, so pool sizing (see `_compute_pool_sizing()` below)
    adapts to whatever database this process is really pointed at --
    today's `postgresSkuName` (infra/main.bicep), a manually resized
    server, or a completely different Postgres altogether -- instead of a
    number guessed at deploy time that quietly goes stale.

    Returns `(max_connections, superuser_reserved_connections)`, both
    standard, non-superuser-restricted Postgres GUCs any role can read.
    Returns None (never raises) if this isn't Postgres or the probe fails
    for any reason -- callers MUST have a safe static fallback for that
    case, since e.g. the test suite points this at SQLite, and a brand
    new environment's Postgres may not even be reachable yet the first
    time this module is imported.

    Uses its own short-lived, unpooled connection (`NullPool`, a tight
    3s connect timeout) that's closed again immediately -- this never
    holds a connection open, so it doesn't itself eat into the very
    budget it's measuring.
    """
    try:
        if not url.get_backend_name().startswith("postgresql"):
            return None
        probe_engine = create_engine(
            url, poolclass=NullPool, connect_args={"connect_timeout": 3}
        )
        try:
            with probe_engine.connect() as conn:
                max_conn, reserved = conn.execute(
                    text(
                        "SELECT current_setting('max_connections')::int, "
                        "current_setting('superuser_reserved_connections')::int"
                    )
                ).one()
            return int(max_conn), int(reserved)
        finally:
            probe_engine.dispose()
    except Exception as exc:
        report_background_exception(exc, component="database", operation="probe_connection_budget", severity="warning")
        logger.warning(
            "database: couldn't probe the Postgres server's connection budget "
            "(unreachable, not Postgres, or lacking permission to read "
            "max_connections) -- falling back to a conservative static pool size.",
            exc_info=True,
        )
        return None


def _pgbouncer_server_pool_budget() -> int:
    """Return a safe PgBouncer budget, capped by the live PostgreSQL server.

    The managed Azure pool size is supplied by infrastructure (derived from
    the Azure compute SKU rather than hard-coded to Azure's generic 50). We
    still probe the direct PostgreSQL endpoint when possible so an operator
    who lowers max_connections cannot accidentally leave the application
    admitting more server connections than the database can safely accept.
    """
    configured = settings.PGBOUNCER_SERVER_POOL_SIZE
    if configured is None or int(configured) <= 0:
        configured_budget = max(
            int(settings.PGBOUNCER_DEFAULT_POOL_SIZE)
            + int(settings.PGBOUNCER_RESERVE_POOL_SIZE),
            1,
        )
    else:
        configured_budget = max(int(configured), 1)

    budget = _probe_postgres_connection_budget(make_url(DIRECT_DATABASE_URL))
    if budget is None:
        return configured_budget

    max_connections, reserved = budget
    live_safe = max(
        max_connections
        - reserved
        - int(settings.DB_CONNECTION_SAFETY_MARGIN),
        1,
    )
    return min(configured_budget, live_safe)


def _split_process_budget(total_budget: int, total_processes: int) -> tuple[int, int]:
    """Split a global DB budget without exceeding it in aggregate.

    Keep each process's full share in SQLAlchemy's persistent pool instead of
    splitting a tiny share between ``pool_size`` and ``max_overflow``. With
    PgBouncer already providing transaction pooling, short-lived SQLAlchemy
    overflow connections add churn without adding database capacity. A stable
    pool also means the normal request path does not need to create an overflow
    connection during a burst before PgBouncer can reuse an existing server
    connection. ``max_overflow`` remains available through the explicit
    DB_POOL_SIZE/DB_MAX_OVERFLOW escape hatch for a deployment that has measured
    a different workload shape.
    """
    total_budget = max(int(total_budget), 1)
    total_processes = max(int(total_processes), 1)
    per_process_budget = max(total_budget // total_processes, 1)
    per_process_budget = min(per_process_budget, 20)
    return per_process_budget, 0


def _apply_explicit_pool_override(
    pool_size: int, max_overflow: int, budget: int, total_processes: int
) -> tuple[int, int]:
    """Apply the DB_POOL_SIZE/DB_MAX_OVERFLOW escape hatch (config.py) on top
    of an adaptively-computed (pool_size, max_overflow), if either is set.

    config.py's own docstring for these two settings is explicit: "set both
    to force a fixed, non-adaptive size instead" -- an operator setting both
    expects EXACTLY that pool_size/max_overflow, not some other split of the
    same total. This must still never let a process exceed its share of the
    real budget, so a request that doesn't fit is capped (via
    _split_process_budget, since at that point the operator's exact numbers
    are no longer safe to honor) rather than silently granted in full.
    """
    if settings.DB_POOL_SIZE is None and settings.DB_MAX_OVERFLOW is None:
        return pool_size, max_overflow

    requested_pool = int(settings.DB_POOL_SIZE or 0)
    requested_overflow = int(settings.DB_MAX_OVERFLOW or 0)
    requested_total = max(requested_pool + requested_overflow, 1)
    allowed_total = max(int(budget) // total_processes, 1)
    if requested_total > allowed_total:
        logger.warning(
            "database: explicit DB_POOL_SIZE/DB_MAX_OVERFLOW=%d/%d "
            "would exceed the available budget %d across %d process(es); "
            "capping this process to %d total connections.",
            requested_pool, requested_overflow, budget,
            total_processes, allowed_total,
        )
        return _split_process_budget(allowed_total, 1)
    # Fits within budget -- honor the operator's exact requested split
    # rather than re-deriving a different pool_size/max_overflow ratio
    # from the same total.
    return requested_pool, requested_overflow


def pgbouncer_effective_budget() -> int:
    """The live-probed, safety-margin-adjusted PgBouncer server-pool budget
    -- the SAME total `_compute_pool_sizing()` derives below before it
    carves out `background_reserve` for Celery/Beat. Only meaningful when
    `settings.USE_PGBOUNCER` is true; callers are responsible for that
    check (this function doesn't itself branch on it, so it stays a plain,
    context-free derivation like `_pgbouncer_server_pool_budget()` above).

    BUG FIX: this used to be inlined directly inside `_compute_pool_sizing()`
    below, with no way for anything outside this module to see the result.
    db_admission.py's `background_db_slot()` independently recomputed its
    own "how big is the PgBouncer server pool" number from the same raw
    settings (PGBOUNCER_SERVER_POOL_SIZE / DEFAULT_POOL_SIZE+RESERVE_POOL_SIZE)
    but WITHOUT the live Postgres probe or the safety-margin percentage
    applied here -- so if the live server turned out smaller than
    configured (a resized SKU, a misconfigured env var), THIS module would
    correctly shrink the API pool's share and `background_reserve` to
    match, while db_admission.py's Celery-side admission ceiling kept
    admitting against the old, larger, un-probed number. That let the API
    pool and background workers independently believe they each owned
    capacity that, combined, could exceed what PgBouncer/Postgres could
    actually grant -- exactly the invariant this whole feature exists to
    protect. Exposing this as a public function (called once, cached in
    `PGBOUNCER_EFFECTIVE_BUDGET` below, right alongside `POOL_SIZE`/
    `MAX_OVERFLOW`) lets db_admission.py read the EXACT SAME number this
    module used for its own reserve, instead of quietly maintaining a
    second, driftable copy of the same computation.
    """
    pgbouncer_budget = _pgbouncer_server_pool_budget()
    safety_percent = min(max(int(settings.PGBOUNCER_SAFETY_MARGIN_PERCENT), 0), 50)
    # Keep an explicit percentage of the pool unused for operational
    # breathing room, then reserve a separate slot for background work.
    # The percentage is applied before the background reservation.
    return max(int(pgbouncer_budget * (100 - safety_percent) / 100), 1)


def _compute_pool_sizing(database_url: str) -> "tuple[int, int]":
    """Compute a bounded SQLAlchemy pool for this process.

    The important invariant is: for a known deployment topology,
    `(pool_size + max_overflow) * DB_EXPECTED_PROCESSES` never exceeds the
    authoritative PgBouncer server pool when PgBouncer is enabled. Probe
    failures never enlarge the pool.
    """
    if settings.DB_EXPECTED_PROCESSES is not None:
        total_processes = max(settings.DB_EXPECTED_PROCESSES, 1)
    else:
        environment = os.environ.get("ENVIRONMENT", "development").strip().lower()
        is_production = environment in ("production", "prod")
        uvicorn_workers = int(os.environ.get("UVICORN_WORKERS", "1")) if is_production else 1
        # The embedded Celery worker is deliberately NOT counted as another
        # full API-pool owner. Its DB work is already protected by the
        # deployment-wide DB_BACKGROUND_CONNECTION_RESERVE /
        # DB_BACKGROUND_CONCURRENCY_LIMIT lease below. Counting that process
        # here as well double-reserves capacity and, on a small ACA deployment,
        # can collapse a healthy PgBouncer budget into a one-connection API
        # pool per Uvicorn process. The Celery process still has its own
        # SQLAlchemy pool, but it can only admit the separately-reserved
        # background DB work, so it must not consume another full API share.
        total_processes = max(settings.BACKEND_MAX_REPLICAS, 1) * max(uvicorn_workers, 1)

    if settings.USE_PGBOUNCER:
        pgbouncer_budget = pgbouncer_effective_budget()
        # Keep one small, explicit slice of the shared PgBouncer budget for
        # Celery/Beat DB tasks.  The background tasks acquire a distributed
        # semaphore before opening a session, so the API pool must not be
        # allowed to consume that reserved capacity.  This makes the global
        # invariant explicit: API pool capacity + background DB capacity <=
        # the PgBouncer server pool.
        background_reserve = min(
            max(
                int(settings.DB_BACKGROUND_CONNECTION_RESERVE),
                int(settings.DB_BACKGROUND_CONCURRENCY_LIMIT),
            ),
            max(pgbouncer_budget - 1, 0),
        )
        api_budget = max(pgbouncer_budget - background_reserve, 1)
        pool_size, max_overflow = _split_process_budget(api_budget, total_processes)
        pool_size, max_overflow = _apply_explicit_pool_override(
            pool_size, max_overflow, api_budget, total_processes
        )
        logger.info(
            "database: PgBouncer-bounded pool -- server_pool=%d, background_reserve=%d, "
            "api_budget=%d, %d process(es) expected -> pool_size=%d max_overflow=%d "
            "per process (%d total API connections).",
            pgbouncer_budget, background_reserve, api_budget, total_processes, pool_size, max_overflow,
            (pool_size + max_overflow) * total_processes,
        )
        return pool_size, max_overflow

    # Direct PostgreSQL path: probe the actual server. A failed probe must
    # fall back to a small fixed pool rather than the old 3+2 per-process
    # value multiplied across the whole fleet.
    probe_url = make_url(DIRECT_DATABASE_URL or database_url)
    budget = _probe_postgres_connection_budget(probe_url)
    if budget is None:
        pool_size, max_overflow = _split_process_budget(5, total_processes)
        pool_size, max_overflow = _apply_explicit_pool_override(
            pool_size, max_overflow, 5, total_processes
        )
        logger.warning(
            "database: Postgres budget probe unavailable; using bounded "
            "fallback pool_size=%d max_overflow=%d across %d process(es).",
            pool_size, max_overflow, total_processes,
        )
        return pool_size, max_overflow

    max_connections, reserved = budget
    available = max(max_connections - reserved - settings.DB_CONNECTION_SAFETY_MARGIN, 1)
    pool_size, max_overflow = _split_process_budget(available, total_processes)
    pool_size, max_overflow = _apply_explicit_pool_override(
        pool_size, max_overflow, available, total_processes
    )
    logger.info(
        "database: adaptive direct-Postgres pool -- max_connections=%d "
        "reserved=%d safety_margin=%d, %d process(es) -> pool_size=%d "
        "max_overflow=%d per process (%d total).",
        max_connections, reserved, settings.DB_CONNECTION_SAFETY_MARGIN,
        total_processes, pool_size, max_overflow,
        (pool_size + max_overflow) * total_processes,
    )
    return pool_size, max_overflow

# -----------------------------------------------------------------------
# BUG FIX ("couldn't access my db" in production, works fine locally):
#
# `create_engine(DATABASE_URL)` with no extra arguments was handing out
# connections from SQLAlchemy's default pool with no health-check and no
# recycling. That's mostly invisible in local Docker Compose (Postgres runs
# right next to the app, gets hit constantly, and the container/network
# never goes away underneath it) -- but it's a real, reproducible failure
# mode against a managed cloud Postgres like Azure Database for PostgreSQL
# Flexible Server (see .env.azure.example / infra/main.bicep) or Render's
# managed Postgres:
#
#   - Managed Postgres providers silently close idle server-side
#     connections after some minutes (and Azure Flexible Server's default
#     `idle_session_timeout`/firewall/load-balancer layers can drop a TCP
#     connection outright without either side sending a clean FIN). A
#     production deployment naturally has longer idle gaps between requests
#     per pooled connection than a dev box you're actively hammering, so
#     it hits this far more often.
#   - SQLAlchemy's pool doesn't know the connection died until it actually
#     tries to use it -- the NEXT request to reuse that dead connection
#     fails with something like `OperationalError: SSL connection has been
#     closed unexpectedly` or `server closed the connection unexpectedly`.
#     From the app's point of view (and from the outside, e.g. the frontend
#     showing a failed API call) that looks exactly like "the app can't
#     reach the database" even though the database itself is perfectly
#     healthy and reachable.
#   - There was also no connect timeout at all: a genuinely unreachable
#     DB (wrong host, firewall blocking the container's IP, etc. -- see
#     infra/main.bicep's postgresFirewallAzure rule) would hang the
#     connection attempt for whatever the OS-level TCP timeout happens to
#     be (often 60s+) instead of failing fast with a clear error.
#
# Fix, all standard SQLAlchemy pooling knobs:
#   pool_pre_ping=True   -- runs a cheap `SELECT 1` before handing a pooled
#                           connection to the app; a dead connection is
#                           transparently discarded and replaced instead of
#                           surfacing as a request failure.
#   pool_recycle=1800    -- proactively recycle any connection older than
#                           30 minutes, well under typical managed-Postgres
#                           idle-close windows, so pre_ping rarely even has
#                           to catch a dead one.
#   connect_args:
#     connect_timeout=10 -- fail fast (10s) with a clear psycopg2 error
#                           instead of hanging when the DB is genuinely
#                           unreachable (bad host/port, firewall rule not
#                           yet applied, etc.).
#
# BUG FIX ("Audit Logs page fails on repeated refresh, but recovers if you
# stop hammering it for a bit" -- psycopg2.OperationalError: ... remaining
# connection slots are reserved for roles with the SUPERUSER attribute):
# this engine was ALSO being created with no pool_size/max_overflow of its
# own, which meant it silently inherited SQLAlchemy's defaults
# (pool_size=5, max_overflow=10 -- up to 15 live connections per process
# that imports this module), with no awareness of how many OTHER
# processes (other Container App replicas, each with their own embedded
# Celery worker -- see start.sh) are doing the exact same thing at once,
# or of how many connections the target Postgres server can even grant.
#
# _compute_pool_sizing() above works both of those out AT STARTUP and
# adapts automatically -- no fixed number to keep in sync by hand every
# time backendMaxReplicas (infra/main.bicep) or the Postgres SKU changes,
# and no need to log into production to check either one. pool_timeout
# makes "this process's own pool is momentarily full" fail fast with a
# clear error instead of a request hanging.
# -----------------------------------------------------------------------
_DB_POOL_SIZE, _DB_MAX_OVERFLOW = _compute_pool_sizing(DIRECT_DATABASE_URL)
# Public read-only values used by the request-level DB concurrency guard.
POOL_SIZE = _DB_POOL_SIZE
MAX_OVERFLOW = _DB_MAX_OVERFLOW
# Public read-only value used by db_admission.py's background-task admission
# ceiling (see pgbouncer_effective_budget()'s own docstring for the bug this
# fixes). Computed once here, at the same startup moment as POOL_SIZE/
# MAX_OVERFLOW above, rather than re-probed on every background task
# admission -- the live probe is a real network round trip (up to a few
# seconds on failure) and is meant to be a one-off startup cost, not a
# per-task one. None when PgBouncer isn't in use, since the concept doesn't
# apply to the direct-Postgres path.
PGBOUNCER_EFFECTIVE_BUDGET = pgbouncer_effective_budget() if settings.USE_PGBOUNCER else None

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=_DB_POOL_SIZE,
    max_overflow=_DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
    connect_args={"connect_timeout": 10},
)

# -----------------------------------------------------------------------
# Per-transaction statement_timeout (see DB_STATEMENT_TIMEOUT_MS's own
# config.py comment for the full "why `begin`, why SET LOCAL, why not the
# readiness engine's connect-time approach" reasoning -- short version:
# PgBouncer's transaction pooling DISCARDs ALL session state, including any
# statement_timeout, when a transaction's backend connection is returned to
# the pool, so a value set once at connection-open time would not reliably
# apply to later transactions on this same pooled SQLAlchemy connection.
# `SET LOCAL` inside the "begin" event is re-issued on every single
# transaction and is automatically scoped to just that transaction, so it
# is correct whether USE_PGBOUNCER is true or false.
#
# Registered on this module-level `engine` object only. Tests never see
# this: tests/conftest.py's db_engine fixture builds and swaps in its OWN
# separate SQLite engine for `database.engine`/`database.SessionLocal`
# rather than reusing this one (see that fixture's module docstring for
# why -- connect_args={"connect_timeout": 10} above is Postgres/psycopg2-
# only and SQLite's DBAPI rejects it), so this listener is simply never
# attached to whatever engine tests actually run queries against. The
# dialect check below is a second, independent layer of safety in case
# this module is ever pointed at a non-Postgres DATABASE_URL directly.
# -----------------------------------------------------------------------
if settings.DB_STATEMENT_TIMEOUT_MS > 0 and engine.url.get_backend_name().startswith("postgresql"):

    @event.listens_for(engine, "begin")
    def _set_transaction_statement_timeout(conn):
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(settings.DB_STATEMENT_TIMEOUT_MS)}")


def set_transaction_statement_timeout(session, timeout_ms: int) -> None:
    """Raise/lower the timeout for one explicitly-known heavy operation.

    The normal application default remains ``DB_STATEMENT_TIMEOUT_MS``. This
    helper is an intentional per-operation escape hatch for a query that has
    been reviewed and proven safe to run longer (for example, a bounded admin
    report on a much larger dataset). It uses ``SET LOCAL``, so the override
    disappears automatically at transaction end and cannot leak into a later
    request, which is essential when PgBouncer is in transaction-pool mode.

    Only a positive integer number of milliseconds is accepted. The caller is
    responsible for choosing a justified value; do not use this helper as a
    blanket replacement for the global timeout.
    """
    bind = session.get_bind()
    if bind is None or not bind.dialect.name.startswith("postgresql"):
        return
    timeout_ms = int(timeout_ms)
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    session.connection().exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")


# Create a session factory for generating isolated database transactions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """
    Tells SQLAlchemy to automatically generate all our defined tables
    inside the PostgreSQL container if they don't already exist.

    NOTE ON ALEMBIC (requirement #2): this call is safe to leave in place --
    `create_all()` only creates tables that don't exist yet and never alters
    existing ones, so it won't conflict with Alembic. Once you've run the
    baseline migration (see README.md's "Alembic" section), Alembic becomes
    the source of truth for all FUTURE schema changes (new columns, new
    tables, etc.) -- just remember to write a new migration any time you
    change models.py instead of relying on this function to pick it up.
    """
    Base.metadata.create_all(bind=engine)


def get_db():
    """
    Dependency provider that yields a database session per API request,
    ensuring connections are automatically closed when a request finishes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        # BUG FIX: a plain `db.close()` here was masking the REAL error on
        # any request whose own connection got forcibly killed mid-request
        # -- currently only services/backup_service.py's restore_backup(),
        # which (deliberately, as of its own fix) runs `pg_terminate_backend`
        # against every OTHER connection to the database, including this
        # very request's own session, right before resetting the schema
        # (see that function's comment for why). `Session.close()` still
        # tries to ROLLBACK the underlying DBAPI connection as part of
        # closing it -- but that connection's socket was already killed
        # server-side, so the rollback itself raises
        # `psycopg2.OperationalError: server closed the connection
        # unexpectedly`. Uncaught, that replaced/chained on top of
        # whatever real error the route had already raised (e.g. restore's
        # own clear "Restore failed: ..." RuntimeError), so the person and
        # the logs saw a confusing SECOND traceback about a dead
        # connection instead of the actual, more useful failure reason.
        # Closing a session whose connection is already gone is expected
        # and harmless here -- just let it go instead of letting a
        # cleanup-time error overwrite/obscure the request's real outcome.
        try:
            db.close()
        except Exception:
            logger.warning(
                "database.get_db: session cleanup failed (connection was likely already "
                "terminated server-side) -- ignoring, since this must not override the "
                "request's real error/response.",
                exc_info=True,
            )


@lru_cache(maxsize=1)
def _expected_migration_heads() -> tuple[str, ...]:
    """Return the immutable migration heads baked into this application image.

    The code/migrations shipped in a running container do not change, so there
    is no reason to parse the Alembic script directory on every readiness probe.
    Caching this removes avoidable filesystem/import work from a hot probe path.
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    backend_dir = os.path.dirname(os.path.abspath(__file__))
    alembic_cfg = AlembicConfig(os.path.join(backend_dir, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    return tuple(sorted(ScriptDirectory.from_config(alembic_cfg).get_heads()))


def _create_readiness_engine_from_url(source_url):
    """Create a short-lived, unpooled engine for a supplied database URL.

    Readiness must never wait behind the application's normal SQLAlchemy pool.
    Under load, a full request pool could otherwise make /readyz appear hung
    even though PostgreSQL itself is healthy. NullPool plus short PostgreSQL
    connect/statement timeouts makes the probe deterministic: healthy DB =>
    fast 200; unreachable/slow DB => fast 503.
    """
    # IMPORTANT: never round-trip a SQLAlchemy URL through `str()` here.
    # `str(URL)` intentionally hides the password as `***` for safe logging.
    # Feeding that masked string back into `make_url()` changes the actual
    # credentials used by the readiness connection and causes misleading
    # `password authentication failed` errors after a restore. Keep an
    # existing SQLAlchemy URL object intact; only parse plain strings.
    url = source_url if hasattr(source_url, "get_backend_name") else make_url(str(source_url))
    kwargs = {"poolclass": NullPool}
    is_postgres = url.get_backend_name().startswith("postgresql")
    if is_postgres:
        # BUG FIX (readyz always 503s locally/on the VM -- "unsupported
        # startup parameter: options"): this used to pass
        # connect_args={"options": "-c statement_timeout=3000"}, which
        # psycopg2 sends as the literal `options` field of the Postgres
        # STARTUP PACKET, not a regular query. When USE_PGBOUNCER=true
        # (the default), `engine.url` -- and therefore this readiness
        # engine -- points at PgBouncer, not straight at Postgres. PgBouncer
        # rejects any startup parameter it isn't explicitly told to allow
        # (see docker-compose.yml/docker-compose.vm.yml's pgbouncer service:
        # IGNORE_STARTUP_PARAMETERS only lists `extra_float_digits`, not
        # `options`), so every readiness connection was refused before a
        # single query ran -- /readyz never got far enough to even check
        # `alembic_version`, it just 503'd on "Could not reach the database"
        # with a startup-parameter error as the real (logged) reason.
        # Direct-to-Postgres (USE_PGBOUNCER=false) never hit this, which is
        # why it worked "sometimes" depending on routing.
        #
        # Fix: only send `connect_timeout` in the startup packet (a
        # psycopg2/libpq option PgBouncer always understands) and set the
        # statement timeout via a normal `SET` command instead, right after
        # each new DBAPI connection is opened -- an ordinary query works
        # identically whether this engine is pointed straight at Postgres or
        # through PgBouncer's transaction pool, and PgBouncer's own
        # SERVER_RESET_QUERY: DISCARD ALL (already configured) clears it
        # when the underlying server connection is returned to the pool.
        kwargs["connect_args"] = {"connect_timeout": 3}
    readiness_engine = create_engine(url, **kwargs)
    if is_postgres:
        from sqlalchemy import event

        @event.listens_for(readiness_engine, "connect")
        def _set_readiness_statement_timeout(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET statement_timeout = 3000")
            finally:
                cursor.close()

    return readiness_engine


# Never share the application's pooled engine with /readyz. In production this
# creates a separate unpooled connection path. The source-engine check also
# matters in tests, where conftest.py swaps `database.engine` for a temporary
# SQLite engine; readiness must follow that test database rather than the
# original production-style engine created during module import.
def _create_readiness_engine():
    """Create the readiness engine from the application's current engine URL."""
    return _create_readiness_engine_from_url(engine.url)


# Keep a dedicated readiness engine separate from the request pool. The
# variable is initialized once, then `_get_readiness_engine()` replaces it if
# tests or application startup swap the main engine for another database.
readiness_engine = _create_readiness_engine()
_readiness_engine_source = engine
_readiness_engine_source_url = engine.url


def _get_readiness_engine():
    """Return a readiness-only engine matching the current application engine."""
    global readiness_engine, _readiness_engine_source, _readiness_engine_source_url

    # Compare both the engine object and its URL. Tests and restore workflows
    # can replace/dispose the main engine without replacing this module's
    # readiness engine object. Comparing the URL object itself keeps the
    # credentials/host identity intact because `str(URL)` masks passwords.
    # identity check. `str(engine.url)` deliberately masks passwords and
    # could make two different database credentials look identical.
    current_source_url = engine.url
    if (
        readiness_engine is not None
        and _readiness_engine_source is engine
        and _readiness_engine_source_url == current_source_url
    ):
        return readiness_engine

    if readiness_engine is not None:
        readiness_engine.dispose()

    readiness_engine = _create_readiness_engine_from_url(engine.url)
    _readiness_engine_source = engine
    _readiness_engine_source_url = current_source_url
    return readiness_engine


def get_schema_status() -> dict:
    """
    Compares the database's ACTUAL current migration revision (whatever
    `alembic upgrade head` last left behind in its `alembic_version` table)
    against the revision THIS CODE was built to run against (the head of
    backend/alembic/versions/ baked into this image at build time). Powers
    GET /readyz in main.py -- see that endpoint's docstring for why this
    check lives there and deliberately NOT in GET /healthz.

    This is the check that was missing before: the deploy pipelines
    (.github/workflows/deploy-azure-*.yml) already run `alembic upgrade
    head` as a separate, blocking step before rolling out a new image --
    but nothing verified that a given RUNNING container's schema still
    actually matches what its own code expects. If that migrate step were
    ever skipped or bypassed (e.g. a manual `az containerapp update`
    straight to an image tag, no pipeline involved), the old /healthz --
    a static "yes I'm up" with no DB awareness at all -- would happily
    report healthy against a schema the new code doesn't actually match,
    and the first real symptom would be a request failing on a missing
    column instead of the deploy failing up front.

    Returns a dict; "ready" is False for any of:
      - the database can't be reached at all (same causes as init_db()'s
        startup check: wrong DATABASE_URL, missing sslmode, firewall, DB
        not up yet)
      - the `alembic_version` table doesn't exist yet -- `alembic upgrade
        head` has never been run against this database
      - `alembic_version` exists but is empty -- same as above
      - `alembic_version`'s revision(s) don't match this build's expected
        head(s) -- the exact "new image, old/wrong schema" scenario
    "ready" is True only when the database's current revision(s) exactly
    equal this code's expected head(s).
    """
    from sqlalchemy import inspect, text

    # Resolve the migration(s) THIS CODE expects to be current once per
    # process. The database side is still checked on every probe.
    expected_heads = set(_expected_migration_heads())

    try:
        # Refresh the dedicated readiness engine first when tests/startup swap
        # the application's main engine, then use that dedicated engine
        # explicitly. This keeps /readyz independent from the request pool.
        _get_readiness_engine()
        with readiness_engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                # BUG FIX (silent readiness failures): every "not ready"
                # branch below used to return straight to the caller with
                # no logging at all. That made a READINESS probe that fails
                # forever -- e.g. the exact "new image, DB unreachable/
                # schema mismatch" scenarios this function exists to catch
                # -- completely invisible in Log Analytics/console logs:
                # the replica keeps running (this isn't a liveness failure,
                # so nothing restarts it or logs a crash), Container Apps
                # just never routes traffic to it and eventually reports
                # the revision as failed to activate, and `az containerapp
                # logs show`/the console log stream shows nothing but a
                # clean startup -- indistinguishable from a healthy
                # container from the logs alone.
                #
                # Logging the exact status dict here (not a paraphrase) is
                # deliberate, not just for humans reading Log Analytics --
                # infra/main.bicep's `alertReadyzFailing` scheduled query
                # rule already greps ContainerAppConsoleLogs_CL for
                # `Log_s has "readyz" and Log_s has "\"ready\": false"`
                # (see that resource's own comment: "Same KQL as
                # SRE_STRATEGY.md section 2b"). That alert has been dead
                # code since it was added -- nothing ever produced a
                # console log line matching it, so it could never fire no
                # matter how long /readyz stayed broken.
                #
                # IMPORTANT: passing the dict via `extra=status` (not
                # embedding `json.dumps(status)` INSIDE the message
                # string) is what actually makes this match. logging_config.py's
                # JsonFormatter renders the whole log line as ONE JSON
                # object; anything placed inside the "message" field's own
                # string value gets its quotes escaped by that outer
                # json.dumps() (`"ready": false` -> `\"ready\": false` in
                # the raw text) -- KQL's `has "\"ready\": false"` searches
                # for a literal, UNescaped `"` character, so an
                # escaped-quotes version inside "message" would silently
                # never match either, just like the original missing-log
                # bug it's meant to fix. `extra=status` instead makes
                # "ready"/"reason"/etc. their own TOP-LEVEL keys in the
                # JSON payload (see JsonFormatter's extra-field folding
                # loop), so `json.dumps` renders them with real,
                # unescaped quote characters -- exactly the raw substring
                # the alert's KQL is looking for.
                status = {
                    "ready": False,
                    "reason": "Database has no 'alembic_version' table -- "
                              "'alembic upgrade head' has never been run against it.",
                    "expected_heads": sorted(expected_heads),
                    "current_heads": [],
                }
                logger.warning("readyz: not ready", extra=status)
                return status
            current_heads = {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))}
    except Exception as exc:
        report_background_exception(exc, component="database", operation="readiness_check", severity="error")
        # Same "fail legibly, not with a raw traceback" reasoning as
        # main.py's on_startup() -- an unreachable database at readiness-
        # check time should read as "not ready yet", not crash the probe.
        # Unlike on_startup() though, this path is hit on EVERY failed
        # readiness poll (every 10s -- see infra/main.bicep's Readiness
        # probe periodSeconds), not just once at boot, so exc_info=True is
        # deliberately omitted to avoid flooding Log Analytics with a full
        # traceback per poll -- the single-line exception message already
        # in `reason` is enough to point at the cause (wrong host, missing
        # sslmode, firewall) without drowning out everything else.
        status = {
            "ready": False,
            "reason": f"Could not reach the database to check its migration state: {exc}",
            "expected_heads": sorted(expected_heads),
            "current_heads": [],
        }
        logger.warning("readyz: not ready", extra=status)
        return status

    if not current_heads:
        status = {
            "ready": False,
            "reason": "Database's 'alembic_version' table is empty -- "
                      "no migration has ever been recorded as applied.",
            "expected_heads": sorted(expected_heads),
            "current_heads": [],
        }
        logger.warning("readyz: not ready", extra=status)
        return status

    if current_heads != expected_heads:
        status = {
            "ready": False,
            "reason": "Database schema version does not match what this build of the code "
                      "expects -- run 'alembic upgrade head' before routing traffic to this image.",
            "expected_heads": sorted(expected_heads),
            "current_heads": sorted(current_heads),
        }
        logger.warning("readyz: not ready", extra=status)
        return status

    return {
        "ready": True,
        "reason": "Database schema matches this build's expected migration head.",
        "expected_heads": sorted(expected_heads),
        "current_heads": sorted(current_heads),
    }


def _root_admin_demo_row() -> "models.User":
    """
    Builds the LOCAL/DEV/TEST-only root admin row seed_db() inserts
    alongside the other demo accounts (see seed_db()'s docstring). This is
    NOT how production gets its root admin -- see
    alembic/versions/0002_bootstrap_root_admin.py for that -- this exists
    purely so a fresh `docker compose up` (AUTO_SEED_DEMO_DATA=true) has
    something to log into as "super_admin" without requiring Alembic to be
    run by hand first.

    Uses a fixed, well-known demo password (same convention as every other
    demo account below -- e.g. "Admin123!") rather than a randomly
    generated one: unlike the production migration, this path is never
    reachable with ENVIRONMENT=production (config.py's AUTO_SEED_DEMO_DATA
    defaults to false there, and the two are meant to be mutually
    exclusive ways of getting the very first root admin row), so there's
    no real secret to protect here -- same threat model as
    "Admin123!"/"Manager123!" below.
    """
    return models.User(
        name=settings.SUPER_ADMIN_NAME,
        email=f"{settings.SUPER_ADMIN_USERNAME}@local",
        username=settings.SUPER_ADMIN_USERNAME,
        role=SUPER_ADMIN_ROLE,
        password_hash=hash_password("RootAdmin123!"),
        is_verified=True, is_active=True,
    )


def seed_db():
    """
    Populate the database with a small set of realistic demo records the
    first time the app starts against an empty database. Safe to call on
    every startup -- it checks whether any users already exist first and
    does nothing if so, so it will never duplicate data or wipe changes
    you've made through the app.

    Demo login credentials created here (all documented in README.md too).
    Data Quality & Usability requirement #6: every account gets a
    `username` too (auto-derived from the email's local part, mirroring
    `services/user_service.py -> _derive_username()`), so `POST
    /auth/login` accepts EITHER value:
      Admin       -> r.adeyemi@corp.io   / username r.adeyemi   / Admin123!
      Manager     -> s.chen@corp.io      / username s.chen      / Manager123!
      Staff       -> t.okafor@corp.io    / username t.okafor    / Staff123!
      Customer    -> d.martins@customer.io / username d.martins / Customer123!
      Root Admin  -> (SUPER_ADMIN_USERNAME, default "superadmin") / RootAdmin123!
                     -- local/dev/test only, see _root_admin_demo_row() below.

    NOTE on the root admin: there's no "Super Admin" row created below on
    purpose -- this function only runs when AUTO_SEED_DEMO_DATA=true
    (local/dev/test, never production; see config.py). In production, the
    root admin is bootstrapped exactly once by
    alembic/versions/0002_bootstrap_root_admin.py during
    `alembic upgrade head` instead. For local/dev/test convenience (so
    there's still something to log into as "super_admin" without running
    Alembic by hand), _seed_root_admin() below inserts the same singleton
    role, but with a well-known demo password -- see its own docstring.
    """
    db = SessionLocal()
    try:
        if db.query(models.User).count() > 0:
            return  # Already seeded on a previous boot -- do nothing.

        # --- Demo user accounts -------------------------------------------------
        # "admin" has every privilege the hardcoded Super Admin has (see
        # deps.py's _FULL_ADMIN_ROLES), but -- unlike the Super Admin --
        # it's a normal, editable, deletable `users` row like any other
        # account.
        admin = models.User(
            name="R. Adeyemi", email="r.adeyemi@corp.io", username="r.adeyemi", role="admin",
            password_hash=hash_password("Admin123!"), is_verified=True, is_active=True,
        )
        manager = models.User(
            name="S. Chen", email="s.chen@corp.io", username="s.chen", role="manager",
            department="Engineering", department_role="Engineering Manager",
            password_hash=hash_password("Manager123!"), is_verified=True, is_active=True,
        )
        staff_1 = models.User(
            name="T. Okafor", email="t.okafor@corp.io", username="t.okafor", role="staff",
            department="Engineering", department_role="Senior Engineer",
            password_hash=hash_password("Staff123!"), is_verified=True, is_active=True,
        )
        staff_2 = models.User(
            name="A. Bello", email="a.bello@corp.io", username="a.bello", role="staff",
            department="Engineering", department_role="Product Designer",
            password_hash=hash_password("Staff123!"), is_verified=True, is_active=True,
        )
        # "customer" is a login-capable role for external contacts who need
        # to see their own custody ledger, distinct from the anonymous
        # Outsider records created ad-hoc during a checkout (those never log in).
        customer_1 = models.User(
            name="D. Martins", email="d.martins@customer.io", username="d.martins", role="customer",
            department_role="External Client Contact",
            password_hash=hash_password("Customer123!"), is_verified=True, is_active=True,
        )
        db.add_all([admin, manager, staff_1, staff_2, customer_1, _root_admin_demo_row()])
        db.commit()

        # --- Demo asset pools ----------------------------------------------------
        # Every pool now gets a category (previously only used in ad-hoc
        # testing) so "Asset Inventory Export by category" and the
        # Properties Hub's category field have real demo data to show
        # instead of an empty "No category set" state on first boot. Same
        # reasoning for `price` -- every pool gets a realistic per-unit
        # price so the Properties Hub's price field isn't blank either.
        laptop_pool = models.AssetType(name='MacBook Pro 14" M3 Pool', total_quantity=15, available_quantity=14, category="Engineering", department="Camera", price=1899.00)
        monitor_pool = models.AssetType(name="Dell UltraSharp U2723QE Monitor", total_quantity=40, available_quantity=39, category="Engineering", department="Lighting", price=629.99)
        mouse_pool = models.AssetType(name="Logitech MX Master 3S", total_quantity=60, available_quantity=59, category="Operations", department="Grip", price=99.99)
        db.add_all([laptop_pool, monitor_pool, mouse_pool])
        db.commit()

        # --- Demo checkouts so the dashboards aren't empty on first login --------
        demo_checkout = models.AssetCheckout(
            asset_id=laptop_pool.id,
            user_id=staff_1.id,
            quantity=1,
            due_date=utc_now() + datetime.timedelta(days=14),
            status="active",
        )
        demo_customer_checkout = models.AssetCheckout(
            asset_id=mouse_pool.id,
            user_id=customer_1.id,
            quantity=1,
            due_date=utc_now() + datetime.timedelta(days=30),
            status="active",
        )
        db.add_all([demo_checkout, demo_customer_checkout])

        # --- Demo audit trail entries ---------------------------------------------
        db.add_all([
            models.AuditLog(
                operator="r.adeyemi@corp.io", action="POOL_CREATED", target_type="AssetType",
                target_id=laptop_pool.id, details="Initial demo pool seeded on first boot.",
            ),
            models.AuditLog(
                operator="s.chen@corp.io", action="CHECKOUT", target_type="AssetType",
                target_id=laptop_pool.id, details="Assigned 1 unit of 'MacBook Pro 14\" M3 Pool' to Staff: T. Okafor.",
            ),
        ])
        db.commit()
    finally:
        db.close()
