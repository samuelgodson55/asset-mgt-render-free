"""
middleware/security_headers.py
--------------------------------
Adds a small, standard set of defensive HTTP response headers to EVERY
response this API sends. This is part of the general security review
("check for securities from all points") rather than one of the five
Operations & Observability requirements, but it's a well-known, cheap
hardening step that a beginner dev should know about, so it's included here
with full explanations of what each header does and why it's safe to add
unconditionally to a JSON API like this one.

  X-Content-Type-Options: nosniff
      Stops a browser from trying to "guess" (sniff) a different content
      type than the one we declared (e.g. treating a JSON error response as
      HTML and executing it). Without this, some older browsers' MIME
      sniffing could theoretically be abused for XSS if an attacker could
      control part of a response body.

  X-Frame-Options: DENY
      Prevents this API's responses from ever being embedded inside an
      <iframe> on another site (a "clickjacking" defense). This API only
      returns JSON anyway, but it costs nothing to set and protects the
      /docs Swagger UI too (which IS renderable HTML).

  Referrer-Policy: strict-origin-when-cross-origin
      Stops the browser from leaking the FULL request URL (which could
      contain sensitive path segments or query params) in the `Referer`
      header when a user follows a link away from this app to a different
      origin. Only the origin (scheme+host) is sent cross-origin.

  Permissions-Policy: geolocation=(), microphone=(), camera=()
      Explicitly tells the browser this app does not use these sensitive
      browser APIs, so it should refuse to grant them even if some future
      bug or third-party script tries to request them.

WHAT THIS DOES **NOT** COVER
-------------------------------
This does NOT set a `Content-Security-Policy` (CSP) header here:
  - This backend is a pure JSON API -- it never returns HTML/CSS/JS for a
    browser to render, so a CSP here would do nothing useful. The actual
    frontend (the HTML/JS/CSS nginx serves) gets a real CSP, tuned against
    its exact CDN/script/font usage, at the reverse-proxy layer instead --
    see `nginx/default.conf.template`'s `Content-Security-Policy` header
    and its accompanying comment for the full breakdown of each directive.
  - Nor does it set `Strict-Transport-Security` (HSTS): that only makes
    sense once you're actually serving this over HTTPS with a real
    certificate (e.g. behind a reverse proxy / load balancer in
    production) -- setting it over plain local HTTP in docker-compose
    would do nothing useful. Add it in your reverse proxy config
    (nginx/Caddy/your cloud load balancer) once you deploy with TLS -- see
    "Suggested Future Features" in README.md.
"""

from starlette.types import ASGIApp, Receive, Scope, Send

_SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"strict-origin-when-cross-origin",
    b"permissions-policy": b"geolocation=(), microphone=(), camera=()",
}


class SecurityHeadersMiddleware:
    """Pure ASGI middleware that stamps the headers above onto every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_SECURITY_HEADERS.items())
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
