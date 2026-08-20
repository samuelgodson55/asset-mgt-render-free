# Covers config.Settings' DATABASE_URL/DIRECT_DATABASE_URL rewriting when
# USE_PGBOUNCER is enabled -- the app always connects through
# DATABASE_URL, but a few call sites (e.g. Alembic migrations, the
# readiness engine) need DIRECT_DATABASE_URL to bypass the pooler and talk
# straight to Postgres. Two distinct topologies are exercised: a managed
# pooler that's just Azure Postgres itself (same host, only the port and
# TLS requirement change) vs. a self-hosted PgBouncer sidecar (a genuinely
# different host, and TLS is dropped for the local hop since traffic never
# leaves the Compose/VM network).
from sqlalchemy.engine import make_url

from config import Settings


def test_managed_pgbouncer_routing_preserves_azure_host_tls_and_credentials():
    # Azure's managed PgBouncer sits in front of the SAME hostname as the
    # database itself (just a different port), so routing through it must
    # NOT rewrite the host, must keep sslmode=require (Azure always
    # requires TLS), and must round-trip a URL-encoded password correctly.
    settings = Settings(
        ENVIRONMENT="production",
        DATABASE_URL="postgresql://user:p%40ss@db.example.azure.com:5432/asset_db?sslmode=require",
        USE_PGBOUNCER=True,
    )
    url = make_url(settings.DATABASE_URL)
    direct = make_url(settings.DIRECT_DATABASE_URL)
    assert url.host == direct.host
    assert url.port == 6432
    assert url.database == "asset_db"
    assert url.query.get("sslmode") == "require"
    assert url.password == "p@ss"


def test_self_hosted_pooler_disables_client_tls_without_changing_direct_database_url():
    # Self-hosted PgBouncer (docker-compose.yml's `pgbouncer` service) is a
    # genuinely different host from `db`. TLS is dropped for the
    # app->pgbouncer hop (both containers on the same private Docker
    # network), while DIRECT_DATABASE_URL must still point straight at
    # `db` with the original sslmode preserved, for callers that need to
    # bypass the pooler entirely (e.g. Alembic).
    settings = Settings(
        DATABASE_URL="postgresql://admin:secret@db:5432/asset_db?sslmode=require",
        USE_PGBOUNCER=True,
        PGBOUNCER_HOST="pgbouncer",
    )
    url = make_url(settings.DATABASE_URL)
    direct = make_url(settings.DIRECT_DATABASE_URL)

    assert url.host == "pgbouncer"
    assert url.port == 6432
    assert url.query.get("sslmode") == "disable"
    assert direct.host == "db"
    assert direct.port == 5432
    assert direct.query.get("sslmode") == "require"


def test_self_hosted_pooler_preserves_other_connection_options():
    # Only sslmode should be touched by the PgBouncer rewrite -- any other
    # query-string connection options the operator set (application_name,
    # connect_timeout, ...) must survive untouched.
    settings = Settings(
        DATABASE_URL=(
            "postgresql://admin:secret@db:5432/asset_db"
            "?sslmode=require&application_name=asset-api&connect_timeout=10"
        ),
        USE_PGBOUNCER=True,
        PGBOUNCER_HOST="pgbouncer",
    )
    url = make_url(settings.DATABASE_URL)
    assert url.query.get("sslmode") == "disable"
    assert url.query.get("application_name") == "asset-api"
    assert url.query.get("connect_timeout") == "10"


def test_direct_mode_keeps_database_url_unchanged():
    # With USE_PGBOUNCER off, DIRECT_DATABASE_URL is just an alias for
    # DATABASE_URL -- no rewriting happens at all.
    settings = Settings(
        DATABASE_URL="postgresql://admin:secret@db:5432/asset_db",
        USE_PGBOUNCER=False,
    )
    assert settings.DATABASE_URL == settings.DIRECT_DATABASE_URL
