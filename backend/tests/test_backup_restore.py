"""
tests/test_backup_restore.py
------------------------------
Real-service tests for services/backup_service.py -- the one module in
this app that fundamentally can't be exercised against conftest.py's
throwaway SQLite database (see test_migrations.py's own module docstring
for why: Alembic's SQLite dialect can't do everything this app's
migrations need), and that also needs a REAL Redis for its distributed
backup/restore locks (conftest.py's REDIS_URL is deliberately
unreachable -- see its own comment). So, same convention as
test_migrations.py and test_redbeat_scheduling.py: talk to real
PostgreSQL/Redis instances, and skip (not fail) if neither is reachable,
since CI always has both (see .github/workflows/ci.yml's service
containers) but a contributor running `pytest backend/tests` locally
without them shouldn't see a hard failure for it.

WHAT'S COVERED HERE
--------------------
1. Filename-collision avoidance (create_backup() picking a disambiguated
   name instead of silently overwriting an existing file with the same
   second-resolution timestamp).
2. The backup-vs-restore mutual-exclusion lock (a backup can't start
   while a restore holds the lock, and vice versa).
3. The auth-epoch forced-logout mechanism (a restore bumps
   AUTH_EPOCH_SETTING_KEY so every existing session is invalidated).
4. Partial-schema detection (_detect_schema_revision()) against a real,
   partially-migrated database, and end-to-end schema reconciliation as
   part of a real restore.
5. The credential-reconciliation bug fix
   (_reconcile_post_restore_credentials()): duplicates get the full
   pre-restore profile (not just the password), accounts missing from
   the restored backup are re-inserted instead of silently dropped, and
   restore-only accounts are left untouched.

WHY A REAL `alembic upgrade head` TO BUILD EACH TEST'S SCHEMA
------------------------------------------------------------------
Building the schema via `models.Base.metadata.create_all()` (like
conftest.py's SQLite fixture does) would skip 0010_partition_audit_logs.py
turning `audit_logs` into an actual partitioned table -- something only
real DDL (not SQLAlchemy's generic create_all) sets up, and something
_detect_schema_revision() explicitly checks for. Running the real
Alembic chain, exactly as test_migrations.py already does for its own
purposes, is what makes a restored dump here behave exactly like a
restored dump would in production, including seeding the one root
super_admin row via 0002_bootstrap_root_admin.py.
"""
import datetime
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

try:
    import redis as redis_lib
except ImportError:  # pragma: no cover - redis is in requirements.txt
    redis_lib = None

BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

PG_HOST = os.environ.get("TEST_POSTGRES_HOST", "localhost")
PG_PORT = os.environ.get("TEST_POSTGRES_PORT", "5432")
PG_USER = os.environ.get("TEST_POSTGRES_USER", "postgres")
PG_PASSWORD = os.environ.get("TEST_POSTGRES_PASSWORD", "postgres")

# A real, local Redis (never the module-level unreachable one conftest.py
# points DEFAULT REDIS_URL at for every other test file) -- dedicated DB
# index so this file's SET/DEL lock traffic can never collide with
# anything a developer's own Redis instance might already be using on
# db 0. Flushed before/after every test (see redis_client fixture).
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

pytestmark = pytest.mark.skipif(
    psycopg2 is None or redis_lib is None,
    reason="psycopg2 and redis must both be installed -- see requirements.txt",
)


def _admin_pg_connection():
    """Connection to the `postgres` maintenance database, used only to
    CREATE/DROP each test's own throwaway database -- see
    test_migrations.py's identical helper for the full rationale."""
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres",
            connect_timeout=5,
        )
    except psycopg2.OperationalError as exc:
        pytest.skip(
            f"No Postgres server reachable at {PG_HOST}:{PG_PORT} ({exc}). "
            "backup_service tests need a real Postgres instance (pg_dump/psql "
            "can't run against SQLite) -- see this file's module docstring. "
            "CI always has one available; start one locally to run this file."
        )
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def _check_redis_reachable():
    try:
        client = redis_lib.from_url(TEST_REDIS_URL, socket_connect_timeout=3)
        client.ping()
    except redis_lib.RedisError as exc:
        pytest.skip(
            f"No Redis server reachable at {TEST_REDIS_URL} ({exc}). backup_service's "
            "backup/restore locks need a real Redis (conftest.py's default REDIS_URL is "
            "deliberately unreachable, see its own comment) -- CI always has one; start "
            "one locally to run this file."
        )
    return client


@pytest.fixture()
def database_url():
    """A brand-new, uniquely-named Postgres database for this one test,
    dropped afterward -- identical pattern to test_migrations.py's own
    `database_url` fixture."""
    db_name = f"backup_restore_test_{uuid.uuid4().hex[:12]}"
    admin_conn = _admin_pg_connection()
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
        yield f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{db_name}"
    finally:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        admin_conn.close()


def _run_alembic(*args, database_url, bootstrap_password="TestBootstrap123!"):
    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "DATABASE_URL": database_url,
        "SUPER_ADMIN_USERNAME": "test_root_admin",
        "SUPER_ADMIN_NAME": "Test Root Admin",
        "ROOT_ADMIN_BOOTSTRAP_PASSWORD": bootstrap_password,
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result


@pytest.fixture()
def redis_client():
    """A real Redis client on a dedicated, flushed-before/after DB index
    -- see TEST_REDIS_URL above."""
    client = _check_redis_reachable()
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture()
def backup_env(tmp_path, monkeypatch, database_url, redis_client):
    """
    Wires up services/backup_service.py (and database.py, which several
    of its functions import and call directly) to talk to THIS test's
    real, freshly-migrated Postgres database and real, flushed Redis --
    the real-service equivalent of conftest.py's `db_engine` fixture.

    Builds the schema via a real `alembic upgrade head` (see module
    docstring for why, not `Base.metadata.create_all()`), which also
    seeds the one root super_admin row via 0002_bootstrap_root_admin.py
    -- most tests below don't need it directly, but a couple do (the
    Super-Admin-specific reconciliation behavior).
    """
    import config
    import database as database_module
    import services.backup_service as backup_service
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    _run_alembic("upgrade", "head", database_url=database_url)

    monkeypatch.setattr(config.settings, "DATABASE_URL", database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(config.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(config.settings, "REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setattr(config.settings, "BACKUP_GDRIVE_ENABLED", False)
    # backup_service caches its Redis client as a module global (see
    # _get_redis_client()) -- clear it so the next call rebuilds one
    # against the just-patched REDIS_URL above instead of reusing
    # whatever a previous test's client was pointed at.
    monkeypatch.setattr(backup_service, "_redis_client", None)

    test_engine = create_engine(database_url)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(database_module, "engine", test_engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSessionLocal)

    yield {
        "database_url": database_url,
        "engine": test_engine,
        "redis": redis_client,
        "backup_dir": str(tmp_path),
    }
    test_engine.dispose()


def _insert_user(engine, **overrides):
    """Inserts a minimal, valid `users` row directly via SQL (bypassing
    services/user_service.py entirely -- this file is testing
    backup_service, not user creation), returning its id."""
    from sqlalchemy import text as sa_text
    from security import hash_password

    fields = {
        "name": "Test User",
        "email": f"user-{uuid.uuid4().hex[:8]}@example.com",
        "username": None,
        "role": "staff",
        "password_hash": hash_password("Whatever123!"),
        "is_verified": True,
        "is_active": True,
        "totp_enabled": False,
        "department": None,
        "department_role": None,
    }
    fields.update(overrides)
    with engine.begin() as conn:
        row = conn.execute(
            sa_text(
                "INSERT INTO users (name, email, username, role, password_hash, is_verified, "
                "is_active, totp_enabled, department, department_role) "
                "VALUES (:name, :email, :username, :role, :password_hash, :is_verified, "
                ":is_active, :totp_enabled, :department, :department_role) RETURNING id"
            ),
            fields,
        ).mappings().first()
    return row["id"]


def _get_user_by_email(engine, email):
    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT * FROM users WHERE lower(email) = lower(:email)"), {"email": email},
        ).mappings().first()
    return dict(row) if row else None


def _insert_outsider(engine, **overrides):
    from sqlalchemy import text as sa_text
    fields = {
        "name": "Test Outsider", "email": f"outsider-{uuid.uuid4().hex[:8]}@example.com",
        "phone_number": None, "company": None, "is_deleted": False, "deleted_at": None,
        "converted_to_user_id": None,
    }
    fields.update(overrides)
    with engine.begin() as conn:
        row = conn.execute(
            sa_text(
                "INSERT INTO outsiders (name, email, phone_number, company, is_deleted, deleted_at, "
                "converted_to_user_id) VALUES (:name, :email, :phone_number, :company, :is_deleted, "
                ":deleted_at, :converted_to_user_id) RETURNING id"
            ),
            fields,
        ).mappings().first()
    return row["id"]


def _get_outsider(engine, outsider_id):
    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT * FROM outsiders WHERE id = :id"), {"id": outsider_id},
        ).mappings().first()
    return dict(row) if row else None


def _insert_asset_type(engine, **overrides):
    from sqlalchemy import text as sa_text
    fields = {
        "name": f"Test Asset {uuid.uuid4().hex[:8]}", "total_quantity": 10, "available_quantity": 10,
    }
    fields.update(overrides)
    with engine.begin() as conn:
        row = conn.execute(
            sa_text(
                "INSERT INTO asset_types (name, total_quantity, available_quantity) "
                "VALUES (:name, :total_quantity, :available_quantity) RETURNING id"
            ),
            fields,
        ).mappings().first()
    return row["id"]


def _get_asset_type(engine, asset_id):
    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT * FROM asset_types WHERE id = :id"), {"id": asset_id},
        ).mappings().first()
    return dict(row) if row else None


def _insert_checkout(engine, **overrides):
    from sqlalchemy import text as sa_text
    fields = {
        "asset_id": None, "user_id": None, "outsider_id": None, "quotation_id": None,
        "quantity": 1, "quantity_returned": 0, "checkout_date": None, "due_date": None,
        "returned_at": None, "status": "active", "is_outsourced": False,
        "outsourced_item_name": None, "outsourced_unit_price": None, "outsourced_source": None,
    }
    fields.update(overrides)
    cols = list(fields.keys())
    with engine.begin() as conn:
        row = conn.execute(
            sa_text(
                f"INSERT INTO asset_checkouts ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)}) RETURNING id"
            ),
            fields,
        ).mappings().first()
    return row["id"]


def _get_checkout(engine, checkout_id):
    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT * FROM asset_checkouts WHERE id = :id"), {"id": checkout_id},
        ).mappings().first()
    return dict(row) if row else None


def _insert_quotation(engine, **overrides):
    from sqlalchemy import text as sa_text
    fields = {
        "user_id": None, "created_at": datetime.datetime(2026, 1, 1),
        "updated_at": datetime.datetime(2026, 1, 1), "status": "draft",
        "reference_number": None, "submitted_at": None,
        "assigned_to_id": None, "assigned_outsider_id": None, "notes": None, "approved_at": None,
        "approved_by_id": None, "fulfilled_at": None, "fulfilled_by_id": None, "discount_percent": 0,
    }
    fields.update(overrides)
    cols = list(fields.keys())
    with engine.begin() as conn:
        row = conn.execute(
            sa_text(
                f"INSERT INTO quotations ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)}) RETURNING id"
            ),
            fields,
        ).mappings().first()
    return row["id"]


def _get_quotation(engine, quotation_id):
    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT * FROM quotations WHERE id = :id"), {"id": quotation_id},
        ).mappings().first()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# 1. Filename-collision avoidance
# ---------------------------------------------------------------------------


def test_create_backup_avoids_filename_collision(backup_env):
    """Two backups whose computed filename would land on the exact same
    second must not overwrite one another -- the second gets a `-2`
    suffix instead (see create_backup()'s own "BUG FIX" comment).

    Rather than trying to race two real create_backup() calls into the
    exact same wall-clock second (flaky), pre-compute the filename
    create_backup() is about to choose (same algorithm it uses
    internally) and pre-create a dummy file at that exact path first --
    deterministically forcing the collision create_backup() must detect
    and disambiguate around.
    """
    import datetime as dt
    import services.backup_service as backup_service
    import services.export_service as export_service

    backup_dir = backup_env["backup_dir"]

    now = dt.datetime.now(dt.timezone.utc)
    display_now = now.astimezone(export_service.DISPLAY_TZ)
    tz_label = "".join(ch for ch in display_now.tzname() if ch.isalnum()) or "TZ"
    expected_base_filename = f"snipeit_backup_{display_now.strftime('%Y%m%d_%H%M%S')}_{tz_label}.sql.gz"
    collision_path = os.path.join(backup_dir, expected_base_filename)

    sentinel_content = b"pre-existing file that must survive untouched"
    with open(collision_path, "wb") as f:
        f.write(sentinel_content)

    entry = backup_service.create_backup(triggered_by="manual")

    # create_backup() must have picked a DIFFERENT name than the one
    # that already existed -- never silently overwriting it.
    assert entry["filename"] != expected_base_filename
    assert os.path.exists(collision_path)
    with open(collision_path, "rb") as f:
        assert f.read() == sentinel_content, "pre-existing file at the colliding name must be untouched"

    new_path = os.path.join(backup_dir, entry["filename"])
    assert os.path.exists(new_path)
    assert os.path.getsize(new_path) > 0

    entries = backup_service.list_backups()
    assert entry["filename"] in {e["filename"] for e in entries}


# ---------------------------------------------------------------------------
# 2. Backup vs. restore mutual-exclusion lock
# ---------------------------------------------------------------------------


def test_backup_lock_blocks_restore_and_vice_versa(backup_env):
    """_acquire_restore_lock() must refuse to start while a backup holds
    BACKUP_LOCK_KEY, and a backup must refuse to start while a restore
    holds BACKUP_LOCK_KEY too -- which _restore_backup_impl() takes out
    for its own entire destructive window (see that function's own
    "ENTERPRISE HARDENING" comment on WHY it reuses the same lock
    restore's own RESTORE_LOCK_KEY doesn't cover on its own), the
    cross-check described in both functions' own docstrings."""
    import services.backup_service as backup_service

    # Direction 1: a backup running (BACKUP_LOCK_KEY held) must block a
    # new restore from starting at all.
    backup_token = uuid.uuid4().hex
    backup_service._acquire_backup_lock(backup_token)
    try:
        with pytest.raises(backup_service.RestoreInProgressError):
            backup_service._acquire_restore_lock(uuid.uuid4().hex)
    finally:
        backup_service._release_backup_lock(backup_token)

    # Direction 2: a restore running holds BOTH RESTORE_LOCK_KEY (via
    # restore_backup()) AND BACKUP_LOCK_KEY (via _restore_backup_impl's
    # own destructive-window lock, see its own comment on why it reuses
    # create_backup()'s lock rather than taking out a separate one) --
    # simulate that combined state and confirm a concurrent backup is
    # rejected because of the BACKUP_LOCK_KEY half specifically.
    restore_token = uuid.uuid4().hex
    backup_service._acquire_restore_lock(restore_token)
    backup_service._acquire_backup_lock(restore_token)
    try:
        with pytest.raises(backup_service.BackupInProgressError):
            backup_service._acquire_backup_lock(uuid.uuid4().hex)
    finally:
        backup_service._release_backup_lock(restore_token)
        backup_service._release_restore_lock(restore_token)


def test_second_concurrent_backup_is_rejected(backup_env):
    """A second create_backup() while one already holds the lock gets a
    clean BackupInProgressError, not a corrupted/overwritten dump."""
    import services.backup_service as backup_service

    token = uuid.uuid4().hex
    backup_service._acquire_backup_lock(token)
    try:
        with pytest.raises(backup_service.BackupInProgressError):
            backup_service._acquire_backup_lock(uuid.uuid4().hex)
    finally:
        backup_service._release_backup_lock(token)


def test_second_concurrent_restore_is_rejected(backup_env):
    """A second restore_backup() while one already holds the restore lock
    gets RestoreInProgressError instead of racing the first (see
    _acquire_restore_lock's own docstring on why two concurrent restores
    would corrupt the database)."""
    import services.backup_service as backup_service

    token = uuid.uuid4().hex
    backup_service._acquire_restore_lock(token)
    try:
        with pytest.raises(backup_service.RestoreInProgressError):
            backup_service._acquire_restore_lock(uuid.uuid4().hex)
    finally:
        backup_service._release_restore_lock(token)


# ---------------------------------------------------------------------------
# 3 & 4. Full round trip: auth-epoch forced logout + schema reconciliation
# ---------------------------------------------------------------------------


def test_restore_bumps_auth_epoch_and_reconciles_schema(backup_env):
    """
    End-to-end: create a real backup, then restore it, and confirm:
      - AUTH_EPOCH_SETTING_KEY (app_settings) is written with a fresh
        timestamp -- the mechanism deps.py's get_current_user() uses to
        force every existing session to log back in (see
        _restore_backup_impl's own "ENTERPRISE HARDENING" comment).
      - the post-restore schema status is fully at head (the migration
        chain built by `alembic upgrade head` in the backup_env fixture
        survives a full DROP SCHEMA / psql reload / re-migrate cycle
        intact).
    """
    import services.backup_service as backup_service
    from security import AUTH_EPOCH_SETTING_KEY
    from sqlalchemy import text as sa_text

    engine = backup_env["engine"]

    before_epoch = datetime.datetime.now(datetime.timezone.utc)

    entry = backup_service.create_backup(triggered_by="manual")
    filepath = os.path.join(backup_env["backup_dir"], entry["filename"])

    result = backup_service.restore_backup(filepath, take_safety_backup=False)

    assert result["schema_status"]["ready"] is True

    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT value FROM app_settings WHERE key = :key"),
            {"key": AUTH_EPOCH_SETTING_KEY},
        ).mappings().first()
    assert row is not None, "restore must set AUTH_EPOCH_SETTING_KEY so existing sessions are invalidated"
    epoch_value = datetime.datetime.fromisoformat(row["value"])
    assert epoch_value >= before_epoch, "auth epoch must be bumped to (at least) restore time, forcing re-login"

    status = backup_service.get_restore_status()
    assert status["status"] == "succeeded"


def test_detect_schema_revision_against_partially_migrated_database(database_url, monkeypatch):
    """
    _detect_schema_revision() must correctly identify how far a
    database's schema actually got by inspecting real columns/tables --
    not just assume "no alembic_version row" means either "brand new" or
    "fully current". Migrate only partway (up to 0007, deliberately
    BEFORE 0008 adds users.totp_enabled/0009 adds recovery_codes/0010
    partitions audit_logs/0011 adds password_reset_tokens) and confirm
    detection stops exactly there.
    """
    from sqlalchemy import create_engine
    import services.backup_service as backup_service

    _run_alembic("upgrade", "0007_split_contact_details", database_url=database_url)

    test_engine = create_engine(database_url)
    try:
        with test_engine.connect() as conn:
            detected = backup_service._detect_schema_revision(conn)
        assert detected == "0007_split_contact_details"
    finally:
        test_engine.dispose()

    # And a fully-migrated database should be detected at the actual
    # current head, including the post-password-recovery migrations. This
    # is important for restore reconciliation: stamping a current
    # create_all()-style schema at 0011 would replay 0012-0016 and can hit
    # DuplicateColumn/duplicate-index errors (notably asset_types.department
    # in 0015).
    _run_alembic("upgrade", "head", database_url=database_url)
    test_engine = create_engine(database_url)
    try:
        with test_engine.connect() as conn:
            detected_full = backup_service._detect_schema_revision(conn)
        assert detected_full == "0016_quotation_paid_status"
    finally:
        test_engine.dispose()


def test_restore_of_backup_from_older_schema_reconciles_up_to_head(backup_env, database_url):
    """
    Full "restore a genuinely old backup" scenario: take a backup from a
    database intentionally stopped at 0007 (predating
    users.totp_enabled/recovery_codes/partitioned audit_logs entirely),
    then restore it into the (currently fully-migrated, via backup_env's
    own fixture setup) target database. The restore must detect the
    dump's real, older schema shape and run the missing migrations on
    top of it, ending up fully at head -- not silently stamp it as
    already current (see _detect_schema_revision's own module docstring
    for the exact bug this guards against).
    """
    import services.backup_service as backup_service
    from sqlalchemy import inspect as sa_inspect

    # A second, throwaway database deliberately migrated only to 0007 --
    # this is what an "old backup" actually looks like.
    old_db_name = f"backup_restore_old_{uuid.uuid4().hex[:12]}"
    admin_conn = _admin_pg_connection()
    old_db_url = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{old_db_name}"
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{old_db_name}"')
        _run_alembic("upgrade", "0007_split_contact_details", database_url=old_db_url)

        # pg_dump the OLD (0007-era) database directly -- bypassing
        # create_backup() (which always dumps settings.DATABASE_URL, the
        # target database) since we specifically need a dump of the
        # *older* schema to restore.
        old_backup_path = os.path.join(backup_env["backup_dir"], "old_schema_backup.sql.gz")
        import gzip as gzip_module
        dump_result = subprocess.run(
            ["pg_dump", "--host", PG_HOST, "--port", PG_PORT, "--username", PG_USER,
             "--dbname", old_db_name, "--no-owner", "--no-privileges", "--format", "plain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PGPASSWORD": PG_PASSWORD}, timeout=120,
        )
        assert dump_result.returncode == 0, dump_result.stderr
        with gzip_module.open(old_backup_path, "wb") as gz_out:
            gz_out.write(dump_result.stdout)
    finally:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (old_db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{old_db_name}"')
        admin_conn.close()

    # Now restore that OLD-schema dump into backup_env's own (fully
    # migrated) target database.
    result = backup_service.restore_backup(old_backup_path, take_safety_backup=False)
    assert result["schema_status"]["ready"] is True

    with backup_env["engine"].connect() as conn:
        inspector = sa_inspect(conn)
        assert "recovery_codes" in inspector.get_table_names()
        assert "totp_enabled" in {c["name"] for c in inspector.get_columns("users")}


# ---------------------------------------------------------------------------
# 5. Credential-reconciliation bug fix: duplicates / reinserted / restore-only
# ---------------------------------------------------------------------------


def test_reconcile_duplicate_account_uses_full_pre_restore_profile(backup_env):
    """
    Case 1 (duplicates): an account present in both the pre-restore
    snapshot and the restored data gets its ENTIRE profile overwritten
    with the pre-restore (current) values -- not just password_hash, the
    old bug's behavior. Its `id` must never change (foreign keys already
    point at it).
    """
    import services.backup_service as backup_service
    from security import hash_password, verify_password

    engine = backup_env["engine"]
    email = "duplicate.user@example.com"
    user_id = _insert_user(
        engine, email=email, name="Old Name", department="Sales",
        password_hash=hash_password("OldPassword123!"),
    )

    # Pre-restore snapshot reflects the CURRENT, more-up-to-date profile
    # (name/department/password changed since the backup was taken).
    pre_restore_users = [{
        "id": user_id, "email": email, "email_lc": email.lower(),
        "name": "New Current Name", "phone_number": "555-0100", "username": "dup.user",
        "role": "staff", "password_hash": hash_password("NewCurrentPassword123!"),
        "is_verified": True, "is_active": True, "failed_login_attempts": 0,
        "locked_until": None, "totp_secret_encrypted": None, "totp_enabled": False,
        "is_deleted": False, "deleted_at": None, "purged_at": None,
        "department": "Engineering", "department_role": "Senior Engineer",
        "converted_to_outsider_id": None,
    }]

    result = backup_service._reconcile_post_restore_credentials(engine, pre_restore_users)

    assert result["users_reconciled"] == 1
    assert result["users_reinserted"] == 0

    row = _get_user_by_email(engine, email)
    assert row["id"] == user_id  # id preserved
    assert row["name"] == "New Current Name"
    assert row["department"] == "Engineering"
    assert row["department_role"] == "Senior Engineer"
    assert verify_password("NewCurrentPassword123!", row["password_hash"])
    assert not verify_password("OldPassword123!", row["password_hash"])


def test_reconcile_reinserts_missing_from_restore_account_preserving_id(backup_env):
    """
    BUG FIX -- case 2 (missing from restore): an account that existed
    pre-restore but has no row at all in the restored data (e.g. created/
    invited AFTER the backup was taken) must be re-inserted wholesale,
    not silently dropped. Its original id is preserved when free.
    """
    import services.backup_service as backup_service
    from security import hash_password, verify_password
    from sqlalchemy import text as sa_text

    engine = backup_env["engine"]

    # Simulate: this account existed pre-restore (id=99999, deliberately
    # far outside anything the restore itself created, so it's free),
    # but the "restored" `users` table has no row for it at all.
    email = "invited.after.backup@example.com"
    pre_restore_users = [{
        "id": 99999, "email": email, "email_lc": email.lower(),
        "name": "Invited After Backup", "phone_number": None, "username": "invited.user",
        "role": "manager", "password_hash": hash_password("TheirCurrentPassword123!"),
        "is_verified": True, "is_active": True, "failed_login_attempts": 0,
        "locked_until": None, "totp_secret_encrypted": None, "totp_enabled": False,
        "is_deleted": False, "deleted_at": None, "purged_at": None,
        "department": "Operations", "department_role": None, "converted_to_outsider_id": None,
    }]

    result = backup_service._reconcile_post_restore_credentials(engine, pre_restore_users)

    assert result["users_reinserted"] == 1
    assert result["users_reconciled"] == 0

    row = _get_user_by_email(engine, email)
    assert row is not None, "account must be re-inserted, not silently dropped"
    assert row["id"] == 99999  # original id preserved since it was free
    assert row["name"] == "Invited After Backup"
    assert row["role"] == "manager"
    assert row["department"] == "Operations"
    assert verify_password("TheirCurrentPassword123!", row["password_hash"])

    # The users_id_seq sequence must be bumped past the reinserted id --
    # otherwise the very next ordinary INSERT (relying on the sequence,
    # not an explicit id) could collide with this row.
    with engine.begin() as conn:
        new_id = conn.execute(
            sa_text(
                "INSERT INTO users (name, email, role, password_hash, is_verified, is_active, totp_enabled) "
                "VALUES ('Someone Else', 'someone.else@example.com', 'staff', 'x', true, true, false) "
                "RETURNING id"
            )
        ).scalar()
    assert new_id > 99999, "sequence must be bumped past the re-inserted id to avoid a future collision"


def test_reconcile_reinserts_missing_user_with_id_collision_falls_back_to_new_id(backup_env):
    """
    Same missing-from-restore case, but the pre-restore account's
    original id is already taken by a genuinely DIFFERENT account the
    restore brought back. The account must still be re-inserted (not
    dropped) -- just with a fresh id instead of forcing the collision.
    """
    import services.backup_service as backup_service
    from security import hash_password

    engine = backup_env["engine"]

    # An unrelated account that the restore brought back, occupying the
    # id our missing account used to have.
    unrelated_id = _insert_user(engine, email="unrelated.restored@example.com")

    email = "collides.on.id@example.com"
    pre_restore_users = [{
        "id": unrelated_id, "email": email, "email_lc": email.lower(),
        "name": "Collides On Id", "phone_number": None, "username": None,
        "role": "staff", "password_hash": hash_password("Whatever123!"),
        "is_verified": True, "is_active": True, "failed_login_attempts": 0,
        "locked_until": None, "totp_secret_encrypted": None, "totp_enabled": False,
        "is_deleted": False, "deleted_at": None, "purged_at": None,
        "department": None, "department_role": None, "converted_to_outsider_id": None,
    }]

    result = backup_service._reconcile_post_restore_credentials(engine, pre_restore_users)
    assert result["users_reinserted"] == 1

    row = _get_user_by_email(engine, email)
    assert row is not None, "account must still be re-inserted despite the id collision"
    assert row["id"] != unrelated_id, "must not steal/overwrite the unrelated restored account's id"

    # The unrelated account itself must be completely untouched.
    unrelated_row = _get_user_by_email(engine, "unrelated.restored@example.com")
    assert unrelated_row["id"] == unrelated_id
    assert unrelated_row["name"] == "Test User"


def test_reconcile_leaves_restore_only_accounts_untouched(backup_env):
    """Case 3: an account present ONLY in the restored data (deleted
    before this restore ran) is left completely as-is -- not touched,
    not removed."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    restore_only_id = _insert_user(
        engine, email="restore.only@example.com", name="Restore Only Original",
    )

    result = backup_service._reconcile_post_restore_credentials(engine, pre_restore_users=[])
    assert result == {
        "users_reconciled": 0, "users_reinserted": 0, "super_admins_reset": 0, "super_admins_revoked": 0, "preserved_user_ids": [],
    }

    row = _get_user_by_email(engine, "restore.only@example.com")
    assert row["id"] == restore_only_id
    assert row["name"] == "Restore Only Original"


def test_reconcile_writes_audit_log_with_all_three_counts(backup_env):
    """The single audit-log row this function writes must reflect both
    reconciled AND reinserted accounts, not just the old
    (reconciled-only) behavior."""
    import services.backup_service as backup_service
    from sqlalchemy import text as sa_text
    from security import hash_password

    engine = backup_env["engine"]
    dup_email = "dup.for.audit@example.com"
    dup_id = _insert_user(engine, email=dup_email)
    missing_email = "missing.for.audit@example.com"

    pre_restore_users = [
        {
            "id": dup_id, "email": dup_email, "email_lc": dup_email.lower(),
            "name": "Dup Current", "phone_number": None, "username": None,
            "role": "staff", "password_hash": hash_password("Whatever123!"),
            "is_verified": True, "is_active": True, "failed_login_attempts": 0,
            "locked_until": None, "totp_secret_encrypted": None, "totp_enabled": False,
            "is_deleted": False, "deleted_at": None, "purged_at": None,
            "department": None, "department_role": None, "converted_to_outsider_id": None,
        },
        {
            "id": 55555, "email": missing_email, "email_lc": missing_email.lower(),
            "name": "Missing Current", "phone_number": None, "username": None,
            "role": "staff", "password_hash": hash_password("Whatever123!"),
            "is_verified": True, "is_active": True, "failed_login_attempts": 0,
            "locked_until": None, "totp_secret_encrypted": None, "totp_enabled": False,
            "is_deleted": False, "deleted_at": None, "purged_at": None,
            "department": None, "department_role": None, "converted_to_outsider_id": None,
        },
    ]

    result = backup_service._reconcile_post_restore_credentials(engine, pre_restore_users)
    assert result["users_reconciled"] == 1
    assert result["users_reinserted"] == 1

    with engine.connect() as conn:
        audit_row = conn.execute(
            sa_text(
                "SELECT details FROM audit_logs WHERE action = 'RESTORE_CREDENTIAL_RECONCILIATION' "
                "ORDER BY timestamp DESC LIMIT 1"
            )
        ).mappings().first()
    assert audit_row is not None
    assert "1 account(s)" in audit_row["details"]
    assert missing_email in audit_row["details"]


def test_reconcile_restore_revokes_backup_only_super_admin_and_preserves_current_root(backup_env):
    """A restore may contain an older second root account, but the current
    root that existed immediately before restore is authoritative and the
    backup-only super_admin must be revoked."""
    import services.backup_service as backup_service
    from security import hash_password, SUPER_ADMIN_ROLE
    from sqlalchemy import text as sa_text
    import subprocess
    import os
    from pathlib import Path

    engine = backup_env["engine"]
    backend_dir = str(Path(__file__).resolve().parent.parent)

    # The normal head schema enforces one root. Drop only the final invariant
    # for this test so we can model the older restored database that contained
    # a second backup root, then put the invariant back afterwards.
    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "DATABASE_URL": backup_env["database_url"],
        "SUPER_ADMIN_USERNAME": "test_root_admin",
        "SUPER_ADMIN_NAME": "Test Root Admin",
        "ROOT_ADMIN_BOOTSTRAP_PASSWORD": "TestBootstrap123!",
    }
    result = subprocess.run(
        ["python", "-m", "alembic", "downgrade", "0016_quotation_paid_status"],
        cwd=backend_dir, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr

    current_email = "current.root@example.com"
    with engine.begin() as conn:
        current_id = conn.execute(
            sa_text(
                "UPDATE users SET email = :email, username = :username, name = :name "
                "WHERE role = :role RETURNING id"
            ),
            {
                "email": current_email, "username": "current.root",
                "name": "Current Root", "role": SUPER_ADMIN_ROLE,
            },
        ).scalar()
    assert current_id is not None

    backup_email = "old.root.from.backup@example.com"
    backup_id = _insert_user(
        engine,
        email=backup_email,
        username="old.root",
        role=SUPER_ADMIN_ROLE,
        name="Old Backup Root",
    )

    pre_restore_users = [{
        "id": current_id, "email": current_email, "email_lc": current_email.lower(),
        "name": "Current Root", "phone_number": None, "username": "current.root",
        "role": SUPER_ADMIN_ROLE, "password_hash": hash_password("CurrentPassword123!"),
        "is_verified": True, "is_active": True, "failed_login_attempts": 0,
        "locked_until": None, "totp_secret_encrypted": "current-secret", "totp_enabled": True,
        "is_deleted": False, "deleted_at": None, "purged_at": None,
        "department": None, "department_role": None, "converted_to_outsider_id": None,
    }]

    result = backup_service._reconcile_post_restore_credentials(engine, pre_restore_users)

    assert result["super_admins_revoked"] == 1
    assert result["super_admins_reset"] == 1

    current_row = _get_user_by_email(engine, current_email)
    assert current_row["id"] == current_id
    assert current_row["role"] == SUPER_ADMIN_ROLE
    assert current_row["is_active"] is True
    assert current_row["is_deleted"] is False
    assert current_row["totp_enabled"] is False
    assert current_row["totp_secret_encrypted"] is None

    backup_row = _get_user_by_email(engine, backup_email)
    assert backup_row["id"] == backup_id
    assert backup_row["role"] != SUPER_ADMIN_ROLE
    assert backup_row["is_active"] is False
    assert backup_row["is_deleted"] is True

    with engine.connect() as conn:
        count = conn.execute(
            sa_text("SELECT COUNT(*) FROM users WHERE role = :role"), {"role": SUPER_ADMIN_ROLE}
        ).scalar()
    assert count == 1

    # Reinstall the DB-level invariant and verify it can be applied cleanly
    # after the restore reconciliation has removed the duplicate root.
    result = subprocess.run(
        ["python", "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 6. Outsider reconciliation (duplicates / reinserted / restore-only)
# ---------------------------------------------------------------------------


def test_reconcile_outsiders_duplicate_uses_pre_restore_profile(backup_env):
    """Case 1: an outsider present in both is matched by id (outsiders
    have no unique email/phone -- see models.Outsider) and its entire
    pre-restore (current) profile wins."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    outsider_id = _insert_outsider(engine, name="Old Name", company="Old Co")

    pre_restore_outsiders = [{
        "id": outsider_id, "name": "New Current Name", "email": "current@example.com",
        "phone_number": "555-0199", "company": "New Co", "is_deleted": False,
        "deleted_at": None, "converted_to_user_id": None,
    }]

    result = backup_service._reconcile_post_restore_outsiders(engine, pre_restore_outsiders)
    assert result["outsiders_reconciled"] == 1
    assert result["outsiders_reinserted"] == 0
    assert result["preserved_outsider_ids"] == [outsider_id]

    row = _get_outsider(engine, outsider_id)
    assert row["name"] == "New Current Name"
    assert row["company"] == "New Co"


def test_reconcile_outsiders_reinserts_missing_outsider_preserving_id(backup_env):
    """Case 2: an outsider added after the backup was taken (no row in
    restored data) is re-inserted wholesale, id preserved when free."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    pre_restore_outsiders = [{
        "id": 88888, "name": "Added After Backup", "email": "added.after@example.com",
        "phone_number": None, "company": "Acme", "is_deleted": False,
        "deleted_at": None, "converted_to_user_id": None,
    }]

    result = backup_service._reconcile_post_restore_outsiders(engine, pre_restore_outsiders)
    assert result["outsiders_reinserted"] == 1
    assert result["preserved_outsider_ids"] == [88888]

    row = _get_outsider(engine, 88888)
    assert row is not None
    assert row["name"] == "Added After Backup"


def test_reconcile_outsiders_leaves_restore_only_untouched(backup_env):
    """Case 3: an outsider present only in the restored backup is left
    completely as-is."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    restore_only_id = _insert_outsider(engine, name="Restore Only")

    result = backup_service._reconcile_post_restore_outsiders(engine, pre_restore_outsiders=[])
    assert result == {"outsiders_reconciled": 0, "outsiders_reinserted": 0, "preserved_outsider_ids": []}

    row = _get_outsider(engine, restore_only_id)
    assert row["name"] == "Restore Only"


# ---------------------------------------------------------------------------
# 7. Asset-activity reconciliation: checkouts and quotations
# ---------------------------------------------------------------------------


def test_reconcile_checkout_duplicate_uses_pre_restore_state_and_recalculates_stock(backup_env):
    """Case 1: a checkout that exists both pre- and post-restore for a
    PRESERVED user has its mutable fields (status/quantity_returned/
    returned_at) updated to the pre-restore (current) value -- e.g. a
    return the user already made must not un-happen -- and the asset's
    cached available_quantity is recalculated afterward."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    user_id = _insert_user(engine, email="checkout.owner@example.com")
    asset_id = _insert_asset_type(engine, total_quantity=5, available_quantity=3)
    checkout_id = _insert_checkout(
        engine, asset_id=asset_id, user_id=user_id, quantity=2, quantity_returned=0, status="active",
    )

    # Pre-restore (current) reality: the user already returned the item.
    pre_restore_checkouts = [{
        "id": checkout_id, "asset_id": asset_id, "user_id": user_id, "outsider_id": None,
        "quotation_id": None, "quantity": 2, "quantity_returned": 2,
        "checkout_date": None, "due_date": None, "returned_at": datetime.datetime(2026, 1, 10),
        "status": "returned", "is_outsourced": False, "outsourced_item_name": None,
        "outsourced_unit_price": None, "outsourced_source": None,
    }]

    result = backup_service._reconcile_post_restore_asset_activity(
        engine, pre_restore_checkouts, [], [], [], [user_id], [],
    )
    assert result["checkouts_reconciled"] == 1
    assert result["checkouts_reinserted"] == 0

    row = _get_checkout(engine, checkout_id)
    assert row["status"] == "returned"
    assert row["quantity_returned"] == 2
    assert row["asset_id"] == asset_id  # identity field untouched

    # Nothing is active for this asset anymore -> available_quantity back
    # up to total_quantity (recalculated, not left at the stale backup value).
    asset_row = _get_asset_type(engine, asset_id)
    assert asset_row["available_quantity"] == 5


def test_reconcile_reinserts_missing_checkout_for_preserved_user(backup_env):
    """Case 2: a checkout made by a preserved user AFTER the backup was
    taken (no row in restored data at all) is re-inserted, and the
    asset's stock is recalculated to reflect it."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    user_id = _insert_user(engine, email="new.checkout.owner@example.com")
    asset_id = _insert_asset_type(engine, total_quantity=5, available_quantity=5)

    pre_restore_checkouts = [{
        "id": 12345, "asset_id": asset_id, "user_id": user_id, "outsider_id": None,
        "quotation_id": None, "quantity": 2, "quantity_returned": 0,
        "checkout_date": datetime.datetime(2026, 1, 15), "due_date": None, "returned_at": None,
        "status": "active", "is_outsourced": False, "outsourced_item_name": None,
        "outsourced_unit_price": None, "outsourced_source": None,
    }]

    result = backup_service._reconcile_post_restore_asset_activity(
        engine, pre_restore_checkouts, [], [], [], [user_id], [],
    )
    assert result["checkouts_reinserted"] == 1

    row = _get_checkout(engine, 12345)
    assert row is not None
    assert row["quantity"] == 2
    assert row["status"] == "active"

    asset_row = _get_asset_type(engine, asset_id)
    assert asset_row["available_quantity"] == 3  # 5 - 2 outbound


def test_reconcile_skips_checkout_whose_asset_no_longer_exists(backup_env):
    """A missing-from-restore checkout referencing an asset that itself
    doesn't exist post-restore must be skipped, never force-inserted
    with a dangling FK."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    user_id = _insert_user(engine, email="orphan.checkout.owner@example.com")

    pre_restore_checkouts = [{
        "id": 54321, "asset_id": 999999, "user_id": user_id, "outsider_id": None,
        "quotation_id": None, "quantity": 1, "quantity_returned": 0,
        "checkout_date": None, "due_date": None, "returned_at": None,
        "status": "active", "is_outsourced": False, "outsourced_item_name": None,
        "outsourced_unit_price": None, "outsourced_source": None,
    }]

    result = backup_service._reconcile_post_restore_asset_activity(
        engine, pre_restore_checkouts, [], [], [], [user_id], [],
    )
    assert result["checkouts_reinserted"] == 0
    assert result["checkouts_skipped"] == 1
    assert any("999999" in d for d in result["skipped_details"])
    assert _get_checkout(engine, 54321) is None


def test_reconcile_ignores_checkout_for_non_preserved_user(backup_env):
    """A checkout whose owner is NOT in preserved_user_ids (e.g. a
    backup-only account) is left exactly as the restored backup has it
    -- not reconciled, not reinserted."""
    import services.backup_service as backup_service

    engine = backup_env["engine"]
    backup_only_user_id = _insert_user(engine, email="backup.only@example.com")
    asset_id = _insert_asset_type(engine)
    checkout_id = _insert_checkout(engine, asset_id=asset_id, user_id=backup_only_user_id, status="active")

    pre_restore_checkouts = [{
        "id": checkout_id, "asset_id": asset_id, "user_id": backup_only_user_id, "outsider_id": None,
        "quotation_id": None, "quantity": 1, "quantity_returned": 1,
        "checkout_date": None, "due_date": None, "returned_at": None,
        "status": "returned", "is_outsourced": False, "outsourced_item_name": None,
        "outsourced_unit_price": None, "outsourced_source": None,
    }]

    # backup_only_user_id is deliberately NOT in preserved_user_ids.
    result = backup_service._reconcile_post_restore_asset_activity(
        engine, pre_restore_checkouts, [], [], [], [], [],
    )
    assert result["checkouts_reconciled"] == 0
    assert result["checkouts_reinserted"] == 0

    row = _get_checkout(engine, checkout_id)
    assert row["status"] == "active"  # restored backup's own value, untouched


def test_reconcile_quotation_duplicate_replaces_items_wholesale(backup_env):
    """Case 1 for quotations: a preserved user's quote gets its mutable
    fields (status/notes/discount) updated to the current value, and its
    item list wholesale-replaced with the pre-restore (current) cart
    contents."""
    import services.backup_service as backup_service
    from sqlalchemy import text as sa_text

    engine = backup_env["engine"]
    user_id = _insert_user(engine, email="quote.owner@example.com")
    asset_id_1 = _insert_asset_type(engine)
    asset_id_2 = _insert_asset_type(engine)
    quotation_id = _insert_quotation(engine, user_id=user_id, status="draft", notes="old note")
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "INSERT INTO quotation_items (quotation_id, asset_id, quantity, start_date, due_date, added_at) "
                "VALUES (:qid, :aid, 1, '2026-01-01', '2026-01-05', '2026-01-01')"
            ),
            {"qid": quotation_id, "aid": asset_id_1},
        )

    pre_restore_quotations = [{
        "id": quotation_id, "user_id": user_id, "created_at": datetime.datetime(2026, 1, 1),
        "updated_at": datetime.datetime(2026, 1, 20), "status": "submitted",
        "reference_number": "QT-000001", "submitted_at": datetime.datetime(2026, 1, 20),
        "assigned_to_id": None, "assigned_outsider_id": None, "notes": "current note",
        "approved_at": None, "approved_by_id": None, "fulfilled_at": None, "fulfilled_by_id": None,
        "discount_percent": 10,
    }]
    pre_restore_items = [{
        "id": 1, "quotation_id": quotation_id, "asset_id": asset_id_2, "quantity": 3,
        "start_date": datetime.date(2026, 1, 2), "due_date": datetime.date(2026, 1, 6),
        "added_at": datetime.datetime(2026, 1, 2),
    }]

    result = backup_service._reconcile_post_restore_asset_activity(
        engine, [], pre_restore_quotations, pre_restore_items, [], [user_id], [],
    )
    assert result["quotations_reconciled"] == 1

    row = _get_quotation(engine, quotation_id)
    assert row["status"] == "submitted"
    assert row["notes"] == "current note"
    assert float(row["discount_percent"]) == 10

    with engine.connect() as conn:
        items = conn.execute(
            sa_text("SELECT asset_id, quantity FROM quotation_items WHERE quotation_id = :qid"),
            {"qid": quotation_id},
        ).mappings().all()
    assert len(items) == 1
    assert items[0]["asset_id"] == asset_id_2
    assert items[0]["quantity"] == 3


def test_reconcile_reinserts_missing_quotation_with_items(backup_env):
    """Case 2 for quotations: a quote submitted by a preserved user after
    the backup, absent from the restored data entirely, is re-inserted
    with its line items."""
    import services.backup_service as backup_service
    from sqlalchemy import text as sa_text

    engine = backup_env["engine"]
    user_id = _insert_user(engine, email="new.quote.owner@example.com")
    asset_id = _insert_asset_type(engine)

    pre_restore_quotations = [{
        "id": 77001, "user_id": user_id, "created_at": datetime.datetime(2026, 1, 25),
        "updated_at": datetime.datetime(2026, 1, 25), "status": "draft",
        "reference_number": None, "submitted_at": None, "assigned_to_id": None,
        "assigned_outsider_id": None, "notes": None, "approved_at": None, "approved_by_id": None,
        "fulfilled_at": None, "fulfilled_by_id": None, "discount_percent": 0,
    }]
    pre_restore_items = [{
        "id": 501, "quotation_id": 77001, "asset_id": asset_id, "quantity": 4,
        "start_date": datetime.date(2026, 1, 25), "due_date": datetime.date(2026, 1, 30),
        "added_at": datetime.datetime(2026, 1, 25),
    }]

    result = backup_service._reconcile_post_restore_asset_activity(
        engine, [], pre_restore_quotations, pre_restore_items, [], [user_id], [],
    )
    assert result["quotations_reinserted"] == 1

    row = _get_quotation(engine, 77001)
    assert row is not None
    assert row["user_id"] == user_id

    with engine.connect() as conn:
        items = conn.execute(
            sa_text("SELECT asset_id, quantity FROM quotation_items WHERE quotation_id = :qid"),
            {"qid": 77001},
        ).mappings().all()
    assert len(items) == 1
    assert items[0]["quantity"] == 4


def test_reconcile_resolves_restore_only_username_collision(backup_env):
    """A backup-only account cannot block the current account's username."""
    import services.backup_service as backup_service
    from sqlalchemy import text as sa_text

    engine = backup_env["engine"]
    current_email = "current.username@example.com"
    current_id = _insert_user(engine, email=current_email, username="current.user")
    _insert_user(engine, email="backup.only@example.com", username="current.user2")

    # Simulate the restored backup: the backup-only user has stolen the
    # username that belongs to the current user.
    with engine.begin() as conn:
        conn.execute(
            sa_text("UPDATE users SET username = :u WHERE email = :e"),
            {"u": "current.user", "e": "backup.only@example.com"},
        )
        conn.execute(
            sa_text("UPDATE users SET username = :u WHERE email = :e"),
            {"u": "old.current", "e": current_email},
        )

    pre_restore_users = [{
        "id": current_id, "email": current_email, "email_lc": current_email.lower(),
        "name": "Current User", "phone_number": None, "username": "current.user",
        "role": "staff", "password_hash": None,
    }]
    # Build the snapshot with a real password hash without depending on the
    # helper's implementation details.
    from security import hash_password
    pre_restore_users[0]["password_hash"] = hash_password("CurrentPassword123!")
    pre_restore_users[0].update({
        "is_verified": True, "is_active": True, "failed_login_attempts": 0,
        "locked_until": None, "totp_secret_encrypted": None, "totp_enabled": False,
        "is_deleted": False, "deleted_at": None, "purged_at": None,
        "department": None, "department_role": None, "converted_to_outsider_id": None,
    })

    result = backup_service._reconcile_post_restore_credentials(engine, pre_restore_users)
    assert result["users_reconciled"] == 1
    assert result["username_conflicts_resolved"] == 1

    with engine.connect() as conn:
        rows = conn.execute(
            sa_text("SELECT email, username FROM users WHERE email IN (:a, :b) ORDER BY email"),
            {"a": current_email, "b": "backup.only@example.com"},
        ).mappings().all()
    by_email = {r["email"]: r["username"] for r in rows}
    assert by_email[current_email] == "current.user"
    assert by_email["backup.only@example.com"] != "current.user"
    assert by_email["backup.only@example.com"].startswith("__restore_conflict_")


def test_reconcile_handles_swapped_current_usernames(backup_env):
    """Username swaps between backup and current state must not hit the unique index."""
    import services.backup_service as backup_service
    from security import hash_password
    from sqlalchemy import text as sa_text

    engine = backup_env["engine"]
    a = "swap.a@example.com"
    b = "swap.b@example.com"
    aid = _insert_user(engine, email=a, username="backup.b")
    bid = _insert_user(engine, email=b, username="backup.a")

    snapshots = []
    for uid, email, username in [(aid, a, "current.a"), (bid, b, "current.b")]:
        snapshots.append({
            "id": uid, "email": email, "email_lc": email.lower(), "name": email,
            "phone_number": None, "username": username, "role": "staff",
            "password_hash": hash_password("CurrentPassword123!"), "is_verified": True,
            "is_active": True, "failed_login_attempts": 0, "locked_until": None,
            "totp_secret_encrypted": None, "totp_enabled": False, "is_deleted": False,
            "deleted_at": None, "purged_at": None, "department": None,
            "department_role": None, "converted_to_outsider_id": None,
        })

    # Force the restored rows into the opposite usernames to create a true
    # swap scenario.
    with engine.begin() as conn:
        conn.execute(sa_text("UPDATE users SET username = NULL WHERE id IN (:a, :b)"), {"a": aid, "b": bid})
        conn.execute(sa_text("UPDATE users SET username = :u WHERE id = :id"), {"u": "current.b", "id": aid})
        conn.execute(sa_text("UPDATE users SET username = :u WHERE id = :id"), {"u": "current.a", "id": bid})

    result = backup_service._reconcile_post_restore_credentials(engine, snapshots)
    assert result["users_reconciled"] == 2

    with engine.connect() as conn:
        rows = conn.execute(
            sa_text("SELECT id, username FROM users WHERE id IN (:a, :b) ORDER BY id"),
            {"a": aid, "b": bid},
        ).mappings().all()
    assert rows[0]["username"] == "current.a"
    assert rows[1]["username"] == "current.b"
