"""Contract tests that protect the assumptions made by chaos-test.sh.

These tests do not run the chaos experiment itself. They make sure future
refactors do not accidentally remove the direct backend readiness check, Redis
wait behavior, or ErrorBeacon outage safeguards that the experiment depends on.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHAOS = (ROOT / "scripts/chaos-test.sh").read_text()
CELERY = (ROOT / "backend/celery_app.py").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()


def test_backend_readiness_is_tested_inside_backend():
    # The chaos script deliberately probes the backend's private diagnostic
    # port directly, so nginx/SPA fallback can never hide a broken /readyz.
    assert 'curl -sS' in CHAOS
    assert '"$BACKEND_URL$path"' in CHAOS
    assert "wait_backend 503" in CHAOS
    assert "localhost:8080/readyz" not in CHAOS


def test_dedicated_worker_and_beat_wait_for_redis():
    assert "CELERY_BROKER_CONNECTION_MAX_RETRIES: ${CELERY_BROKER_CONNECTION_MAX_RETRIES:-none}" in COMPOSE
    assert "broker_connection_max_retries=(" in CELERY


def test_errorbeacon_outage_is_non_blocking():
    assert "TELEM_TIME=" in CHAOS and "value>1.5" in CHAOS


def test_chaos_uses_dedicated_direct_backend_port():
    assert "BACKEND_DIRECT_PORT" in CHAOS
    assert "BACKEND_URL" in CHAOS
    assert "http://127.0.0.1:${BACKEND_DIRECT_PORT}" in CHAOS


def test_readiness_does_not_share_request_pool():
    database = (ROOT / "backend/database.py").read_text()
    assert "readiness_engine = _create_readiness_engine()" in database
    assert "with readiness_engine.connect() as conn:" in database
    assert "statement_timeout=3000" in database
    assert "@lru_cache(maxsize=1)" in database


def test_git_bash_path_conversion_is_disabled():
    assert "MSYS_NO_PATHCONV=1" in CHAOS


def test_correlation_client_event_is_marked_as_chaos_test():
    telemetry = (ROOT / "backend/integrations/fastapi_errorbeacon.py").read_text()
    assert '"chaos_test"' in telemetry
    assert "context.get('test')" in telemetry
    assert '"status_code": None' in telemetry
