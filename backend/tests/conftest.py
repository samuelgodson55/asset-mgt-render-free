"""
tests/conftest.py
------------------
Shared pytest fixtures for the whole backend test suite -- formalizes the
manual "throwaway SQLite database" pattern documented in README.md's
"Testing Your Changes" section into something that runs automatically in
CI (see .github/workflows/ci.yml's `Pytest` step).

WHY THESE ENVIRONMENT VARIABLES ARE SET *BEFORE* ANY APP IMPORT
------------------------------------------------------------------
config.py builds `settings` (a Pydantic `BaseSettings` instance) once, at
IMPORT time, by reading `os.environ`. Every other backend module
(database.py, security.py, main.py, ...) imports that same `settings`
object and never re-reads the environment itself. That means the test
environment variables below MUST be set before the very first
`import database` / `import main` anywhere in the test session -- doing it
inside a fixture, after some other test file already imported `main`,
would be too late. Pytest always collects/imports `conftest.py` before any
test module in the same directory, so setting them at MODULE level here
(not inside a fixture function) is what guarantees the ordering.

WHY A FRESH SQLITE FILE PER TEST, NOT ONE SHARED DATABASE
------------------------------------------------------------
Every service function in this app calls `db.commit()` itself (see e.g.
services/checkout_service.py), which rules out the common "wrap each test
in one transaction and roll it back" pattern -- an inner `commit()` would
already end that outer transaction. Recreating a brand-new, freshly-seeded
SQLite file for every single test function is simpler to reason about and
completely immune to state leaking between tests, at the (small, since
SQLite is fast and this schema is tiny) cost of re-running `init_db()` +
`seed_db()` once per test.

WHY THE `engine`/`SessionLocal` SWAP INSTEAD OF JUST SETTING DATABASE_URL
---------------------------------------------------------------------------
database.py's real `engine` is built with
`connect_args={"connect_timeout": 10}` (see its own module docstring) --
that's a psycopg2/Postgres-only connect argument. SQLite's DBAPI
(`sqlite3.connect()`) doesn't accept a `connect_timeout` keyword at all and
raises `TypeError` if you try. So instead of pointing `DATABASE_URL` at a
SQLite file and letting database.py build its normal Postgres-flavored
engine around it, the `db_engine` fixture below builds its OWN
SQLite-appropriate engine and swaps it into `database.engine` /
`database.SessionLocal` for the duration of one test (`monkeypatch`
automatically restores the originals afterward) -- `database.init_db()`
and `database.seed_db()` both read those two names as module globals at
call time, so they transparently pick up the swap.
"""

import os
import sys
from pathlib import Path

# --- Make `backend/` importable regardless of pytest's rootdir/cwd --------
# `pytest backend/tests` (see ci.yml) collects this file without `backend/`
# itself being on sys.path -- every module in this app does a plain
# `import models` / `from database import get_db` (not package-qualified),
# so without this, `import main` below would fail with ModuleNotFoundError.
BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# --- Test environment configuration (see module docstring) ----------------
# Deliberately harmless, throwaway values -- this process never talks to a
# real SMTP server, real Redis, or real Postgres. `setdefault` so a
# developer can still override any of these locally without editing this
# file. Note there's no SUPER_ADMIN_PASSWORD anymore -- the root admin's
# password now lives only in the `users` table (see security.py's module
# docstring), seeded here with the same well-known demo password
# database.py's seed_db() uses for local/dev (see _root_admin_demo_row()).
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-jwt-secret-key-do-not-use-in-production-0123456789")
os.environ.setdefault("SUPER_ADMIN_USERNAME", "superadmin")
os.environ.setdefault("NOTIFICATIONS_ENABLED", "false")
os.environ.setdefault("ENABLE_AUTO_BACKUP", "false")
os.environ.setdefault("ENABLE_API_DOCS", "false")
os.environ.setdefault("AUTO_INIT_DB", "true")
os.environ.setdefault("AUTO_SEED_DEMO_DATA", "true")
# Deliberately unreachable/fast-failing rather than pointed at a real Redis
# -- see middleware/rate_limit.py's "FAIL-OPEN ON REDIS ERRORS" section:
# login/rate-limiting still works correctly (just fails open) without a
# real broker running, which keeps this test suite dependency-free.
os.environ.setdefault("REDIS_URL", "redis://redis-not-available-in-tests:6379/0")
os.environ.setdefault("CELERY_BROKER_URL", "redis://redis-not-available-in-tests:6379/0")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://redis-not-available-in-tests:6379/0")

import pytest  # noqa: E402 -- see module docstring: must come after the os.environ.setdefault() calls above
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402

# Demo account credentials seeded by database.py's seed_db() -- kept here
# as named constants so every test file can log in as a given role without
# re-typing (and risking a typo in) the raw strings. See seed_db()'s own
# docstring for the full list this mirrors.
DEMO_USERS = {
    "admin": {"identifier": "r.adeyemi@corp.io", "password": "Admin123!"},
    "manager": {"identifier": "s.chen@corp.io", "password": "Manager123!"},
    "staff": {"identifier": "t.okafor@corp.io", "password": "Staff123!"},
    "staff2": {"identifier": "a.bello@corp.io", "password": "Staff123!"},
    "customer": {"identifier": "d.martins@customer.io", "password": "Customer123!"},
}
SUPER_ADMIN = {"identifier": os.environ["SUPER_ADMIN_USERNAME"], "password": "RootAdmin123!"}


@pytest.fixture()
def db_engine(tmp_path, monkeypatch):
    """
    Swaps `database.engine`/`database.SessionLocal` for a brand-new,
    file-based SQLite database unique to this one test (see module
    docstring for why). Yields a `sessionmaker` any test can use to open
    its own direct DB session for setup/assertions alongside the HTTP
    calls made through the `client` fixture.
    """
    db_path = tmp_path / "test.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "SessionLocal", TestSessionLocal)

    # Tests that only need `db_session` (no HTTP client) never trigger
    # main.py's `@app.on_event("startup")` handler -- that's the only place
    # `database.init_db()`/`database.seed_db()` normally get called (see
    # main.py's on_startup()). Call them directly here too so both kinds of
    # test get a fully created, seeded database; `seed_db()` is idempotent
    # (checks `users` is empty first) so this is harmless when the
    # `client` fixture's own startup event calls it again afterward.
    database.init_db()
    database.seed_db()

    yield TestSessionLocal

    test_engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """A single direct DB session for a test to set up fixtures or assert
    on rows the API doesn't return in full (e.g. checkout ids)."""
    session = db_engine()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_engine):
    """
    A FastAPI TestClient wired to the swapped SQLite database above.
    Using `with TestClient(app) as c` (rather than plain `TestClient(app)`)
    is what triggers main.py's `@app.on_event("startup")` handler, which is
    what actually calls `database.init_db()` + `database.seed_db()` -- see
    main.py's on_startup(). Also overrides the `get_db` FastAPI dependency
    directly, since every route depends on that exact function object (see
    api/*.py's `from database import get_db` + `Depends(get_db)`), so
    requests read/write through the SAME swapped SQLite database the
    `db_session` fixture above uses.
    """
    def _override_get_db():
        session = db_engine()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[database.get_db] = _override_get_db
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


def auth_headers(client: TestClient, identifier: str, password: str) -> dict:
    """Logs in via POST /api/auth/login and returns an Authorization header
    dict ready to spread into any subsequent request's `headers=`."""
    response = client.post("/api/auth/login", json={"identifier": identifier, "password": password})
    assert response.status_code == 200, f"Login failed for {identifier!r}: {response.status_code} {response.text}"
    token = response.json().get("access_token") or client.cookies.get("access_token")
    if token:
        return {"Authorization": f"Bearer {token}"}
    # POST /auth/login sets the token as an HttpOnly cookie rather than
    # returning it in the JSON body (see api/auth.py) -- the TestClient
    # instance already carries that cookie on every subsequent request it
    # makes, so an empty header dict is correct in that case too.
    return {}


@pytest.fixture()
def as_admin(client):
    """Convenience fixture: (client, headers) already logged in as the
    seeded demo Admin account."""
    headers = auth_headers(client, **DEMO_USERS["admin"])
    return client, headers


@pytest.fixture()
def as_manager(client):
    headers = auth_headers(client, **DEMO_USERS["manager"])
    return client, headers


@pytest.fixture()
def as_staff(client):
    headers = auth_headers(client, **DEMO_USERS["staff"])
    return client, headers


@pytest.fixture()
def as_customer(client):
    headers = auth_headers(client, **DEMO_USERS["customer"])
    return client, headers


@pytest.fixture()
def as_super_admin(client):
    headers = auth_headers(client, **SUPER_ADMIN)
    return client, headers
