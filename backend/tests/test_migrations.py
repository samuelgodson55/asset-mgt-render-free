"""
tests/test_migrations.py
-------------------------
Runs the REAL `alembic` CLI (not `Base.metadata.create_all()`, which every
other test in this suite uses via database.py's init_db()) against a
throwaway, real PostgreSQL database, to answer the question the rest of
the suite can't: does `alembic upgrade head` actually work end-to-end, and
does 0002_bootstrap_root_admin.py do the right thing, in a process as
close to a real production deploy as we can get?

WHY POSTGRES HERE AND NOT SQLITE (LIKE EVERY OTHER TEST FILE)
----------------------------------------------------------------
0001_baseline_schema.py adds several foreign keys via a separate
`op.create_foreign_key(...)` (an ALTER TABLE ... ADD CONSTRAINT, run after
the referenced tables all exist, to avoid circular-dependency ordering
problems at CREATE TABLE time). SQLite's Alembic dialect has no support
for ALTER-ing constraints onto an existing table at all (it requires
"batch mode" / a copy-and-recreate strategy this project doesn't use) --
so the migration chain itself can only be run for real against Postgres,
which is what production actually uses anyway (see docker-compose.yml /
DATABASE_URL). This is also why this file needs a running `postgresql`
service in CI (see .github/workflows/ci.yml) instead of the zero-setup
SQLite file every other test in this suite gets away with.

WHY A SEPARATE SUBPROCESS INSTEAD OF THE `client`/`db_session` FIXTURES
------------------------------------------------------------------------
Every other test file drives the app through database.py's init_db()
(`Base.metadata.create_all()`), which -- deliberately, see database.py's
own docstring -- is a totally different code path from Alembic. That's
exactly why it can't tell us whether the migration chain itself (schema +
the root admin bootstrap data migration) is actually consistent and
runnable. Shelling out to the real `alembic` console script, against a
real Postgres database, with ENVIRONMENT/DATABASE_URL/
ROOT_ADMIN_BOOTSTRAP_PASSWORD set via a plain `env=` dict (never touching
`os.environ` in this process, so it can't interact with conftest.py's
module-level settings for every other test), is the closest thing to
"would this work in the `migrate` Container Apps Job / Render's
`alembic upgrade head` step" that we can exercise in CI.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

try:
    import psycopg2
    import psycopg2.extensions
except ImportError:  # pragma: no cover - psycopg2-binary is in requirements.txt
    psycopg2 = None

BACKEND_DIR = str(Path(__file__).resolve().parent.parent)

# Same throwaway local Postgres server docker-compose.yml's `db` service
# and .github/workflows/ci.yml's Postgres service container both use --
# never a real/shared database. Overridable via env for a differently
# configured local Postgres.
PG_HOST = os.environ.get("TEST_POSTGRES_HOST", "localhost")
PG_PORT = os.environ.get("TEST_POSTGRES_PORT", "5432")
PG_USER = os.environ.get("TEST_POSTGRES_USER", "postgres")
PG_PASSWORD = os.environ.get("TEST_POSTGRES_PASSWORD", "postgres")

pytestmark = pytest.mark.skipif(
    psycopg2 is None, reason="psycopg2 not installed -- see requirements.txt"
)


def _admin_connection():
    """A connection to Postgres itself (the `postgres` maintenance
    database), used only to CREATE/DROP the one throwaway database each
    test gets -- never to the app's own database."""
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres",
            connect_timeout=5,
        )
    except psycopg2.OperationalError as exc:
        # No Postgres reachable -- expected for a contributor running
        # `pytest backend/tests` locally without one running (every other
        # test file in this suite needs nothing but SQLite). CI always has
        # one (see .github/workflows/ci.yml's `postgres` service
        # container), so this only ever skips locally, never in CI.
        pytest.skip(
            f"No Postgres server reachable at {PG_HOST}:{PG_PORT} ({exc}). "
            "This file needs a real Postgres instance -- see its module "
            "docstring for why SQLite won't do. Start one locally (e.g. "
            "`docker run -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16`) "
            "or just skip this file; CI always has one available."
        )
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


@pytest.fixture()
def database_url():
    """
    Creates a brand-new, empty, uniquely-named Postgres database for this
    one test, and drops it afterward -- the Postgres equivalent of every
    other test file's fresh-SQLite-file-per-test pattern (see
    conftest.py's `db_engine` fixture docstring), so nothing about
    Alembic's migration-history bookkeeping (`alembic_version` table) can
    leak between tests either.
    """
    db_name = f"migration_test_{uuid.uuid4().hex[:12]}"
    admin_conn = _admin_connection()
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
        yield f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{db_name}"
    finally:
        with admin_conn.cursor() as cur:
            # Terminate any lingering connections (e.g. a crashed
            # subprocess) before DROP DATABASE, which otherwise fails
            # with "database is being accessed by other users".
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        admin_conn.close()


def _run_alembic(*args, database_url, environment, bootstrap_password=None, extra_env=None):
    """
    Invokes `alembic <args>` as a real subprocess, exactly as a deploy
    pipeline would, with its own isolated environment (a plain dict we
    build here -- never `os.environ.update()`, so this can't leak into or
    be affected by any other test in the suite, including conftest.py's
    own module-level env setup for the FastAPI/SQLite test fixtures).
    """
    env = {
        **os.environ,
        "ENVIRONMENT": environment,
        "DATABASE_URL": database_url,
        "SUPER_ADMIN_USERNAME": "test_root_admin",
        "SUPER_ADMIN_NAME": "Test Root Admin",
    }
    if bootstrap_password is not None:
        env["ROOT_ADMIN_BOOTSTRAP_PASSWORD"] = bootstrap_password
    else:
        env.pop("ROOT_ADMIN_BOOTSTRAP_PASSWORD", None)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result


def _query_users(database_url):
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, username, role, password_hash, "
                "is_verified, is_active, is_deleted FROM users"
            )
            columns = [d.name for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _table_names(database_url):
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def test_upgrade_head_creates_full_schema(database_url):
    """
    The baseline schema migration alone (0001) must produce every table
    the app's models.py defines -- a sanity check that the migration chain
    itself isn't broken, independent of the root admin bootstrap.
    """
    result = _run_alembic("upgrade", "head", database_url=database_url, environment="development")
    assert result.returncode == 0, f"alembic upgrade head failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    tables = _table_names(database_url)
    for expected in ("users", "asset_types", "asset_checkouts", "audit_logs", "quotations", "app_settings"):
        assert expected in tables, f"Expected table '{expected}' missing after upgrade head: {tables}"


def test_upgrade_head_in_development_never_bootstraps_root_admin(database_url):
    """
    0002_bootstrap_root_admin.py must be a strict no-op outside of
    ENVIRONMENT=production -- local/dev/test get their root admin from
    database.py's seed_db() instead (see that migration's own docstring),
    so running the full migration chain against a fresh dev database must
    never insert a "super_admin" row nor print a one-time password nobody
    asked for.
    """
    result = _run_alembic("upgrade", "head", database_url=database_url, environment="development")
    assert result.returncode == 0, result.stderr

    users = _query_users(database_url)
    assert users == []
    assert "ROOT ADMIN" not in result.stderr


def test_upgrade_head_in_production_bootstraps_exactly_one_root_admin(database_url):
    """
    The core requirement: `alembic upgrade head` against a fresh production
    database inserts exactly one real `users` row with role=super_admin,
    using a caller-supplied bootstrap password, and that password is
    usable (verifiable) afterward -- i.e. it's a normal Argon2id hash, not
    a placeholder.
    """
    sys.path.insert(0, BACKEND_DIR)
    from security import verify_password  # local import: only needed here, after sys.path is set up

    result = _run_alembic(
        "upgrade", "head",
        database_url=database_url,
        environment="production",
        bootstrap_password="TestBootstrap123!",
    )
    assert result.returncode == 0, f"alembic upgrade head failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    users = _query_users(database_url)
    root_admins = [u for u in users if u["role"] == "super_admin"]
    assert len(root_admins) == 1, f"Expected exactly one super_admin row, found {len(root_admins)}: {users}"

    root_admin = root_admins[0]
    assert root_admin["username"] == "test_root_admin"
    assert root_admin["name"] == "Test Root Admin"
    assert root_admin["email"] == "test_root_admin@local"
    assert root_admin["is_active"] is True
    assert root_admin["is_verified"] is True
    assert root_admin["is_deleted"] is False

    # The password is a real, salted Argon2id hash that verifies against
    # the plaintext we supplied -- not a placeholder, and not stored in
    # plaintext anywhere in the row.
    assert root_admin["password_hash"] != "TestBootstrap123!"
    assert verify_password("TestBootstrap123!", root_admin["password_hash"])
    assert not verify_password("some-other-password", root_admin["password_hash"])

    # Using ROOT_ADMIN_BOOTSTRAP_PASSWORD should NOT print a one-time
    # generated password to the logs -- that path is only for when the
    # caller didn't supply one.
    assert "ROOT ADMIN ACCOUNT BOOTSTRAPPED" not in result.stderr


def test_upgrade_head_in_production_generates_password_when_none_supplied(database_url):
    """
    When ROOT_ADMIN_BOOTSTRAP_PASSWORD is left unset, the migration must
    generate a random password itself, hash it into the row, and print the
    plaintext to its own output exactly once -- and that printed password
    must actually be the one that got hashed in.
    """
    sys.path.insert(0, BACKEND_DIR)
    from security import verify_password

    result = _run_alembic(
        "upgrade", "head", database_url=database_url, environment="production", bootstrap_password=None,
    )
    assert result.returncode == 0, result.stderr
    assert "ROOT ADMIN ACCOUNT BOOTSTRAPPED" in result.stderr

    printed_password = None
    for line in result.stderr.splitlines():
        line = line.strip()
        if line.startswith("password:"):
            printed_password = line.split("password:", 1)[1].strip()
            break
    assert printed_password, f"Could not find printed password in migration output:\n{result.stderr}"

    users = _query_users(database_url)
    root_admin = next(u for u in users if u["role"] == "super_admin")
    assert verify_password(printed_password, root_admin["password_hash"])


def test_rerunning_upgrade_head_never_duplicates_root_admin(database_url):
    """
    Re-running `alembic upgrade head` against a database that's already at
    head (the normal, idempotent "deploy again with no new migrations"
    case) must not touch the root admin row at all, and definitely must
    never insert a second one.
    """
    first = _run_alembic(
        "upgrade", "head", database_url=database_url, environment="production", bootstrap_password="FirstRun123!",
    )
    assert first.returncode == 0, first.stderr

    second = _run_alembic(
        "upgrade", "head", database_url=database_url, environment="production", bootstrap_password="FirstRun123!",
    )
    assert second.returncode == 0, second.stderr

    users = _query_users(database_url)
    root_admins = [u for u in users if u["role"] == "super_admin"]
    assert len(root_admins) == 1, f"Re-running upgrade head duplicated the root admin row: {root_admins}"


def test_downgrade_and_reupgrade_round_trip_does_not_corrupt_schema(database_url):
    """
    Full round-trip: upgrade to head, downgrade all the way back to base,
    upgrade to head again. This is what a deploy pipeline effectively
    exercises over the app's lifetime (rollback-and-retry, or a
    fix-forward migration added later) -- it must never leave the
    database in a broken state, and the root admin bootstrap specifically
    must survive a downgrade/upgrade cycle without duplicating or
    orphaning data.
    """
    up1 = _run_alembic(
        "upgrade", "head", database_url=database_url, environment="production", bootstrap_password="RoundTrip123!",
    )
    assert up1.returncode == 0, up1.stderr
    assert len(_query_users(database_url)) == 1

    down = _run_alembic("downgrade", "base", database_url=database_url, environment="production")
    assert down.returncode == 0, down.stderr
    # Downgrading to `base` drops every table this app owns, including
    # `users` itself -- there's nothing left to query (Alembic's own
    # bookkeeping table, alembic_version, is the only thing that may
    # remain, which is expected/harmless).
    assert _table_names(database_url) <= {"alembic_version"}

    up2 = _run_alembic(
        "upgrade", "head", database_url=database_url, environment="production", bootstrap_password="RoundTrip123!",
    )
    assert up2.returncode == 0, up2.stderr

    users = _query_users(database_url)
    root_admins = [u for u in users if u["role"] == "super_admin"]
    assert len(root_admins) == 1, f"Downgrade/upgrade round-trip left {len(root_admins)} root admin rows: {root_admins}"

    tables = _table_names(database_url)
    for expected in ("users", "asset_types", "asset_checkouts", "audit_logs"):
        assert expected in tables


def test_downgrade_from_head_by_one_step_removes_only_the_root_admin_row(database_url):
    """
    Downgrading just the one root-admin-bootstrap migration (not the whole
    chain) must remove exactly that row and leave every other table/row
    (the schema itself) completely untouched.
    """
    up = _run_alembic(
        "upgrade", "head", database_url=database_url, environment="production", bootstrap_password="StepDown123!",
    )
    assert up.returncode == 0, up.stderr
    assert len(_query_users(database_url)) == 1
    tables_before = _table_names(database_url)

    down_one = _run_alembic(
        "downgrade", "0001_baseline_schema", database_url=database_url, environment="production",
    )
    assert down_one.returncode == 0, down_one.stderr

    assert _query_users(database_url) == []
    # The schema itself (every table) must be untouched by this one-step
    # data-migration downgrade -- only the row is gone, not the table.
    assert _table_names(database_url) == tables_before
