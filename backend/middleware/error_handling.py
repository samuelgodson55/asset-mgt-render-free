"""
middleware/error_handling.py
------------------------------
Global safety net for unhandled exceptions (Operations & Observability --
"are errors logged and traceable?").

WHAT THIS SOLVES
-----------------
Before this middleware existed, an unhandled exception raised anywhere
inside a route/service/dependency (a bug, an unexpected third-party
error, a database constraint violation nothing already caught, etc.) had
no app-level handler at all, so it fell through to Starlette's own
default handling. That had three real problems:

  1. HARD TO CORRELATE: the traceback WAS logged (to "uvicorn.error",
     which logging_config.py already reconfigures to flow through our
     own JSON/text formatter -- see that file's module docstring), but
     the RESPONSE body the caller actually received was a generic
     plain-text "Internal Server Error" with no request/correlation ID
     in it. That breaks the whole point of `X-Request-ID`/
     `RequestContextMiddleware` (see middleware/request_context.py): a
     user or the frontend has no ID to hand back to support, and support
     has no easy way to jump from "here's the error the user saw" to
     "here's the exact log line(s) for that request".
  2. INCONSISTENT RESPONSE SHAPE: every OTHER error path in this API
     (every `raise HTTPException(...)` across api/*.py) returns JSON
     shaped like `{"detail": "..."}`. An unhandled exception fell
     through to a bare plain-text body instead -- frontend/js/api.js's
     error handling (which expects to read `error.detail` from JSON)
     had nothing usable to show the user for exactly the class of error
     most worth surfacing cleanly.
  3. CORS HEADERS SILENTLY MISSING: this is the subtle one.
     `@app.exception_handler(Exception)` looks like the obvious fix, but
     FastAPI/Starlette special-cases a handler registered for the bare
     `Exception` class (or status code 500): it gets wired into
     `ServerErrorMiddleware`, which is the OUTERMOST layer wrapping the
     whole app -- OUTSIDE `CORSMiddleware`, not inside it (see
     `starlette.applications.Starlette.build_middleware_stack`). When an
     exception reaches that point, it's already unwound past
     CORSMiddleware's own response post-processing, so the JSON error
     response ships with NO `Access-Control-Allow-Origin` header. A
     browser calling this API cross-origin (or through nginx) then
     reports an opaque, generic network error in DevTools with no
     visibility into the real 500 -- the exact same class of problem
     already solved for rate-limited responses by the
     CORSMiddleware/RateLimitMiddleware ordering below, just triggered a
     different way.

THE FIX: a plain ASGI middleware (same style as RequestContextMiddleware
-- not `BaseHTTPMiddleware`, for the same streaming/performance reasons
that file's docstring explains), added FIRST in main.py's middleware
stack so it ends up INNERMOST -- wrapping the actual route dispatch
directly, one layer inside RateLimitMiddleware, well inside
RequestContextMiddleware/CORSMiddleware/SecurityHeadersMiddleware. From
every other middleware's point of view, this one converts "an exception
came out of the app" into "a normal-looking JSONResponse came out of the
app" -- so CORS headers, security headers, and the X-Request-ID header
all get added completely normally, no special-casing needed anywhere
else. See main.py's "MIDDLEWARE STACK" comment for the full ordering.

This is deliberately a LAST-RESORT safety net, not a replacement for
specific `try/except` blocks with actionable handling (see main.py's
on_startup() database-connectivity catch, or api/backup_api.py's
`except Exception as exc: raise HTTPException(500, detail=f"Restore
failed: {exc}")` for cases where the caller genuinely benefits from
knowing WHAT failed) -- those stay exactly as they are. This only ever
fires for exceptions nobody anticipated, and its job is narrow: log the
full traceback with the request's correlation ID attached (automatic --
see logging_config.py's RequestIdLogFilter), and hand the caller back a
safe, generic, consistently-shaped error message plus that same
correlation ID -- never the raw exception message or traceback, which
could leak internals (a table/column name, a file path, a library
version) to whoever triggered it.
"""

import logging

import sqlalchemy.exc
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from logging_config import request_id_var
from integrations.fastapi_errorbeacon import report_exception

logger = logging.getLogger(__name__)

# RESILIENCE FIX ("pool exhausted under a burst -> every request gets a
# bare 500 for ~10s"): database.py sizes each process's connection pool
# (`pool_size`/`max_overflow`) to the target Postgres server's real,
# probed budget -- see database.py's _compute_pool_sizing() -- but ANY
# finite pool can still be briefly outrun by a large enough burst of
# truly concurrent requests (a legitimate traffic spike, a retry storm,
# a pentest throwing 50 requests at once). `pool_timeout` (settings.
# DB_POOL_TIMEOUT_SECONDS) makes that fail FAST with a clear
# `sqlalchemy.exc.TimeoutError` instead of hanging forever -- but until
# now that exception just fell through to the generic handler below,
# which reports it as an unanticipated 500 "unexpected error occurred"
# with a full ERROR-level traceback, indistinguishable in the logs (and
# to the caller) from a genuine bug. That's the wrong shape on both
# ends: the correct HTTP semantics for "the server is momentarily out of
# a specific resource, try again shortly" is 503 (with Retry-After), not
# 500 -- and this is an expected, self-recovering condition under load,
# not a defect worth an ERROR-level page/alert every time a burst hits.
#
# Handled here, in this SAME innermost middleware (rather than a second,
# separate `@app.exception_handler`), for the exact CORS/response-shape/
# X-Request-ID reasons the module docstring above already lays out for
# the generic case -- registering a second bare `@app.exception_handler`
# would hit the identical ServerErrorMiddleware/outside-of-CORSMiddleware
# problem this file exists to avoid.
_POOL_TIMEOUT_RETRY_AFTER_SECONDS = 3


def _pool_exhaustion_response(request: Request, request_id: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(_POOL_TIMEOUT_RETRY_AFTER_SECONDS)},
        content={
            "detail": "The server is momentarily busy handling other requests. Please retry shortly.",
            "request_id": request_id,
        },
    )


class UnhandledExceptionMiddleware:
    """Pure ASGI middleware -- see module docstring above for the full
    reasoning, especially why this ISN'T just
    `@app.exception_handler(Exception)`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Not an HTTP request (e.g. a lifespan/startup event) --
            # nothing for this middleware to do, just pass it through.
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except sqlalchemy.exc.TimeoutError as exc:
            # See _pool_exhaustion_response() above -- every pooled/
            # overflow connection was already checked out for longer than
            # settings.DB_POOL_TIMEOUT_SECONDS. Expected, self-recovering
            # behavior under a burst, not a bug: WARNING (not ERROR), no
            # traceback noise, and reported to errorbeacon as a degraded-
            # dependency event rather than an application exception, same
            # "fail_open"-style distinction test_resilience.py already
            # exercises for a Redis outage.
            request = Request(scope, receive=receive)
            logger.warning(
                "Database connection pool exhausted while handling %s %s -- returning 503",
                request.method,
                request.url.path,
                extra={"http_method": request.method, "http_path": request.url.path},
            )
            report_exception(
                exc, scope, status_code=503,
                component="database", operation="pool_checkout",
                severity="warning", category="dependency_degraded",
                context={"request_id": request_id_var.get(), "failure_mode": "pool_exhausted"},
            )

            if response_started:
                raise

            response = _pool_exhaustion_response(request, request_id_var.get())
            await response(scope, receive, send)
        except Exception as exc:
            request = Request(scope, receive=receive)
            logger.error(
                "Unhandled exception while handling %s %s",
                request.method,
                request.url.path,
                exc_info=True,
                extra={"http_method": request.method, "http_path": request.url.path},
            )
            report_exception(exc, scope, status_code=500, context={"request_id": request_id_var.get()})

            if response_started:
                # A response had already started streaming (e.g. this
                # broke mid-way through a large CSV/PDF export download)
                # -- too late to swap in a clean JSON error body without
                # corrupting whatever bytes the client already received.
                # We've already logged it above with full traceback and
                # request_id; re-raise so the ASGI server itself notices
                # the connection ended abnormally, same as it always did
                # before this middleware existed.
                raise

            error_response = JSONResponse(
                status_code=500,
                content={
                    "detail": "An unexpected error occurred. If this keeps happening, "
                              "please contact support and include the request ID below.",
                    "request_id": request_id_var.get(),
                },
            )
            await error_response(scope, receive, send)
