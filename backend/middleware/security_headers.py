"""
middleware/security_headers.py
--------------------------------
Adds a small, standard set of defensive HTTP response headers to EVERY
response this app sends -- both the JSON API AND the static frontend
(HTML/CSS/JS) it now serves directly from the same process (see main.py's
module docstring for why there's no separate nginx layer anymore). This
is part of the general security review ("check for securities from all
points") rather than one of the five Operations & Observability
requirements, but it's a well-known, cheap hardening step that a
beginner dev should know about, so it's included here with full
explanations of what each header does and why it's safe to add
unconditionally.

  X-Content-Type-Options: nosniff
      Stops a browser from trying to "guess" (sniff) a different content
      type than the one we declared (e.g. treating a JSON error response as
      HTML and executing it). Without this, some older browsers' MIME
      sniffing could theoretically be abused for XSS if an attacker could
      control part of a response body.

  X-Frame-Options: DENY
      Prevents this app's responses from ever being embedded inside an
      <iframe> on another site (a "clickjacking" defense).

  Referrer-Policy: strict-origin-when-cross-origin
      Stops the browser from leaking the FULL request URL (which could
      contain sensitive path segments or query params) in the `Referer`
      header when a user follows a link away from this app to a different
      origin. Only the origin (scheme+host) is sent cross-origin.

  Permissions-Policy: geolocation=(), microphone=(), camera=()
      Explicitly tells the browser this app does not use these sensitive
      browser APIs, so it should refuse to grant them even if some future
      bug or third-party script tries to request them.

  Content-Security-Policy
      Used to live at the nginx reverse-proxy layer only (see this repo's
      git history / the old nginx/default.conf.template), tuned against
      the frontend's ACTUAL resource usage: same-origin scripts/styles
      only, plus the Google Fonts stylesheet + font host. Now that this
      backend serves the HTML/JS/CSS directly (no nginx in front of it
      anymore), the policy moved here instead so it still applies. See
      each directive's inline comment below for the reasoning -- unchanged
      from the original nginx version.

  Strict-Transport-Security (HSTS)
      Only added once settings.is_production is true (see config.py) --
      this tells browsers "always use HTTPS for this domain from now on",
      which only makes sense once you're actually serving over real HTTPS
      (e.g. Render terminates TLS in front of this app automatically).
      Browsers ignore this header entirely over plain HTTP, so it's inert
      -- not wrong, just pointless -- in local development.
"""

from starlette.types import ASGIApp, Receive, Scope, Send

from config import settings

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)

# FastAPI's own /docs (Swagger UI) and /redoc pages don't ship their own
# JS/CSS -- they load swagger-ui-bundle.js / redoc.standalone.js and their
# stylesheets from the jsdelivr CDN. The strict CSP above (`script-src
# 'self'`) would silently block every one of those, leaving a blank page
# with no visible error except in the browser console. Only these two
# doc-viewer paths get this relaxed policy; every other response
# (including /openapi.json itself, and the whole app's real frontend) gets
# the strict one above. Only reachable at all when settings.ENABLE_API_DOCS
# is true (see main.py's docs_url/redoc_url).
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https://cdn.jsdelivr.net https://fastapi.tiangolo.com; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)
_DOCS_PATHS = {"/docs", "/redoc"}

_SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"strict-origin-when-cross-origin",
    b"permissions-policy": b"geolocation=(), microphone=(), camera=()",
}
_HSTS = {b"strict-transport-security": b"max-age=63072000; includeSubDomains"}


class SecurityHeadersMiddleware:
    """Pure ASGI middleware that stamps the headers above onto every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        csp = _DOCS_CSP if path in _DOCS_PATHS else _CSP

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_SECURITY_HEADERS.items())
                headers.append((b"content-security-policy", csp.encode("ascii")))
                if settings.is_production:
                    headers.extend(_HSTS.items())
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
