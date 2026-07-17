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
# backend/config.py's `Settings` class runs two `model_validator` startup
# checks (`_enforce_prod_jwt_secret` / `_enforce_prod_super_admin_password`)
# the INSTANT it's instantiated, which happens at module import time -- i.e.
# the moment this file did `from config import settings`. Those checks
# refuse to construct `Settings()` at all if ENVIRONMENT=production and
# JWT_SECRET_KEY/SUPER_ADMIN_PASSWORD aren't yet set to real values.
#
# That's exactly the right behavior for the actual app (main.py) -- it
# should never boot with forgeable secrets. But it made `alembic upgrade
# head` (and every other alembic subcommand) impossible to run before those
# unrelated app secrets existed: migrations only need DATABASE_URL, so
# requiring the JWT secret and Super Admin password just to connect and
# apply schema changes was a chicken-and-egg problem that failed with a
# cryptic pydantic ValidationError on every single invocation, regardless
# of the command.
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
config.set_main_option("sqlalchemy.url", db_settings.DATABASE_URL)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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
