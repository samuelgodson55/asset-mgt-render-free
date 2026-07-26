"""
middleware/request_context.py
-------------------------------
Request Correlation IDs (Operations & Observability requirement #2).

WHAT THIS SOLVES (beginner-friendly explanation)
--------------------------------------------------
Imagine a Manager reports "checkout failed for me around 2:15pm". Your
production logs from that minute might contain hundreds of interleaved
lines from dozens of concurrent requests -- good luck finding the 4-5 lines
that belong to THAT ONE request.

A "correlation ID" (a.k.a. "request ID" or "trace ID") is a random token
generated the moment a request arrives, attached to every log line produced
while handling it, AND echoed back to the caller in an `X-Request-ID`
response header. Now:
  - The frontend/API caller can show that ID in an error message ("Something
    went wrong. Reference: b3f1c2..."), and a support agent can ask the user
    for it.
  - You can grep/filter your log aggregator for that exact ID and see the
    complete, precisely-scoped story of that one request -- nothing more,
    nothing less.
  - If this API ever calls out to another internal service, forwarding the
    same ID lets you trace a single user action across multiple services.

HOW IT WORKS HERE
-------------------
This is plain ASGI middleware (not `BaseHTTPMiddleware`, which has known
streaming/performance caveats) so it stays lightweight and beginner-legible:
  1. On each incoming HTTP request, look for an `X-Request-ID` header. If
     the caller (or an upstream load balancer/proxy) already supplied one,
     reuse it -- that lets a request ID stay consistent across multiple
     hops in a bigger system. Otherwise, generate a fresh UUID4.
  2. Store it in `logging_config.request_id_var` (a `contextvars.ContextVar`)
     for the lifetime of this request -- see logging_config.py's module
     docstring for exactly how every log line then picks it up
     automatically.
  3. Call the next layer of the app (eventually reaching your route).
  4. Before the response goes out, add the SAME id back as the
     `X-Request-ID` response header, so the caller can see/log it too.
  5. Reset the ContextVar afterwards so it can never "leak" into a
     different, unrelated request handled later on the same worker.
"""

import uuid

from opentelemetry import trace
from starlette.types import ASGIApp, Receive, Scope, Send

from logging_config import request_id_var

REQUEST_ID_HEADER = "x-request-id"


class RequestContextMiddleware:
    """Pure ASGI middleware -- see module docstring above for the flow."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Not an HTTP request (e.g. a lifespan/startup event) -- nothing
            # for this middleware to do, just pass it straight through.
            await self.app(scope, receive, send)
            return

        # Look for an incoming X-Request-ID header (case-insensitive; ASGI
        # headers are a list of (bytes, bytes) tuples).
        incoming_id = None
        for raw_key, raw_value in scope.get("headers", []):
            if raw_key.decode("latin-1").lower() == REQUEST_ID_HEADER:
                incoming_id = raw_value.decode("latin-1")
                break

        request_id = incoming_id or str(uuid.uuid4())
        token = request_id_var.set(request_id)

        # Tie this request's log-correlation ID to its trace, in BOTH
        # directions: telemetry.py's LoggingInstrumentor already stamps
        # otelTraceID/otelSpanID onto every log line (see that module's
        # docstring); this stamps request_id onto the span itself, so
        # looking up a request_id a user reported ("Reference: b3f1...")
        # in your tracing backend's UI is just as possible as looking it
        # up in your log aggregator. `is_recording()` is false whenever
        # OTEL_ENABLED is off (settings.OTEL_ENABLED -- see telemetry.py)
        # or this span was sampled out, so this is a no-op in the
        # overwhelmingly common case and never worth an extra
        # settings-import here just to skip it.
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("app.request_id", request_id)

        async def send_with_request_id(message):
            # Inject the header into the outgoing response's "start" event
            # (that's the ASGI event that carries HTTP status + headers).
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # Always reset, even if the request raised -- otherwise this
            # worker's NEXT unrelated request could start with a stale
            # request_id still set from this one.
            request_id_var.reset(token)
