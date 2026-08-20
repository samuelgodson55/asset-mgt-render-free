from sqlalchemy.engine import make_url

from config import Settings


def test_managed_pgbouncer_routing_preserves_azure_host_tls_and_credentials():
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
    settings = Settings(
        DATABASE_URL="postgresql://admin:secret@db:5432/asset_db",
        USE_PGBOUNCER=False,
    )
    assert settings.DATABASE_URL == settings.DIRECT_DATABASE_URL
