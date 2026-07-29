import sys
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# -----------------------------------------------------------------------------
# Make sure Python can find our app modules (models.py, config.py) when
# Alembic is run from the backend/ directory -- this is what lets us import
# our REAL SQLAlchemy models below instead of redefining the schema by hand.
# -----------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Base  # noqa: E402  (import after sys.path tweak, on purpose)
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


# -----------------------------------------------------------------------------
# BUG FIX (was: `from config import settings`):
#
# backend/config.py's `Settings` class runs a `model_validator` startup
# check (`_enforce_prod_jwt_secret`) the INSTANT it's instantiated, which
# happens at module import time -- i.e. the moment this file did
# `from config import settings`. That check refuses to construct
# `Settings()` at all if ENVIRONMENT=production and JWT_SECRET_KEY isn't
# yet set to a real value.
#
# That's exactly the right behavior for the actual app (main.py) -- it
# should never boot with a forgeable session-signing secret. But it made
# `alembic upgrade head` (and every other alembic subcommand) impossible
# to run before that unrelated app secret existed: migrations only need
# DATABASE_URL (plus, for 0002_bootstrap_root_admin.py, a couple of plain
# non-secret env vars it reads directly via `os.environ`, not through this
# settings object -- see that file's own comment for why), so requiring
# the JWT secret just to connect and apply schema changes was a chicken-
# and-egg problem that failed with a cryptic pydantic ValidationError on
# every single invocation, regardless of the command.
#
# The fix: define a second, minimal settings class here that reads ONLY
# `DATABASE_URL` (same `.env` file support as the real one) and has none of
# `Settings`'s production-secret validators.
# -----------------------------------------------------------------------------
class _MigrationSettings(BaseSettings):
    DATABASE_URL: str = "postgresql://admin:supersecret@db:5432/asset_db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


db_settings = _MigrationSettings()

# Inject the real database URL (env var / .env file) instead of the
# placeholder left in alembic.ini. This is the "link env.py to the existing
# SQLAlchemy database models" step from the setup instructions in README.md.
#
# BUG FIX: `config.set_main_option()` ultimately calls Python's stdlib
# `configparser.ConfigParser.set()`, which by default uses
# `BasicInterpolation` -- ANY literal `%` character in the value raises
# `ValueError: invalid interpolation syntax`, not just `%(name)s`-style
# references. `infra/main.bicep`'s `databaseUrl` deliberately runs the
# Postgres password through `uriComponent()` so URL-unsafe characters from
# `openssl rand -base64 24` (`+`, `/`, `=`) can't break the URL's own
# syntax -- but percent-ENCODING a `+`/`/`/`=` produces literal `%2B`/`%2F`/
# `%3D` sequences, which is exactly the `%` that then breaks ConfigParser.
# Any password containing one of those three characters (which
# `openssl rand -base64` frequently produces) hits this on every single
# `alembic upgrade head` run.
#
# Fix: escape `%` as `%%` before handing the URL to `set_main_option()`.
# ConfigParser's interpolation un-escapes `%%` back to a single `%` on
# *read* (`get_main_option()` below, and `engine_from_config()`'s internal
# read in `run_migrations_online()`), so the URL alembic/SQLAlchemy actually
# see is the original, unmodified one -- this only works around
# ConfigParser's in-memory representation, it doesn't change the URL itself.
config.set_main_option("sqlalchemy.url", db_settings.DATABASE_URL.replace("%", "%%"))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# BUG FIX: `logging.config.fileConfig()` defaults to
# `disable_existing_loggers=True`. That flag doesn't just configure the
# loggers listed in alembic.ini's [loggers] section (root/sqlalchemy/
# alembic) -- it walks every logger already registered in
# `logging.Logger.manager.loggerDict` at the time this runs and sets
# `.disabled = True` on every ONE OF THEM that isn't in that ini file.
# `Logger.handle()` checks `self.disabled` before `callHandlers()`, so a
# disabled logger drops every record before any handler (including
# pytest's `caplog`) ever sees it, no matter what level the handler is
# listening at.
#
# `restore_backup()` (services/backup_service.py) calls
# `alembic.command.upgrade()`/`command.stamp()` *in-process* (not via a
# subprocess) as part of post-restore schema reconciliation, which runs
# this env.py -- and therefore this fileConfig() call -- inside the same
# Python interpreter as the rest of the test suite. By the time that
# happens, application modules like main.py and telemetry.py have
# already done `logging.getLogger(__name__)` at import time, so this call
# was silently disabling the "main" and "telemetry" loggers for the rest
# of that pytest session. Any later test asserting against
# `caplog.records` for those loggers (e.g.
# test_error_handling.py::test_unhandled_exception_is_logged_with_traceback,
# test_telemetry.py::test_setup_tracing_warns_with_no_exporter_configured)
# would then see zero records and fail -- but only when it happened to
# run in the same process *after* a test that exercises restore_backup(),
# which is exactly the "passes alone, fails combined" symptom this fixed.
#
# Fix: `disable_existing_loggers=False` -- this file only needs to ADD/
# configure the loggers alembic.ini defines; it has no business silencing
# loggers that belong to the rest of the application.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Point Alembic at our actual models' metadata so `alembic revision
# --autogenerate` can diff the live database against models.py and generate
# migration scripts automatically.
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
