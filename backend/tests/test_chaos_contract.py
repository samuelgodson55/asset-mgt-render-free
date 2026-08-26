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


def test_errorbeacon_reference_integration_stays_in_sync():
    # errorbeacon/integrations/fastapi_errorbeacon.py is a REFERENCE copy
    # shipped for other teams integrating with ErrorBeacon (see its
    # README) -- nothing in this repo imports it. That means it can drift
    # silently: this test is the only thing standing between "we fixed a
    # bug here" and "we shipped the same bug again in the reference doc"
    # the next time someone follows it.
    #
    # Deliberately NOT a byte-for-byte diff: the reference copy legitimately
    # uses namespaced env var names (ERRORBEACON_URL vs URL) so a copied
    # integration doesn't collide with an existing app's globals. This
    # checks the BEHAVIORAL contract of report_client_event() instead --
    # its parameters, and that it actually forwards them -- which is what
    # actually matters for a team copying this file.
    live = (ROOT / "backend/integrations/fastapi_errorbeacon.py").read_text()
    reference = (ROOT / "errorbeacon/integrations/fastapi_errorbeacon.py").read_text()

    for source, label in [(live, "live"), (reference, "reference")]:
        assert "def report_client_event(" in source, f"{label} copy is missing report_client_event()"
        assert "request_id_correlated: bool = True" in source, (
            f"{label} copy's report_client_event() is missing the request_id_correlated "
            "parameter -- see backend/api/telemetry_api.py for why an uncorrelated "
            "request_id must never be silently presented as if it were causal."
        )
        assert "breadcrumb_request_id: str | None = None" in source, (
            f"{label} copy's report_client_event() is missing the breadcrumb_request_id "
            "parameter."
        )
        # Both parameters must actually be forwarded into the outgoing
        # payload's context, not just accepted and dropped.
        assert 'merged_context["request_id_correlated"] = request_id_correlated' in source
        assert 'merged_context["breadcrumb_request_id"] = breadcrumb_request_id' in source
