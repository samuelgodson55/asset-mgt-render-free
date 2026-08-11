"""
tests/test_spa_fallback_middleware.py
----------------------------------------
Covers middleware/spa_fallback.py -- the React "Ledger" SPA counterpart to
middleware/clean_urls.py, used only in the free-tier single-service
Render deployment when FRONTEND_VARIANT=react (see main.py's
SERVE_FRONTEND section and config.py's FRONTEND_VARIANT docstring). The
nginx-fronted deployment shape's equivalent behavior
(`try_files $uri $uri/ /index.html;`) is covered separately by
nginx/test-config.sh.

Same "tiny throwaway Starlette app" approach as
test_clean_urls_middleware.py, for the same reason (main.app is a big
singleton wired to the database/Celery/every API router at IMPORT time --
none of that is relevant to what this middleware does in isolation).
"""

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from middleware.spa_fallback import SpaFallbackMiddleware


@pytest.fixture()
def client():
    """A minimal FastAPI app: one real /api/* route (to prove the
    passthrough rule works), plus SpaFallbackMiddleware + a
    StaticFiles(html=True) mount over a throwaway directory shaped like a
    real Vite build -- index.html, a content-hashed asset under assets/,
    and a favicon -- the same shape main.py wires up for real when
    SERVE_FRONTEND and FRONTEND_VARIANT=react are both enabled."""
    with tempfile.TemporaryDirectory() as frontend_dir:
        with open(os.path.join(frontend_dir, "index.html"), "w") as f:
            f.write("<html><body>ledger app shell</body></html>")
        with open(os.path.join(frontend_dir, "favicon.ico"), "w") as f:
            f.write("fake-favicon-bytes")
        os.makedirs(os.path.join(frontend_dir, "assets"))
        with open(os.path.join(frontend_dir, "assets", "index-a1b2c3.js"), "w") as f:
            f.write("// noop")

        app = FastAPI()

        @app.get("/api/ping")
        def ping():
            return {"ok": True}

        app.add_middleware(SpaFallbackMiddleware, frontend_dir=frontend_dir)
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# (1) Real files resolve normally, untouched by the fallback.
# ---------------------------------------------------------------------------
def test_root_serves_index_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "ledger app shell" in response.text


def test_real_hashed_asset_resolves_normally(client):
    response = client.get("/assets/index-a1b2c3.js")
    assert response.status_code == 200
    assert response.text == "// noop"


def test_favicon_resolves_normally(client):
    response = client.get("/favicon.ico")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# (2) A client-side route (react-router owns it, no real file) falls back
#     to index.html instead of 404ing -- the entire point of this
#     middleware.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/checkouts", "/assets/42", "/settings/notifications"])
def test_client_side_route_falls_back_to_index_html(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "ledger app shell" in response.text


def test_a_genuinely_missing_asset_also_falls_back_rather_than_404ing(client):
    # Same trade-off nginx's own try_files makes -- see module docstring.
    response = client.get("/assets/does-not-exist.js")
    assert response.status_code == 200
    assert "ledger app shell" in response.text


# ---------------------------------------------------------------------------
# (3) /api/* is left completely alone -- never falls back to index.html.
# ---------------------------------------------------------------------------
def test_api_routes_are_not_touched_by_the_middleware(client):
    response = client.get("/api/ping")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_unknown_api_path_404s_normally_not_falling_back(client):
    response = client.get("/api/this-route-does-not-exist")
    assert response.status_code == 404
    # And, crucially, is NOT the SPA shell -- the passthrough rule means
    # this 404 came from FastAPI's own routing, not a served index.html.
    assert "ledger app shell" not in response.text


# ---------------------------------------------------------------------------
# (4) Path-traversal safety: a crafted path can't escape frontend_dir via
#     the isfile() existence check.
# ---------------------------------------------------------------------------
def test_path_traversal_attempt_falls_back_rather_than_escaping(client):
    response = client.get("/../../etc/passwd")
    # Starlette normalizes ".." segments in the URL itself before this
    # middleware ever sees scope["path"], so this always lands on a
    # normal in-app path one way or another -- either the SPA shell (this
    # middleware's fallback) or Starlette's own 404, never a real file
    # read from outside frontend_dir.
    assert response.status_code in (200, 404)
    assert "root:" not in response.text
