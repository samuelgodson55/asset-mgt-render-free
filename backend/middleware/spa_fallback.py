"""
middleware/spa_fallback.py
----------------------------
The React "Ledger" SPA's counterpart to middleware/clean_urls.py -- used
only when the free-tier single-service Render deployment (see
Dockerfile.render / main.py's "STATIC FRONTEND" section) is configured
with `FRONTEND_VARIANT=react` (see config.py's own docstring on that
setting) instead of the default legacy multi-page site.

WHY THIS EXISTS
A client-side route the React Router owns (e.g. `/checkouts`, `/assets/42`)
is NOT a real file on disk -- there is exactly one HTML file in a Vite
build, `index.html`, and every other "page" is rendered client-side after
that shell loads. Starlette's `StaticFiles(html=True)` mount (see main.py)
only auto-resolves a bare "/" to "index.html"; it has no concept of
"any path that isn't a real file should also fall back to index.html and
let client-side JS take over" -- request that mount for `/checkouts`
directly (a page refresh, a bookmark, a shared link) and it 404s.

nginx/default.react.conf.template's `location / { try_files $uri $uri/
/index.html; }` already solves exactly this for the multi-service/nginx
deployment shape (see that file's own comment on the SPA-fallback block).
This middleware is the same fallback, one layer up, for when FastAPI
itself is the one serving the SPA directly with no nginx in front of it.

HOW IT WORKS
Gated to plain GET/HEAD requests outside /api/*, /docs, /redoc,
/openapi.json (identical passthrough rule to CleanUrlsMiddleware -- those
are left completely alone and fall through to the routes/mount registered
after this middleware). For everything else: if the requested path
resolves to a REAL file under `frontend_dir` (a built JS/CSS asset, a
favicon, etc.), the request passes through untouched so the StaticFiles
mount serves it normally. If it does NOT correspond to a real file, the
path is rewritten in-place to "/" before it reaches the StaticFiles
mount, which then serves index.html exactly as if "/" had been requested
directly -- the browser's address bar keeps showing the original path
throughout (a rewrite, not a redirect), so a full-page load of a
client-side route works exactly like a client-side navigation to it would.

Unlike CleanUrlsMiddleware's small, explicit CLEAN_URL_MAP (deliberately
NOT a generic "try appending .html" rule, so it can never expose an
unintended file), this middleware's fallback IS intentionally generic --
that's the correct behavior for an SPA, where "no real file exists" is
supposed to mean "the client router owns this," not "this is missing."
A genuinely missing built asset (a typo'd `/assets/xyz.js` reference)
still ends up serving index.html rather than a real 404 -- the same
trade-off nginx's own `try_files ... /index.html;` makes, and standard
practice for SPA hosting generally.
"""

import os

from starlette.types import ASGIApp, Receive, Scope, Send

# Paths this middleware must never touch -- the API, and FastAPI's own
# interactive-docs routes (see main.py's docs/redoc/openapi.json handling).
# Kept identical to middleware/clean_urls.py's own constants on purpose --
# both middlewares must agree on what counts as "not the frontend's job."
_PASSTHROUGH_PREFIXES = ("/api",)
_PASSTHROUGH_EXACT = ("/docs", "/redoc", "/openapi.json")


class SpaFallbackMiddleware:
    """Pure ASGI middleware: rewrites any request that doesn't resolve to
    a real file under `frontend_dir` to "/", so the StaticFiles mount
    registered after this middleware serves index.html and the SPA's own
    client-side router can take over. See module docstring above."""

    def __init__(self, app: ASGIApp, frontend_dir: str) -> None:
        self.app = app
        self.frontend_dir = frontend_dir

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        if path in _PASSTHROUGH_EXACT or any(path.startswith(p) for p in _PASSTHROUGH_PREFIXES):
            await self.app(scope, receive, send)
            return

        if path != "/":
            # Strip the leading "/" and normalize away any "..", so this
            # can never be tricked into checking a path outside
            # frontend_dir -- os.path.isfile() below is only ever used to
            # answer "does this exact built asset exist", never to serve
            # a file directly (StaticFiles, registered after this
            # middleware, still owns the actual file read).
            relative = os.path.normpath(path.lstrip("/"))
            candidate = os.path.join(self.frontend_dir, relative)
            is_real_file = (
                not relative.startswith("..")
                and os.path.commonpath([os.path.abspath(candidate), os.path.abspath(self.frontend_dir)]) == os.path.abspath(self.frontend_dir)
                and os.path.isfile(candidate)
            )
            if not is_real_file:
                scope = dict(scope)
                scope["path"] = "/"

        await self.app(scope, receive, send)
