"""
middleware/clean_urls.py
-------------------------
Serves the static frontend at clean, extension-less URLs
(/admin, /manager, /staff, /customer, / for login) instead of raw
filenames (/admin.html, /manager.html, ...), and 301-redirects anyone who
still hits an old *.html URL (an old bookmark, a search-engine result, a
link pasted before this change) to its clean equivalent.

WHY THIS EXISTS
This only matters for the free-tier single-service Render deployment (see
Dockerfile.render / render-start.sh), where FastAPI itself serves the
frontend directly via a `StaticFiles(html=True)` mount (see main.py's
"STATIC FRONTEND" section) -- Starlette's StaticFiles only auto-resolves a
bare "/" to "index.html"; it has no concept of "/admin" meaning
"admin.html" for any other file. In the nginx-fronted deployment shape
(docker-compose.yml / a multi-service cloud deployment), the equivalent
rewrite/redirect lives in nginx/default.conf.template's `location /` and
`location ~ ^/(.+)\\.html$` blocks instead -- same behavior, different
layer, so both deployment shapes present identical URLs to the browser.

HOW IT WORKS
Two responsibilities, both gated to plain GET/HEAD requests outside
/api/*, /docs, /redoc, /openapi.json (those are left completely alone and
fall through to the routes/mount registered after this middleware):

  1. Request for "/admin.html" (etc.) -> HTTP 301 redirect to "/admin".
     Canonicalizes old links/bookmarks onto the new URL shape rather than
     silently serving duplicate content at two URLs (bad for any future
     SEO/analytics, and just confusing to have two URLs for one page).

  2. Request for "/admin" (etc., see CLEAN_URL_MAP below) -> rewritten
     in-place to "/admin.html" *before* it reaches the StaticFiles mount,
     which then serves that file completely normally. The browser's
     address bar keeps showing "/admin" throughout -- this is a rewrite,
     not a redirect, so there's no extra round-trip and no URL flash.

Deliberately a small, explicit map (not a generic "try appending .html to
anything" rule) so this can't accidentally expose some *other* .html file
that happens to exist under FRONTEND_DIR at a clean-sounding URL nobody
intended to publish.
"""

from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Clean URL -> actual file under settings.FRONTEND_DIR. Keep in sync with
# frontend/js/auth.js's PAGE_ACCESS_RULES/redirectByUserRole and
# frontend/js/auth-guard.js, which generate/consume these same clean paths.
CLEAN_URL_MAP = {
    "/": "index.html",
    "/admin": "admin.html",
    "/manager": "manager.html",
    "/staff": "staff.html",
    "/customer": "customer.html",
}

# Paths this middleware must never touch -- the API, and FastAPI's own
# interactive-docs routes (see main.py's docs/redoc/openapi.json handling).
_PASSTHROUGH_PREFIXES = ("/api",)
_PASSTHROUGH_EXACT = ("/docs", "/redoc", "/openapi.json")


class CleanUrlsMiddleware:
    """Pure ASGI middleware: rewrites clean URLs to their .html file, and
    redirects old .html URLs to their clean equivalent. See module
    docstring above for the full explanation."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        if path in _PASSTHROUGH_EXACT or any(path.startswith(p) for p in _PASSTHROUGH_PREFIXES):
            await self.app(scope, receive, send)
            return

        # (1) Old-style "/admin.html" -> 301 redirect to "/admin".
        if path.endswith(".html"):
            clean = path[: -len(".html")]
            if clean in ("", "/index"):
                clean = "/"
            query = scope.get("query_string", b"").decode("latin-1")
            location = f"{clean}?{query}" if query else clean
            response = RedirectResponse(url=location, status_code=301)
            await response(scope, receive, send)
            return

        # (2) Clean URL -> rewrite in place to the real filename, then let
        # the StaticFiles mount registered after this middleware serve it
        # exactly as if that filename had been requested directly.
        target = CLEAN_URL_MAP.get(path)
        if target is not None:
            scope = dict(scope)
            scope["path"] = f"/{target}"

        await self.app(scope, receive, send)
