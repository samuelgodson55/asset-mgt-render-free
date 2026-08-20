from sqlalchemy.engine import make_url

from config import Settings


def test_pgbouncer_routing_preserves_azure_host_tls_and_credentials():
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


def test_local_pooler_host_is_used_without_changing_direct_database_url():
    settings = Settings(
        DATABASE_URL="postgresql://admin:secret@db:5432/asset_db",
        USE_PGBOUNCER=True,
        PGBOUNCER_HOST="pgbouncer",
    )
    assert make_url(settings.DATABASE_URL).host == "pgbouncer"
    assert make_url(settings.DATABASE_URL).port == 6432
    assert make_url(settings.DIRECT_DATABASE_URL).host == "db"
    assert make_url(settings.DIRECT_DATABASE_URL).port == 5432


def test_direct_mode_keeps_database_url_unchanged():
    settings = Settings(
        DATABASE_URL="postgresql://admin:secret@db:5432/asset_db",
        USE_PGBOUNCER=False,
    )
    assert settings.DATABASE_URL == settings.DIRECT_DATABASE_URL
