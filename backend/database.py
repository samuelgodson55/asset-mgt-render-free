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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, utc_now
import models
from security import hash_password
from config import settings

# Retrieve the connection string from the central `settings` object
# (backend/config.py), which itself reads DATABASE_URL from the
# environment (injected by docker-compose.yml from your git-ignored
# `.env` file) or falls back to a safe local-dev default.
DATABASE_URL = settings.DATABASE_URL

# Create the engine instance that handles network traffic to PostgreSQL
engine = create_engine(DATABASE_URL)

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

    NOTE: there's no "Super Admin" row here on purpose. The Super Admin is
    a single hardcoded identity configured via the SUPER_ADMIN_USERNAME/
    SUPER_ADMIN_PASSWORD environment variables (see config.py and
    security.py's super_admin_principal()) -- it's never a database row,
    so it can't be seeded, edited, or deleted like the accounts below.
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
        db.add_all([admin, manager, staff_1, staff_2, customer_1])
        db.commit()

        # --- Demo asset pools ----------------------------------------------------
        laptop_pool = models.AssetType(name='MacBook Pro 14" M3 Pool', total_quantity=15, available_quantity=14)
        monitor_pool = models.AssetType(name="Dell UltraSharp U2723QE Monitor", total_quantity=40, available_quantity=39)
        mouse_pool = models.AssetType(name="Logitech MX Master 3S", total_quantity=60, available_quantity=59)
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
