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
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, utc_now
import models
from security import hash_password, SUPER_ADMIN_ROLE
from config import settings

# Retrieve the connection string from the central `settings` object
# (backend/config.py), which itself reads DATABASE_URL from the
# environment (injected by docker-compose.yml from your git-ignored
# `.env` file) or falls back to a safe local-dev default.
DATABASE_URL = settings.DATABASE_URL

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
# -----------------------------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
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
        db.close()


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
                return {
                    "ready": False,
                    "reason": "Database has no 'alembic_version' table -- "
                              "'alembic upgrade head' has never been run against it.",
                    "expected_heads": sorted(expected_heads),
                    "current_heads": [],
                }
            current_heads = {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))}
    except Exception as exc:
        # Same "fail legibly, not with a raw traceback" reasoning as
        # main.py's on_startup() -- an unreachable database at readiness-
        # check time should read as "not ready yet", not crash the probe.
        return {
            "ready": False,
            "reason": f"Could not reach the database to check its migration state: {exc}",
            "expected_heads": sorted(expected_heads),
            "current_heads": [],
        }

    if not current_heads:
        return {
            "ready": False,
            "reason": "Database's 'alembic_version' table is empty -- "
                      "no migration has ever been recorded as applied.",
            "expected_heads": sorted(expected_heads),
            "current_heads": [],
        }

    if current_heads != expected_heads:
        return {
            "ready": False,
            "reason": "Database schema version does not match what this build of the code "
                      "expects -- run 'alembic upgrade head' before routing traffic to this image.",
            "expected_heads": sorted(expected_heads),
            "current_heads": sorted(current_heads),
        }

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
        laptop_pool = models.AssetType(name='MacBook Pro 14" M3 Pool', total_quantity=15, available_quantity=14, category="Engineering", price=1899.00)
        monitor_pool = models.AssetType(name="Dell UltraSharp U2723QE Monitor", total_quantity=40, available_quantity=39, category="Engineering", price=629.99)
        mouse_pool = models.AssetType(name="Logitech MX Master 3S", total_quantity=60, available_quantity=59, category="Operations", price=99.99)
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
