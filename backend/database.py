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
import os
from sqlalchemy import create_engine, text
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
    except Exception:
        logger.warning(
            "database: couldn't probe the Postgres server's connection budget "
            "(unreachable, not Postgres, or lacking permission to read "
            "max_connections) -- falling back to a conservative static pool size.",
            exc_info=True,
        )
        return None


def _compute_pool_sizing(database_url: str) -> "tuple[int, int]":
    """
    Works out this process's own `pool_size`/`max_overflow` so that,
    across every process that can simultaneously be running this same
    code, total connections stay comfortably under the target Postgres
    server's real budget -- automatically, with no number to hand-tune
    (and re-tune every time infra changes) in config.py, and with no need
    to touch the production database directly to check its settings.

    settings.DB_POOL_SIZE/DB_MAX_OVERFLOW remain a manual escape hatch:
    if BOTH are set, they win outright and none of the below runs.
    Otherwise:
      1. Work out how many DB-connecting PROCESSES can exist at once.
         Prefers settings.DB_EXPECTED_PROCESSES if a deployment set it
         explicitly (docker-compose.yml/docker-compose.vm.yml both do --
         see that setting's own docstring for why: they run `worker`/
         `beat` as separate always-on processes rather than embedding
         them, and Celery's own `--concurrency`/blue-green pairing add
         more processes than this module can see from the environment
         alone). Otherwise derives it from settings.BACKEND_MAX_REPLICAS
         (worst-case Container App replica count -- see
         infra/main.bicep's `backendMaxReplicas`, wired through as the
         env var of the same name) x how many uvicorn worker processes
         run per replica (UVICORN_WORKERS, only honored when
         ENVIRONMENT is production/prod -- mirrors start.sh's own logic)
         x whether RUN_EMBEDDED_WORKER (start.sh) also runs an embedded,
         always-`--concurrency=1` Celery worker in-process -- correct
         for infra/main.bicep's Container Apps layout and render.yaml's
         single-instance Free plan.
      2. Probe the live server for its real `max_connections` /
         `superuser_reserved_connections`.
      3. Split (budget - safety margin) evenly across those processes,
         then this one process's own share between `pool_size` (always-
         open) and `max_overflow` (burst capacity).
    If the probe fails (not Postgres, unreachable at import time, etc.),
    falls back to a small static size that's safe against even the
    smallest realistic Postgres server regardless of process count.
    """
    if settings.DB_POOL_SIZE is not None and settings.DB_MAX_OVERFLOW is not None:
        logger.info(
            "database: DB_POOL_SIZE/DB_MAX_OVERFLOW explicitly set (%d/%d) -- "
            "using them as a fixed size instead of adaptive sizing.",
            settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW,
        )
        return settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW

    if settings.DB_EXPECTED_PROCESSES is not None:
        # Explicit, deployment-supplied ground truth wins outright -- see
        # config.py's DB_EXPECTED_PROCESSES docstring for why the
        # replica-derived guess below doesn't fit every compose/infra
        # shape (docker-compose.yml/docker-compose.vm.yml both set this).
        total_processes = max(settings.DB_EXPECTED_PROCESSES, 1)
    else:
        # Mirrors start.sh's OWN logic for when it actually honors
        # UVICORN_WORKERS (production/prod only -- development runs a
        # single `--reload` process regardless of this env var), so the
        # derived process count tracks reality instead of assuming every
        # deployment runs exactly one uvicorn process per replica.
        environment = os.environ.get("ENVIRONMENT", "development").strip().lower()
        is_production = environment in ("production", "prod")
        uvicorn_workers = int(os.environ.get("UVICORN_WORKERS", "1")) if is_production else 1
        embedded_worker = os.environ.get("RUN_EMBEDDED_WORKER", "false").lower() == "true"
        # start.sh's embedded worker is always launched with
        # `--concurrency=1` (hardcoded there, not configurable) -- exactly
        # 1 extra process, never more.
        processes_per_replica = uvicorn_workers + (1 if embedded_worker else 0)
        total_processes = max(settings.BACKEND_MAX_REPLICAS, 1) * processes_per_replica

    budget = _probe_postgres_connection_budget(make_url(database_url))
    if budget is None:
        pool_size, max_overflow = 3, 2
        logger.info(
            "database: adaptive pool sizing unavailable -- using conservative "
            "static defaults (pool_size=%d, max_overflow=%d) instead.",
            pool_size, max_overflow,
        )
        return pool_size, max_overflow

    max_connections, reserved = budget
    available = max_connections - reserved - settings.DB_CONNECTION_SAFETY_MARGIN
    # Never let a bad probe/topology reading drive this to zero or
    # negative -- every process still gets at least a floor of 2
    # connections (1 pooled + 1 overflow) so the app stays usable even on
    # a tiny server, at the cost of the safety margin being the thing
    # that gives if the two truly can't both fit.
    per_process_budget = max(available // total_processes, 2)
    # ...and cap the other direction too: a huge/misreported
    # `max_connections` with a small `total_processes` (e.g. a beefy
    # Postgres someone pointed a single local `docker compose` container
    # at) shouldn't hand one process an unreasonably large pool either.
    per_process_budget = min(per_process_budget, 20)
    pool_size = max(per_process_budget // 2, 1)
    max_overflow = per_process_budget - pool_size

    if settings.DB_EXPECTED_PROCESSES is not None:
        topology_desc = f"DB_EXPECTED_PROCESSES={settings.DB_EXPECTED_PROCESSES} (explicit override)"
    else:
        topology_desc = (
            f"BACKEND_MAX_REPLICAS={settings.BACKEND_MAX_REPLICAS} x "
            f"{total_processes // max(settings.BACKEND_MAX_REPLICAS, 1)} process/replica (derived)"
        )
    logger.info(
        "database: adaptive pool sizing -- Postgres max_connections=%d "
        "superuser_reserved=%d, safety_margin=%d, %d process(es) expected "
        "(%s) -> pool_size=%d max_overflow=%d per process (%d total across "
        "all processes).",
        max_connections, reserved, settings.DB_CONNECTION_SAFETY_MARGIN,
        total_processes, topology_desc,
        pool_size, max_overflow, per_process_budget * total_processes,
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
_DB_POOL_SIZE, _DB_MAX_OVERFLOW = _compute_pool_sizing(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=_DB_POOL_SIZE,
    max_overflow=_DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
    connect_args={"connect_timeout": 10},
)

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
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    # Resolve the migration(s) THIS CODE expects to be current -- reads
    # straight from backend/alembic/versions/ as shipped in this image,
    # completely independent of whatever the database actually contains.
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    alembic_cfg = AlembicConfig(os.path.join(backend_dir, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    expected_heads = set(ScriptDirectory.from_config(alembic_cfg).get_heads())

    try:
        with engine.connect() as conn:
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
