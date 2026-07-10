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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

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
#   RateLimitMiddleware         (added first -> innermost: only cares about
#                                POST /auth/login; every other path is a
#                                no-op pass-through)
#   -> your route handlers
app.add_middleware(
    RateLimitMiddleware,
    limited_paths={"/auth/login"},
    max_requests=settings.LOGIN_RATE_LIMIT_MAX,
    window_seconds=settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
)
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
    return {"status": "healthy", "message": "Asset Management API is live"}


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

# ---------------------------------------------------------------------------
# STATIC FRONTEND (free-tier single-service deployment only)
# ---------------------------------------------------------------------------
# Off by default (see config.py's SERVE_FRONTEND docstring) so this stays a
# pure API-only container for docker-compose.yml's `backend` service and
# any multi-service Render deployment, exactly as before. Dockerfile.render
# (the free-tier combined image) sets SERVE_FRONTEND=true and COPYs
# frontend/ into the image at settings.FRONTEND_DIR.
#
# Mounted LAST and at "/" on purpose: FastAPI/Starlette matches routes in
# the order they're registered, so every "/api/*" route (and /docs/etc.
# above) is matched first and this StaticFiles mount only ever handles
# whatever's left over -- the actual frontend/*.html, css/*, and js/* files.
# `html=True` makes "/" itself resolve to frontend/index.html; every other
# page (admin.html, manager.html, staff.html, customer.html) is requested
# by its exact filename from the frontend's own <a>/redirect links, so no
# further SPA-style fallback routing is needed here.
if settings.SERVE_FRONTEND:
    import os

    from fastapi.staticfiles import StaticFiles

    if os.path.isdir(settings.FRONTEND_DIR):
        app.mount("/", StaticFiles(directory=settings.FRONTEND_DIR, html=True), name="frontend")
    else:
        logger.warning(
            "SERVE_FRONTEND is enabled but FRONTEND_DIR (%s) doesn't exist -- "
            "the frontend won't be served. Check Dockerfile.render's COPY step.",
            settings.FRONTEND_DIR,
        )
