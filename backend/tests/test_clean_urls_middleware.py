"""
tests/test_clean_urls_middleware.py
-------------------------------------
Covers middleware/clean_urls.py -- the FastAPI-side half of "clean URLs"
(/admin instead of /admin.html), used only in the free-tier single-service
Render deployment where FastAPI itself serves the frontend (see
main.py's SERVE_FRONTEND section). The nginx-fronted deployment shape's
equivalent behavior is covered separately by nginx/test-config.sh (a
shell script, not a pytest test, since it needs to shell out to the real
`nginx` binary -- see that script's own header for why).

Deliberately builds a tiny, throwaway Starlette app here instead of
importing the real `main.app` -- main.py's app is a big singleton wired to
the database/Celery/every API router at IMPORT time (see conftest.py's
own module docstring for why environment-dependent singletons like that
are awkward to test against directly), and none of that is relevant to
what this middleware does. A minimal app with the same
CleanUrlsMiddleware + StaticFiles(html=True) mount shape as main.py's
real one is enough to prove the middleware itself is correct in
isolation, and runs faster besides.
"""

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from middleware.clean_urls import CleanUrlsMiddleware

_PAGES = {
    "index.html": "<html><body>login</body></html>",
    "admin.html": "<html><body>admin dashboard</body></html>",
    "manager.html": "<html><body>manager dashboard</body></html>",
    "staff.html": "<html><body>staff dashboard</body></html>",
    "customer.html": "<html><body>customer dashboard</body></html>",
}


@pytest.fixture()
def client():
    """A minimal FastAPI app: one real /api/* route (to prove the
    passthrough rule works), plus CleanUrlsMiddleware + a StaticFiles(html=True)
    mount over a throwaway directory of the 5 real page names -- the same
    shape main.py wires up for real when SERVE_FRONTEND is enabled."""
    with tempfile.TemporaryDirectory() as frontend_dir:
        for filename, content in _PAGES.items():
            with open(os.path.join(frontend_dir, filename), "w") as f:
                f.write(content)
        # A real file WITHOUT a clean-URL mapping (like js/auth.js) -- proves
        # ordinary static assets still resolve untouched.
        os.makedirs(os.path.join(frontend_dir, "js"))
        with open(os.path.join(frontend_dir, "js", "auth.js"), "w") as f:
            f.write("// noop")

        app = FastAPI()

        @app.get("/api/ping")
        def ping():
            return {"ok": True}

        app.add_middleware(CleanUrlsMiddleware)
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

        # follow_redirects=False so tests can assert on the 301 itself,
        # not wherever it eventually leads.
        with TestClient(app, follow_redirects=False) as c:
            yield c


# ---------------------------------------------------------------------------
# (1) Clean URLs resolve directly -- no redirect, no URL change.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected_snippet",
    [
        ("/", "login"),
        ("/admin", "admin dashboard"),
        ("/manager", "manager dashboard"),
        ("/staff", "staff dashboard"),
        ("/customer", "customer dashboard"),
    ],
)
def test_clean_url_serves_the_right_page(client, path, expected_snippet):
    response = client.get(path)
    assert response.status_code == 200
    assert expected_snippet in response.text


# ---------------------------------------------------------------------------
# (2) Old-style *.html links 301-redirect to their clean equivalent.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected_location",
    [
        ("/admin.html", "/admin"),
        ("/manager.html", "/manager"),
        ("/staff.html", "/staff"),
        ("/customer.html", "/customer"),
        ("/index.html", "/"),
    ],
)
def test_old_html_url_redirects_to_clean_url(client, path, expected_location):
    response = client.get(path)
    assert response.status_code == 301
    assert response.headers["location"] == expected_location


def test_html_redirect_preserves_query_string(client):
    response = client.get("/manager.html?foo=bar")
    assert response.status_code == 301
    assert response.headers["location"] == "/manager?foo=bar"


# ---------------------------------------------------------------------------
# (3) /api/* and other real static assets are left completely alone.
# ---------------------------------------------------------------------------
def test_api_routes_are_not_touched_by_the_middleware(client):
    response = client.get("/api/ping")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_ordinary_static_asset_still_resolves(client):
    response = client.get("/js/auth.js")
    assert response.status_code == 200


def test_unknown_clean_path_404s_rather_than_guessing(client):
    # No CLEAN_URL_MAP entry and no matching file on disk -- should 404,
    # not silently serve some unrelated page.
    response = client.get("/not-a-real-page")
    assert response.status_code == 404
