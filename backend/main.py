"""
main.py
-------
FastAPI application entrypoint for Snipe-IT Lite.

This file's ONLY job is: create the FastAPI app, configure CORS, customize
the OpenAPI schema, run startup (init_db/seed_db), and mount every
`APIRouter` from `api/`. All request/response schemas live in `schemas/`,
all business/CRUD logic lives in `services/`, and the shared auth
dependencies (`get_current_user`, `require_super_admin`,
`require_privileged_role`) live in `deps.py`. Nothing in this file talks to
the database directly.

Authentication model:
  - POST /auth/login checks the email/password against the `users` table and,
    on success, returns a signed JWT that encodes the user's id/name/email/
    role/department.
  - Every other protected route requires that JWT in the `Authorization:
    Bearer <token>` header. `deps.get_current_user` decodes it to learn who
    is calling and what they're allowed to do.

Role model:
  - "super_admin" -> full access to everything. IS a database row now (see
                     security.py's module docstring) -- exactly one,
                     bootstrapped by alembic/versions/0002_bootstrap_root_admin.py
                     during `alembic upgrade head` in production. Its
                     IDENTITY (username/name) is fixed/hardcoded via
                     config.py's SUPER_ADMIN_USERNAME/SUPER_ADMIN_NAME, but
                     its password is a normal Argon2id hash, rotatable
                     through the same self-service/admin-reset flows as any
                     other account. It can never be created (again),
                     edited, or deleted through the app, and it never
                     appears in the User Directory, bulk exports, or the
                     Audit Trail (see deps.py + services/auth_service.py +
                     services/user_service.py + services/audit_service.py
                     for where this is enforced).
  - "admin"       -> a normal, database-backed account with every privilege
                     "super_admin" has (see deps.py's _FULL_ADMIN_ROLES) --
                     the difference is purely how the account exists
                     (editable/deletable `users` row vs. the one hardcoded
                     identity above), never what it's allowed to do.
  - "manager"     -> can view inventory, dispatch/check-in items to ANY of
                     the three channels (Staff, Linked Customers, or Ad-Hoc
                     Individuals), and view + manage custody for ANY user
                     system-wide (no department-scoping). Can ALSO provision
                     new Staff and Customer login accounts (see POST
                     /users), but can never provision another Manager or
                     Admin account. Still cannot create/delete asset pools,
                     cannot adjust pool capacity, and cannot flag
                     maintenance exceptions -- those remain Super
                     Admin/Admin-only.
  - "staff"       -> a regular employee record. Has a read-only self-service
                     dashboard (staff.html) showing only their own custody.
  - "customer"    -> an external contact with a login. Has a read-only
                     self-service dashboard (customer.html), same idea as
                     staff but for people outside the company.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.concurrency import run_in_threadpool

from database import init_db, seed_db, get_schema_status, engine as db_engine
import db_pool_metrics
from celery_app import check_redis_health
from config import settings
from logging_config import configure_logging
from telemetry import (
    setup_tracing,
    shutdown_tracing,
    instrument_fastapi_app,
    instrument_http_error_tags,
    instrument_sqlalchemy_engine,
    instrument_celery,
    instrument_redis,
)
from integrations.fastapi_errorbeacon import report_background_exception
from middleware.error_handling import UnhandledExceptionMiddleware
from middleware.request_context import RequestContextMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.security_headers import SecurityHeadersMiddleware
from middleware.clean_urls import CleanUrlsMiddleware
from middleware.spa_fallback import SpaFallbackMiddleware
from middleware.maintenance_mode import MaintenanceModeMiddleware
from middleware.db_concurrency import DBConcurrencyMiddleware

from api.auth_api import router as auth_router
from api.assets_api import router as assets_router
from api.users_api import router as users_router
from api.outsiders_api import router as outsiders_router
from api.checkouts_api import router as checkouts_router
from api.audit_api import router as audit_router
from api.backup_api import router as backup_router
from api.quotations_api import router as quotations_router
from api.notifications_api import router as notifications_router
from api.reports_api import router as reports_router
from api.telemetry_api import router as telemetry_router
from api.maintenance_api import router as maintenance_router
from api.diagnostics_api import router as diagnostics_router

# ---------------------------------------------------------------------------
# STRUCTURED LOGGING -- configure this FIRST, before anything else in the
# app has a chance to call `logging.getLogger(...).info(...)`, so no log
# line is ever emitted with the default unconfigured format.
# ---------------------------------------------------------------------------
configure_logging(settings)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DISTRIBUTED TRACING (OpenTelemetry, Operations & Observability requirement)
# ---------------------------------------------------------------------------
# A no-op when settings.OTEL_ENABLED is false (the default) -- see
# telemetry.py's module docstring for the full "why" and exactly what gets
# instrumented. Deliberately called here, right after configure_logging()
# and before anything else touches the database or builds `app`: the
# SQLAlchemy instrumentation below needs the global TracerProvider to
# already exist to patch `db_engine` correctly, and every span this app
# would ever create (FastAPI requests, SQL queries) needs the SAME
# TracerProvider instance, set exactly once.
#
# NOTE on uvicorn's `--workers N` (production, see start.sh): each worker
# process re-imports this ENTIRE module independently (uvicorn's built-in
# multi-worker mode is genuinely N separate Python processes, each doing
# its own fresh top-to-bottom import of `main:app` -- not one process
# forking after this module already ran), so calling setup_tracing() here
# at plain module level is safe and correctly gives every worker its own
# TracerProvider. Contrast with celery_app.py, where the SAME call would
# NOT be safe at plain import time -- see that file's own comment for why
# Celery's prefork pool needs a `worker_process_init` signal instead.
setup_tracing(settings)
instrument_sqlalchemy_engine(db_engine, settings)
instrument_redis(settings)
instrument_celery(settings)

# Real PgBouncer/Postgres/SQLAlchemy connection-pool numbers, exported as
# OTel gauges when OTEL_ENABLED -- see db_pool_metrics.py's module
# docstring. Deliberately placed AFTER setup_tracing() above: an
# ObservableGauge's callback is wired up once at registration, so
# metrics.get_meter() needs to already resolve to the real configured
# MeterProvider at this point, not the API's no-op default.
db_pool_metrics.register_gauges()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the same startup/shutdown work without FastAPI's deprecated hooks.

    Keeping the actual work in ``on_startup``/``on_shutdown`` functions is
    intentional: the test suite and operational tooling can still call those
    functions directly, while FastAPI uses the supported lifespan protocol in
    real server/TestClient lifecycles. No startup ordering or shutdown cleanup
    is changed by this wrapper.
    """
    # FastAPI's old sync startup/shutdown event hooks were executed through
    # Starlette's threadpool. Keep that behavior here because both functions
    # perform blocking database/scheduler/telemetry work; calling them directly
    # from this async lifespan would unnecessarily block the event loop.
    await run_in_threadpool(on_startup)
    try:
        yield
    finally:
        await run_in_threadpool(on_shutdown)


app = FastAPI(
    title="Custom Snipe-IT API",
    lifespan=lifespan,
    # SECURITY: when settings.ENABLE_API_DOCS is False (set this in any
    # environment reachable from the public internet -- see config.py's
    # ENABLE_API_DOCS docstring), passing None here doesn't just hide these
    # pages behind a login or a "hidden" URL -- FastAPI skips generating
    # the OpenAPI schema and never registers these routes at all, so
    # requesting them returns a plain 404 like any other nonexistent path.
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)


# ---------------------------------------------------------------------------
# APP LIFESPAN / STARTUP (Operations & Observability requirement #1)
# ---------------------------------------------------------------------------
# `init_db()` (create tables) and `seed_db()` (insert demo accounts/data)
# used to run unconditionally the INSTANT this module was imported --
# before FastAPI, uvicorn, or even this `app` object existed. That coupled
# "importing main.py" to "touching the production database", with no way to
# opt out (see config.py's AUTO_INIT_DB/AUTO_SEED_DEMO_DATA docstring for
# the full reasoning).
#
# Now both calls live inside the FastAPI **lifespan startup phase** --
# they only run once, right as the server actually starts serving requests
# -- and each is individually gated by a settings flag so a production
# deployment can disable them and rely on `alembic upgrade head` instead
# (see README.md's "Database Migrations" and "Running in Production"
# sections).
def on_startup() -> None:
    # BUG FIX: wrap the first real database touch in a try/except that logs
    # a clear, actionable diagnostic before re-raising. Previously, if
    # DATABASE_URL pointed at something unreachable (wrong host, firewall
    # not open yet, credentials wrong, sslmode missing/wrong for a managed
    # Postgres that requires it -- see .env.azure.example), this call would
    # raise a raw SQLAlchemy/psycopg2 traceback with no indication of WHAT
    # to check, straight into the startup event where it's easy to miss
    # amid everything else in a production log stream. We still fail fast
    # (a container that can't reach its database should not silently start
    # serving requests) -- this only makes the failure legible.
    try:
        if settings.AUTO_INIT_DB:
            logger.info("AUTO_INIT_DB is enabled -- ensuring tables exist.")
            init_db()
        else:
            logger.info("AUTO_INIT_DB is disabled -- skipping create_all(). Run 'alembic upgrade head' instead.")
    except Exception as exc:
        report_background_exception(exc, component="startup", operation="database_init", severity="critical")
        logger.error(
            "Could not reach the database at startup. Common causes: (1) "
            "DATABASE_URL is wrong/unset -- check host, port, credentials, "
            "and database name; (2) a managed Postgres (Azure Flexible "
            "Server, Render, etc.) requires '?sslmode=require' in "
            "DATABASE_URL and it's missing -- see .env.azure.example; (3) "
            "a firewall/network rule is blocking this container's outbound "
            "IP -- see infra/main.bicep's postgresFirewallAzure rule for "
            "the Azure case; (4) the database server isn't up yet -- if "
            "you're on docker-compose, confirm the 'db' service is healthy. "
            "Refusing to continue starting up.",
            exc_info=True,
        )
        raise

    if settings.AUTO_SEED_DEMO_DATA:
        if settings.is_production:
            # Not a hard failure (an already-populated prod DB makes this a
            # harmless no-op -- see seed_db()'s own guard), but loud enough
            # that it shows up immediately in production logs/alerts if
            # someone forgot to turn this off before a real deployment.
            logger.warning(
                "AUTO_SEED_DEMO_DATA is enabled while ENVIRONMENT=production. "
                "If this database is empty, demo accounts with PUBLIC default "
                "passwords (see database.py's seed_db()) will be created. "
                "Set AUTO_SEED_DEMO_DATA=false in your production .env unless "
                "this is intentional."
            )
        seed_db()
    else:
        logger.info("AUTO_SEED_DEMO_DATA is disabled -- skipping demo data seeding.")

    # Daily database backup scheduler (see services/backup_service.py). A
    # plain daemon thread inside this same process -- not Celery -- so it
    # runs regardless of RUN_EMBEDDED_WORKER/Redis configuration. No-op if
    # settings.ENABLE_AUTO_BACKUP is false.
    import services.backup_service as backup_service

    backup_service.start_backup_scheduler()


def on_shutdown() -> None:
    # Flushes any spans still buffered by telemetry.py's BatchSpanProcessor
    # and stops its background export thread -- a no-op when
    # settings.OTEL_ENABLED is false, see shutdown_tracing()'s own
    # docstring for exactly what this fixes (losing the last few seconds
    # of spans, and a background-thread-vs-interpreter-teardown race that
    # otherwise surfaces as a noisy "I/O operation on closed file" error
    # right as the process exits -- most visible running `pytest` locally
    # with OTEL_CONSOLE_EXPORTER=true).
    shutdown_tracing()


# ---------------------------------------------------------------------------
# MIDDLEWARE STACK
# ---------------------------------------------------------------------------
# Starlette/FastAPI executes middleware in the REVERSE of the order they're
# added here -- the LAST one added ends up as the OUTERMOST layer, seeing
# every request first and every response last. We rely on that to get this
# execution order (outermost -> innermost):
#
#   SecurityHeadersMiddleware   (added last -> outermost: stamps headers
#                                onto literally every response, including
#                                CORS preflights and 429s from below)
#   CORSMiddleware              (must wrap RateLimitMiddleware so a
#                                browser-blocked/rate-limited response still
#                                carries correct CORS headers and isn't
#                                reported to the frontend as a confusing
#                                opaque network error)
#   RequestContextMiddleware    (assigns the request's correlation ID as
#                                early as possible, so even a request that
#                                gets rate-limited below still logs with a
#                                proper request_id and returns X-Request-ID)
#   RateLimitMiddleware         (only cares about POST /auth/login; every
#                                other path is a no-op pass-through)
#   UnhandledExceptionMiddleware (added first -> INNERMOST, wrapping the
#                                actual route dispatch directly -- see
#                                middleware/error_handling.py's module
#                                docstring for why this has to be the
#                                innermost layer rather than a plain
#                                `@app.exception_handler(Exception)`:
#                                FastAPI/Starlette routes a handler
#                                registered for the bare `Exception` class
#                                to the OUTERMOST ServerErrorMiddleware --
#                                past CORSMiddleware entirely -- so a 500
#                                built that way would ship with no CORS
#                                headers. Being innermost here means every
#                                other layer above still sees a normal
#                                response and adds its headers exactly as
#                                it would for any other request.)
#   -> your route handlers
app.add_middleware(UnhandledExceptionMiddleware)
app.add_middleware(
    RateLimitMiddleware,
    # BUG FIX: every router below is mounted with `prefix="/api"` (see
    # `app.include_router(auth_router, prefix="/api")` further down), and
    # `auth_router` itself already declares `prefix="/auth"` (see
    # api/auth.py) -- so the real, final path a POST request to log in
    # actually hits is "/api/auth/login", NOT "/auth/login". This
    # middleware runs at the raw ASGI layer, BEFORE FastAPI's router ever
    # sees the request, so it only ever gets the full incoming path -- it
    # has no idea about route-level prefixes. With the old bare
    # "/auth/login" entry, `scope.get("path") not in self.limited_paths`
    # was ALWAYS True for every real login request, so this middleware
    # silently passed every single one straight through untouched: the
    # per-IP throttle documented above (module docstring) never actually
    # engaged, no matter how many login attempts came from the same IP in
    # the same window. That removed the outer layer of brute-force
    # protection entirely and let unlimited rapid-fire guesses reach the
    # inner, per-ACCOUNT lockout in services/auth_service.py -- so a
    # single noisy IP (an attacker, a misbehaving script, or even someone
    # fat-fingering their password several times fast) could run a
    # legitimate account straight into a 15-minute lockout (HTTP 423)
    # without the IP-based limiter ever stepping in first to slow them
    # down, which is exactly the scenario the two layers together were
    # supposed to prevent.
    # SECURITY: the two 2FA endpoints (mfa/verify checks a 6-digit TOTP
    # code, mfa/setup/confirm checks one during enrollment) are exactly as
    # IP-guessable as a password -- a 6-digit code is actually a SMALLER
    # search space than most passwords -- so they get the same outer,
    # cross-replica IP throttle as /auth/login itself, on top of the
    # per-account lockout counter mfa_verify() already reuses from
    # services/auth_service.py's password path (see that function's
    # docstring).
    limited_paths={
        "/api/auth/login", "/api/auth/mfa/verify", "/api/auth/mfa/setup/confirm",
        # Requires an already-valid session to reach at all (unlike the
        # three above), but still checks a password guess against the
        # account -- worth the same outer IP throttle in case a session
        # cookie were ever compromised without the password itself.
        "/api/auth/mfa/recovery-codes/regenerate",
        # Pre-login, unauthenticated, and email-sending -- exactly the
        # same brute-force/spam surface as /auth/login itself:
        # forgot-password could otherwise be hammered to flood an
        # arbitrary inbox with reset emails, and reset-password's token
        # guess is worth throttling the same way a password guess is (see
        # services/auth_service.py's request_password_reset()/
        # confirm_password_reset()).
        "/api/auth/forgot-password", "/api/auth/reset-password",
    },
    max_requests=settings.LOGIN_RATE_LIMIT_MAX,
    window_seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
)
# Maintenance is added before RequestContext because middleware executes in reverse registration order:
# RequestContext must remain outside it so planned 503 responses still receive X-Request-ID.
app.add_middleware(MaintenanceModeMiddleware)
# DBConcurrencyMiddleware MUST be registered after MaintenanceModeMiddleware
# so Starlette's reverse registration order makes it the outer DB admission
# gate. MaintenanceModeMiddleware performs a synchronous DB-backed check
# (dispatched to a worker thread), and that check must consume the same
# per-process DB budget as the route it protects.
app.add_middleware(DBConcurrencyMiddleware)
app.add_middleware(RequestContextMiddleware)

# Frontend is served by an nginx container on port 8080 in docker-compose.
# The allowed origins list comes from `settings.CORS_ORIGINS` (see
# backend/config.py / .env.example) instead of being hardcoded, so you can
# add a production domain without touching Python code.
origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)

# TRACING -- the FastAPI server span is the outermost telemetry layer, so
# the pure-ASGI error-tagging middleware is registered immediately BEFORE it. This lets
# the error-tagging middleware run inside the active server span and mark
# 4xx/5xx responses with the stable `error=true` tag that Jaeger's Search
# page can filter. FastAPIInstrumentor is still added LAST so its server span
# covers the FULL request lifecycle (CORS, rate limiting, security headers,
# and the actual route handler alike). Both are no-ops when OTEL is disabled.
instrument_http_error_tags(app, settings)
instrument_fastapi_app(app, settings)

# --- OPENAPI CLEANUP ---
# FastAPI auto-names the CSV-import endpoint's implicit multipart schema
# something ugly like "Body_import_assets_from_csv_assets_import_post" --
# this just renames it to something readable in the generated docs.


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(title="Custom Snipe-IT API", version="0.1.0", routes=app.routes)
    ugly_key = "Body_import_assets_from_csv_assets_import_post"
    clean_key = "CSVImportPayload"
    if "components" in openapi_schema and "schemas" in openapi_schema["components"]:
        schemas = openapi_schema["components"]["schemas"]
        if ugly_key in schemas:
            schemas[clean_key] = schemas.pop(ugly_key)
            schemas[clean_key]["title"] = clean_key
            path_target = openapi_schema.get("paths", {}).get("/assets/import", {}).get("post", {})
            try:
                ref_path = path_target["requestBody"]["content"]["multipart/form-data"]["schema"]
                if ref_path.get("$ref") == f"#/components/schemas/{ugly_key}":
                    ref_path["$ref"] = f"#/components/schemas/{clean_key}"
            except KeyError:
                pass
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

# --- ROUTES ---


@app.get("/healthz")
def health_check():
    """
    LIVENESS probe (infra/main.bicep's Liveness probe points here) --
    deliberately just "is the process up and able to answer HTTP at all",
    with NO database dependency. A liveness failure makes the platform
    KILL and restart the container, which is the right response to "the
    process is hung/deadlocked" but the WRONG response to "the database
    had a brief network blip" or "migrations haven't finished yet on a
    fresh deploy" -- neither of those is fixed by restarting this
    container, and restarting it wouldn't help either one. See GET
    /readyz below for the check that DOES look at the database and the
    schema.
    """
    return {"status": "healthy", "message": "Asset Management API is live"}


@app.get("/readyz")
def readiness_check(response: Response):
    """
    READINESS probe (infra/main.bicep's Readiness probe points here, NOT
    at /healthz) -- unlike /healthz, this queries the database and
    compares its current Alembic migration revision against what THIS
    BUILD of the code expects. See database.py's get_schema_status() for
    the full reasoning and the exact "not ready" cases it covers.

    Why this is a separate endpoint instead of adding the check to
    /healthz: a READINESS failure just stops traffic from being routed to
    this replica while it keeps running and gets another chance next
    poll -- correct for "database temporarily unreachable" and "this
    replica came up before `alembic upgrade head` finished" alike, unlike
    a liveness failure's kill-and-restart (see health_check() above).
    It's also what closes the actual gap that motivated this endpoint:
    the deploy-azure-*.yml pipelines already run `alembic upgrade head`
    as its own blocking step before rolling out a new image, but nothing
    previously verified that a given RUNNING container's schema still
    matched its own code -- if that migrate step were ever bypassed (e.g.
    a manual image update outside the pipeline), the old /healthz would
    have reported healthy regardless, and the first symptom would've been
    a request failing on a missing column instead of the rollout itself
    failing.

    Returns 200 + {"ready": true, ...} once the schema matches, or 503 +
    {"ready": false, "reason": "..."} otherwise. 503 -- not 500 -- is
    what tells Container Apps' readiness probe (and any load balancer or
    external health checker) "not ready yet, try again", rather than
    reading as an application error worth alerting on by itself.
    """
    status = get_schema_status()
    response.status_code = 200 if status["ready"] else 503
    return status


@app.get("/health/dependencies")
def dependency_health_check(response: Response):
    """
    Proactive health check for this app's optional-but-important external
    dependencies -- currently just Redis, which backs the Celery
    broker/result backend (see celery_app.py), the login/telemetry rate
    limiters (middleware/rate_limit.py, utils/rate_limiter.py), and
    background-task DB admission (db_admission.py).

    WHY THIS EXISTS
    ----------------
    Before this endpoint, the only way to learn Redis was struggling was
    to actually hit one of its symptoms on a real request: a slow login,
    a fail-open rate limiter, or (see api/audit_api.py) a 503 on
    starting/polling/downloading an audit export. That's reactive --
    users see the degradation before anyone/anything monitoring this
    service does. Polling this endpoint (from infra/monitoring, or
    optionally the frontend itself) surfaces the same "Redis is
    unreachable" condition BEFORE it turns into a user-facing failure --
    e.g. the frontend could use a non-ok `redis` here to proactively grey
    out the "Export" button rather than let someone click it into a 503.

    Deliberately a SEPARATE endpoint from /healthz and /readyz, not folded
    into either: /healthz must stay a pure "is the process up" check with
    zero dependencies (see health_check()'s own docstring), and /readyz's
    contract is specifically about schema readiness, gating whether
    traffic gets routed to this replica at all -- a struggling Redis is a
    real but PARTIAL degradation (exports/rate-limiting/background admission
    only), not a reason to pull an otherwise-healthy replica out of
    rotation entirely.

    Returns 200 when every checked dependency is healthy, 503 otherwise --
    same "503, not 500" convention as /readyz, since this is an expected,
    self-recovering "a dependency is currently degraded" condition, not an
    application bug worth alerting on as a crash.
    """
    redis_status = check_redis_health()
    response.status_code = 200 if redis_status["ok"] else 503
    return {"ready": redis_status["ok"], "dependencies": {"redis": redis_status}}


# ---------------------------------------------------------------------------
# API ROUTES -- all mounted under /api
# ---------------------------------------------------------------------------
# Every router below already declares its own resource prefix (/auth,
# /assets, /users, /outsiders, /checkouts, /audit-logs -- see each
# api/*.py's `router = APIRouter(prefix=...)` line), so adding "/api" here
# gives e.g. POST /api/auth/login. This matches frontend/js/api.js's
# `API_URL = '/api'` constant exactly, in BOTH deployment shapes this app
# supports:
#   - docker-compose.yml / a multi-service Render deployment: nginx receives
#     "/api/auth/login" and forwards it straight through unchanged (see
#     nginx/default.conf.template) to this same "/api/auth/login" route --
#     no path-rewriting needed on nginx's end anymore.
#   - the free-tier single-service Render deployment (see Dockerfile.render,
#     render-start.sh, render.yaml, and README.md's "Deploying on Render's
#     Free Plan" section): the browser talks to this FastAPI process
#     directly, and "/api/*" simply IS the API while everything else falls
#     through to the static-frontend mount below.
app.include_router(auth_router, prefix="/api")
app.include_router(assets_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(outsiders_router, prefix="/api")
app.include_router(checkouts_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(backup_router, prefix="/api")
app.include_router(quotations_router, prefix="/api")
app.include_router(notifications_router, prefix="/api")
app.include_router(reports_router, prefix="/api")
app.include_router(telemetry_router, prefix="/api")
app.include_router(maintenance_router, prefix="/api")
app.include_router(diagnostics_router, prefix="/api")

# ---------------------------------------------------------------------------
# STATIC FRONTEND (free-tier single-service deployment only)
# ---------------------------------------------------------------------------
# Off by default (see config.py's SERVE_FRONTEND docstring) so this stays a
# pure API-only container for docker-compose.yml's `backend` service and
# any multi-service Render deployment, exactly as before. Dockerfile.render
# (the free-tier combined image) sets SERVE_FRONTEND=true and COPYs BOTH
# the legacy site and the React SPA into the image (settings.FRONTEND_DIR
# and settings.FRONTEND_REACT_DIR respectively) -- which one actually gets
# SERVED is picked below by settings.FRONTEND_VARIANT (see that setting's
# own docstring for why this is a runtime choice, not a build one).
#
# Mounted LAST and at "/" on purpose: FastAPI/Starlette matches routes in
# the order they're registered, so every "/api/*" route (and /docs/etc.
# above) is matched first and this StaticFiles mount only ever handles
# whatever's left over -- the actual frontend files. `html=True` makes "/"
# itself resolve to the chosen frontend's index.html. The legacy site's
# other pages are requested by their CLEAN url (/admin, /manager, /staff,
# /customer -- see frontend/js/auth.js); the React SPA's other "pages" are
# entirely client-side routes (e.g. /checkouts) that never correspond to a
# real file at all. Exactly one of CleanUrlsMiddleware (legacy) or
# SpaFallbackMiddleware (react), added just below, handles the one this
# deployment is actually serving -- see each middleware's own docstring.
if settings.SERVE_FRONTEND:
    import os

    from fastapi.staticfiles import StaticFiles

    is_react = settings.FRONTEND_VARIANT == "react"
    frontend_dir = settings.FRONTEND_REACT_DIR if is_react else settings.FRONTEND_DIR

    if os.path.isdir(frontend_dir):
        if is_react:
            # SPA fallback (see middleware/spa_fallback.py): rewrites any
            # request that isn't a real built file to "/", so the
            # StaticFiles mount below serves index.html and React Router
            # takes over client-side -- the SPA equivalent of the clean-URL
            # rewrite below, for a site that has no *.html pages to map.
            app.add_middleware(SpaFallbackMiddleware, frontend_dir=frontend_dir)
        else:
            # Clean URLs (see middleware/clean_urls.py): rewrites "/admin" ->
            # "admin.html" before it reaches the StaticFiles mount below, and
            # 301-redirects any lingering "/admin.html"-style link to "/admin".
            # Registered here (rather than unconditionally near the other
            # app.add_middleware(...) calls above) because it only makes sense
            # at all when there's actually a frontend mounted to rewrite paths
            # for -- the docker-compose/multi-service deployment shape has
            # SERVE_FRONTEND off and does the equivalent rewrite in nginx
            # instead (see nginx/default.conf.template).
            app.add_middleware(CleanUrlsMiddleware)
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:
        logger.warning(
            "SERVE_FRONTEND is enabled but the %s frontend directory (%s) "
            "doesn't exist -- the frontend won't be served. Check "
            "Dockerfile.render's COPY step and FRONTEND_VARIANT.",
            settings.FRONTEND_VARIANT,
            frontend_dir,
        )
