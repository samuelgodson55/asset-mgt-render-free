"""
main.py
-------
FastAPI application entrypoint for Snipe-IT Lite.

This file's job is: create the FastAPI app, configure CORS/security
headers, customize the OpenAPI schema, run startup (init_db/seed_db +
the overdue-notification scheduler), mount every `APIRouter` from `api/`
under `/api`, and serve the static frontend (frontend/*.html, css/, js/)
from the SAME process. All request/response schemas live in `schemas/`,
all business/CRUD logic lives in `services/`, and the shared auth
dependencies (`get_current_user`, `require_super_admin`,
`require_privileged_role`) live in `deps.py`. Nothing in this file talks
to the database directly.

ONE PROCESS, NOT THREE (why this changed)
-------------------------------------------
This app used to be split across three containers: a private FastAPI
`backend`, a private Celery `worker`, and a public `nginx` reverse proxy
serving the static frontend and forwarding `/api/*` to the backend over a
private network. That shape doesn't fit Render's (or most platforms')
FREE tier for two separate reasons:
  1. Background workers and private services aren't Free-tier-eligible at
     all (see https://render.com/docs/free) -- only Web Services,
     Postgres, and Key Value are.
  2. Free web services can't receive PRIVATE network traffic from other
     services (see the same docs page) -- so even the nginx-front,
     private-FastAPI-backend split wouldn't work between two free web
     services, only a paid one.
The fix: ONE FastAPI app, mounted as a single free Web Service, serves
BOTH the JSON API (under `/api/*`, matching frontend/js/api.js's existing
`API_URL = '/api'` constant unchanged) AND the static frontend files
(everything else) directly via Starlette's StaticFiles. See jobs.py and
scheduler.py for how the old Celery worker's two jobs (async exports,
the overdue-checkout digest) now run as background threads inside this
same process instead.

Authentication model:
  - POST /api/auth/login checks the email/password against the `users`
    table and, on success, returns a signed JWT that encodes the user's
    id/name/email/role/department.
  - Every other protected route requires that JWT in the `Authorization:
    Bearer <token>` header. `deps.get_current_user` decodes it to learn who
    is calling and what they're allowed to do.

Role model:
  - "super_admin" -> full access to everything. NOT a database row -- this
                     is a single hardcoded root identity configured via the
                     SUPER_ADMIN_USERNAME/SUPER_ADMIN_PASSWORD environment
                     variables (see config.py and security.py's
                     super_admin_principal()). Exactly one exists, always;
                     it can never be created, edited, or deleted through
                     the app, and it never appears in the User Directory or
                     any other listing (see deps.py + services/auth_service.py
                     + services/user_service.py for where this is enforced).
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
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

import scheduler
from database import init_db, seed_db
from config import settings
from logging_config import configure_logging
from middleware.request_context import RequestContextMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.security_headers import SecurityHeadersMiddleware

from api.auth import router as auth_router
from api.assets import router as assets_router
from api.users import router as users_router
from api.outsiders import router as outsiders_router
from api.checkouts import router as checkouts_router
from api.audit import router as audit_router
from api.system import router as system_router

# ---------------------------------------------------------------------------
# STRUCTURED LOGGING -- configure this FIRST, before anything else in the
# app has a chance to call `logging.getLogger(...).info(...)`, so no log
# line is ever emitted with the default unconfigured format.
# ---------------------------------------------------------------------------
configure_logging(settings)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Custom Snipe-IT API",
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
# APP STARTUP (Operations & Observability requirement #1)
# ---------------------------------------------------------------------------
# `init_db()` (create tables) and `seed_db()` (insert demo accounts/data)
# used to run unconditionally the INSTANT this module was imported --
# before FastAPI, uvicorn, or even this `app` object existed. That coupled
# "importing main.py" to "touching the production database", with no way to
# opt out (see config.py's AUTO_INIT_DB/AUTO_SEED_DEMO_DATA docstring for
# the full reasoning).
#
# Now both calls live inside a proper FastAPI **startup event handler** --
# they only run once, right as the server actually starts serving requests
# -- and each is individually gated by a settings flag so a production
# deployment can disable them and rely on `alembic upgrade head` instead
# (see README.md's "Database Migrations" and "Running in Production"
# sections).
@app.on_event("startup")
def on_startup() -> None:
    if settings.AUTO_INIT_DB:
        logger.info("AUTO_INIT_DB is enabled -- ensuring tables exist.")
        init_db()
    else:
        logger.info("AUTO_INIT_DB is disabled -- skipping create_all(). Run 'alembic upgrade head' instead.")

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

    # Starts the in-process overdue-checkout digest thread (a no-op if
    # NOTIFICATIONS_ENABLED is false) -- see scheduler.py's module
    # docstring for why this replaced a separate Celery Beat container.
    scheduler.start()


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
#                                CORS preflights, 429s, and static files)
#   GZipMiddleware              (compresses static assets + JSON alike --
#                                nginx used to do this for static files only)
#   CORSMiddleware              (must wrap RateLimitMiddleware so a
#                                browser-blocked/rate-limited response still
#                                carries correct CORS headers and isn't
#                                reported to the frontend as a confusing
#                                opaque network error)
#   RequestContextMiddleware    (assigns the request's correlation ID as
#                                early as possible, so even a request that
#                                gets rate-limited below still logs with a
#                                proper request_id and returns X-Request-ID)
#   RateLimitMiddleware         (added first -> innermost: only cares about
#                                POST /api/auth/login; every other path is a
#                                no-op pass-through)
#   -> your route handlers / static files
app.add_middleware(
    RateLimitMiddleware,
    limited_paths={"/api/auth/login"},
    max_requests=settings.LOGIN_RATE_LIMIT_MAX,
    window_seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
)
app.add_middleware(RequestContextMiddleware)

# The frontend and API are now served from this SAME app/origin (see this
# file's module docstring), so CORS almost never matters for normal
# browser use -- `settings.CORS_ORIGINS` defaults to empty for exactly
# that reason. It still exists for the rare case of calling this API from
# a genuinely different origin (see config.py's CORS_ORIGINS docstring).
origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(SecurityHeadersMiddleware)

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
            path_target = openapi_schema.get("paths", {}).get("/api/assets/import", {}).get("post", {})
            try:
                ref_path = path_target["requestBody"]["content"]["multipart/form-data"]["schema"]
                if ref_path.get("$ref") == f"#/components/schemas/{ugly_key}":
                    ref_path["$ref"] = f"#/components/schemas/{clean_key}"
            except KeyError:
                pass
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

# --- API ROUTES (mounted under /api, matching frontend/js/api.js's API_URL) ---
# Every router below already carries its own resource prefix (e.g.
# audit_router is APIRouter(prefix="/audit-logs")) -- passing prefix="/api"
# here just prepends /api in front of that, e.g. POST /api/auth/login,
# GET /api/audit-logs. This is the one line that replaces nginx's old
# `rewrite ^/api/(.*)$ /$1 break;` reverse-proxy rule -- same net effect
# (the browser calls /api/*, the router itself doesn't know about that
# prefix), just done inside this one process instead of a second
# container.
app.include_router(auth_router, prefix="/api")
app.include_router(assets_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(outsiders_router, prefix="/api")
app.include_router(checkouts_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(system_router, prefix="/api")


@app.get("/healthz")
def health_check():
    """
    Plain liveness check, kept OUTSIDE the /api prefix and registered
    directly on the app (before the static-file mount below) so it always
    resolves to this JSON response rather than the static frontend's
    index.html. Point Render's healthCheckPath (see render.yaml) or any
    external uptime pinger at this path. See api/system.py's GET
    /api/system/health for a second, equivalent endpoint under the /api
    prefix if you'd rather keep everything under one namespace.
    """
    return {"status": "healthy", "message": "Snipe-IT Lite is live"}


# --- STATIC FRONTEND (index.html/admin.html/staff.html/manager.html/
#     customer.html + css/, js/) -----------------------------------------
# Mounted LAST and at the root path so every /api/* route and /healthz
# above still take priority (FastAPI/Starlette matches explicit routes
# before falling through to a mount). `html=True` makes StaticFiles serve
# frontend/index.html for "/" and 404.html-style fallback behavior for
# unmatched paths, while still serving admin.html/staff.html/etc. as
# plain files at their own literal paths -- exactly what nginx's
# `root /usr/share/nginx/html; index index.html;` used to do.
#
# FRONTEND_DIR is resolved relative to THIS file rather than hardcoded as
# "frontend" (a relative path would break depending on the working
# directory uvicorn happens to be started from) -- see the root
# Dockerfile, which COPYs frontend/ to ../frontend relative to this
# backend/ directory inside the image, matching this repo's own layout.
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    logger.warning(
        "Frontend directory not found at %s -- only the /api/* JSON API will be served. "
        "This is expected if you're running the backend standalone (see README.md's "
        "'Running Without Docker' section); it's NOT expected in a Docker/Render deploy.",
        FRONTEND_DIR,
    )
