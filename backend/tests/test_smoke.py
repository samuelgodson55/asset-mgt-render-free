"""
tests/test_smoke.py
--------------------
A deliberately small smoke test suite, run by .github/workflows/ci.yml on
every push/PR against a real (throwaway) Postgres service container.

This isn't meant to be a full test suite for the app's business logic --
it exists to catch the class of mistake CI is best at catching cheaply:
"the app doesn't even boot", "a route 500s on the happy path", "the health
check is broken", etc. -- before those ever reach a deploy.
"""

import os

# NOTE: these two env vars must be set BEFORE `main` (and therefore
# `config.settings`) is imported below, since Pydantic Settings reads them
# once at import time. The CI workflow also sets DATABASE_URL/JWT_SECRET_KEY
# etc. as real environment variables for the same reason; the two here are
# just extra safety nets so this file also works if run locally without
# the full CI environment configured.
os.environ.setdefault("AUTO_SEED_DEMO_DATA", "true")
os.environ.setdefault("NOTIFICATIONS_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)


def test_health_check_ok():
    """GET /healthz -- see main.py. Also what render.yaml's healthCheckPath points at."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_system_health_ok():
    """GET /api/system/health -- the unauthenticated pinger-friendly equivalent, see api/system.py."""
    response = client.get("/api/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_frontend_index_served():
    """StaticFiles mount at "/" should serve frontend/index.html for the root path."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_unauthenticated_request_to_protected_route_is_rejected():
    """No Authorization header -> 401/403, never a 500 or an accidental 200."""
    response = client.get("/api/audit-logs")
    assert response.status_code in (401, 403)


def test_login_with_bad_credentials_is_rejected():
    """Wrong password against a seeded demo account should be a clean 401, not a 500."""
    response = client.post(
        "/api/auth/login",
        json={"identifier": "definitely-not-a-real-user@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_security_headers_present():
    """Spot-check a couple of headers from SecurityHeadersMiddleware are actually applied."""
    response = client.get("/healthz")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert "content-security-policy" in response.headers
