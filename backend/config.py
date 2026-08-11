"""
config.py
---------
Central configuration module for the backend, built on Pydantic Settings v2.

WHY THIS FILE EXISTS
---------------------
Before this change, secrets like the Postgres password and the JWT signing
key were hardcoded directly inside docker-compose.yml AND repeated again as
fallback defaults inside database.py / security.py. That meant:
  1. Secrets lived in plain text inside version control.
  2. The "real" value and the "fallback" value could silently drift apart.

Now there is exactly ONE place secrets are allowed to live: a git-ignored
`.env` file at the project root. Everything else -- Docker Compose AND the
Python backend -- reads from that single source of truth.

HOW VARIABLES FLOW (beginner-friendly walkthrough)
---------------------------------------------------
1. You copy `.env.example` (committed to git, contains no real secrets) to
   `.env` (NOT committed to git -- see .gitignore) and fill in real values.
2. `docker compose up` reads `.env` automatically (Docker Compose always
   looks for a file literally named `.env` in the same folder as
   docker-compose.yml) and substitutes `${VARIABLE_NAME}` placeholders in
   docker-compose.yml with those values.
3. docker-compose.yml passes those same values into the `backend` container
   as environment variables (see the `environment:` block for the `backend`
   service).
4. Once inside the container, THIS file (`config.py`) uses Pydantic's
   `BaseSettings` to automatically read those environment variables into a
   typed, validated Python object (`settings`).
5. Every other backend module (`database.py`, `security.py`, `main.py`)
   imports `settings` from here instead of calling `os.getenv(...)`
   directly. One source of truth, one place to add new config values.
"""

from typing import Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder/default secrets that ship in this repo (config.py's own
# fallback, plus the one committed in .env.example). If ANY of these are
# still the active JWT_SECRET_KEY while ENVIRONMENT=production, anyone who
# has read the source (i.e. everyone, since it's public) can forge a valid
# session token for any user -- including super_admin.
_INSECURE_JWT_SECRETS = {
    "",
    "dev-secret-change-me-in-production",
    "change-this-to-a-long-random-string",
}
_MIN_PROD_JWT_SECRET_LENGTH = 32


def _parse_utc_hours_csv(raw: str, field_name: str, *, default_hour: int) -> list[int]:
    """
    Shared parser for every "*_HOURS_UTC" config field (BACKUP_HOURS_UTC,
    OVERDUE_DIGEST_HOURS_UTC, DUE_SOON_DIGEST_HOURS_UTC): a comma-separated
    string of hours of day, UTC, each 0-23 -- "3" for once a day, "3,15,21"
    for three times a day -- parsed into a sorted, deduped list of ints.
    Raises a clear ValueError for a non-numeric or out-of-range entry
    rather than silently ignoring it, so a typo like "3,25" surfaces at
    startup (see each field's own `_validate_*` model_validator) instead
    of only being discovered whenever the scheduler next wakes up.
    """
    hours: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour = int(part)
        except ValueError:
            raise ValueError(f"{field_name} contains a non-numeric hour: '{part}'.")
        if not 0 <= hour <= 23:
            raise ValueError(f"{field_name} contains an out-of-range hour: {hour} (must be 0-23).")
        hours.add(hour)
    return sorted(hours) or [default_hour]


class Settings(BaseSettings):
    """
    Each attribute below maps 1:1 to an environment variable of the same
    name (case-insensitive). Pydantic Settings v2 validates types
    automatically -- e.g. if JWT_EXPIRY_HOURS isn't a valid integer, the
    app will fail fast at startup with a clear error instead of crashing
    later mid-request.
    """

    @model_validator(mode="before")
    @classmethod
    def apply_environment_defaults(cls, data):
        if not isinstance(data, dict):
            return data

        environment_name = str(data.get("ENVIRONMENT", "development")).strip().lower()
        is_production = environment_name in {"production", "prod"}

        # ENVIRONMENT is the single source of truth for every default below --
        # there used to be a separate LEAN_MODE override that could diverge
        # from ENVIRONMENT (see the BUG FIX note this replaced: a VM deploy
        # set ENVIRONMENT=production but also forced LEAN_MODE="false", which
        # silently re-enabled AUTO_INIT_DB and raced the deploy workflow's
        # own "alembic upgrade head" step). LEAN_MODE has been removed
        # entirely -- every flag below now keys directly off is_production,
        # so ENVIRONMENT alone always determines the outcome, with no
        # separate switch that can be left out of sync with it. An explicit
        # value for any of these flags still always wins (see the
        # `if "X" not in data` guard on each) -- only the DEFAULT changed.
        if "ENABLE_API_DOCS" not in data:
            data["ENABLE_API_DOCS"] = False if is_production else True
        if "AUTO_INIT_DB" not in data:
            data["AUTO_INIT_DB"] = False if is_production else True
        if "AUTO_SEED_DEMO_DATA" not in data:
            data["AUTO_SEED_DEMO_DATA"] = False if is_production else True
        if "ENABLE_AUTO_BACKUP" not in data:
            data["ENABLE_AUTO_BACKUP"] = False if is_production else True
        if "LOG_LEVEL" not in data:
            data["LOG_LEVEL"] = "WARNING" if is_production else "INFO"
        return data

    # --- Environment ----------------------------------------------------
    # "local", "development" (default, safe for local docker compose), or
    # "production". As far as THIS backend process is concerned, only
    # "production" is special -- it drives the JWT-secret/Super Admin
    # password startup checks below; "local" is treated identically to
    # "development" and never changes backend runtime behavior beyond that,
    # so it's safe to leave unset locally.
    #
    # This same value is also read by docker-compose.yml at *image build*
    # time (not by this backend process) to decide how frontend/js gets
    # processed -- see frontend/Dockerfile's frontend-build stage and
    # build-frontend/build.js: local = untouched, development = minified,
    # production = minified + obfuscated.
    ENVIRONMENT: str = "development"

    # --- Database -----------------------------------------------------
    # Full SQLAlchemy connection string. In Docker Compose, "db" is the
    # hostname because Compose puts every service on the same network and
    # lets them reach each other by service name.
    DATABASE_URL: str = "postgresql://admin:supersecret@db:5432/asset_db"

    # BUG FIX ("Audit Logs page fails on repeated refresh, but recovers if
    # you stop hammering it for a bit" -- `psycopg2.OperationalError: ...
    # remaining connection slots are reserved for roles with the SUPERUSER
    # attribute"): database.py's engine used to be created with
    # SQLAlchemy's un-bounded defaults (`pool_size=5` + `max_overflow=10`
    # -- up to 15 live connections per process that imports it), with no
    # awareness of how many OTHER processes are doing the exact same thing
    # at once: `backendMaxReplicas` Container App replicas (infra/main.bicep)
    # each also running a second, separate embedded-Celery-worker process
    # (RUN_EMBEDDED_WORKER, see start.sh) that imports database.py too.
    # That fan-out can add up to more connections than a small managed
    # Postgres tier actually grants non-superuser roles -- a single
    # request usually still finds a free slot, but a burst of concurrent
    # requests exhausts what's left.
    #
    # Rather than hand-picking a pool size for "today's" replica count and
    # Postgres tier (a number that goes stale -- and needs a human to
    # notice and fix -- the moment either changes), database.py now works
    # this out ITSELF at startup: it asks the target Postgres server what
    # its real `max_connections`/`superuser_reserved_connections` budget
    # is, divides that by how many DB-connecting processes can exist at
    # once (`BACKEND_MAX_REPLICAS` below x 1 or 2 processes/replica
    # depending on RUN_EMBEDDED_WORKER), and sizes `pool_size`/
    # `max_overflow` to fit -- see database.py's `_compute_pool_sizing()`.
    # That self-adjusts automatically if the Postgres SKU is resized or
    # `backendMaxReplicas` changes, with zero code/config edits and
    # without ever needing anyone to log into prod and tune a number by
    # hand. DB_POOL_SIZE/DB_MAX_OVERFLOW below are an ESCAPE HATCH only --
    # leave them unset (the default) to get the automatic behavior; set
    # both to force a fixed, non-adaptive size instead (e.g. for a
    # database that can't be probed, or a deliberately different budget).
    DB_POOL_SIZE: int | None = None
    DB_MAX_OVERFLOW: int | None = None
    # How many connections' worth of headroom to leave un-allocated to any
    # process's pool, on top of Postgres's own superuser-reserved slots --
    # covers one-off, non-pooled connections the adaptive sizing above
    # can't see coming: `alembic upgrade head` during a deploy, someone
    # running a `scripts/` one-off, or a manual `psql` session.
    DB_CONNECTION_SAFETY_MARGIN: int = 5
    # Worst-case number of `backend` Container App replicas that can be
    # running (and therefore importing database.py) AT ONCE -- mirrors
    # infra/main.bicep's `backendMaxReplicas` param, which also passes
    # this same value through as this env var so the two never drift
    # apart. Defaults to 1, correct for local `docker compose up` (a
    # single backend container, no autoscaling) and for any other
    # single-process deployment that never sets this env var.
    BACKEND_MAX_REPLICAS: int = 1
    # DIRECT override for "how many separate OS processes, across the
    # WHOLE deployment, can be importing database.py (and therefore
    # holding their own pool open) at the same moment" -- for deployment
    # shapes `BACKEND_MAX_REPLICAS x (1 or 2 depending on
    # RUN_EMBEDDED_WORKER)` doesn't fit.
    #
    # That derivation assumes every DB-connecting process is an
    # interchangeable `backend` replica (each optionally running several
    # uvicorn workers, optionally paired with its own embedded, single-
    # concurrency Celery worker) -- true for infra/main.bicep's Container
    # Apps layout and render.yaml's single-instance Free plan, but NOT
    # true for docker-compose.yml (local dev) or docker-compose.vm.yml
    # (the VM target): both run `worker` (celery --concurrency=2 -- 2
    # forked processes, not 1) and `beat` as their OWN separate, always-
    # on containers rather than embedding them, and docker-compose.vm.yml
    # ALSO briefly runs BOTH its blue and green `backend` pair (each 2
    # uvicorn workers, UVICORN_WORKERS=2) at once during a rollout, on
    # top of that. Left at the replica-derived guess, each of those extra
    # processes would size its own pool as if it were the only consumer,
    # silently under-accounting for the others -- the same class of bug
    # this whole adaptive-sizing mechanism exists to prevent.
    #
    # Each of those compose files sets this explicitly to its own real,
    # worst-case concurrent-process count instead of relying on the
    # derivation; leave unset (None, the default) to keep using the
    # BACKEND_MAX_REPLICAS-based derivation.
    DB_EXPECTED_PROCESSES: int | None = None
    # Fail fast with a clear "pool exhausted" error instead of a request
    # hanging indefinitely when every pooled/overflow connection above is
    # already checked out.
    DB_POOL_TIMEOUT_SECONDS: int = 10

    # --- Async export workers (Celery + Redis) -----------------------------
    # CSV/PDF ledger exports used to be generated synchronously, inline in
    # the request/response cycle -- fine for a small date range, but a
    # Super Admin exporting months of the (unbounded, append-only) audit
    # ledger as a PDF could tie up an API worker process for a long time
    # building it. Generation now happens in a separate `celery` worker
    # process/container (see backend/celery_app.py, backend/tasks/, and the
    # `worker` service in docker-compose.yml); the API only ever enqueues a
    # job and polls/returns its result.
    #
    # REDIS_URL is used as the Celery broker (where jobs queue up), the
    # Celery result backend (where a finished job's small JSON metadata --
    # filename/content-type/where-the-file-lives -- is stashed until the
    # frontend downloads it), the shared counter store for the login rate
    # limiter (see middleware/rate_limit.py), and the distributed lock the
    # scheduled-backup thread uses so only one replica actually fires it
    # (see services/backup_service.py's _acquire_scheduled_backup_lock).
    # "redis" is the docker-compose service name below, same pattern as
    # DATABASE_URL's "db" hostname above.
    REDIS_URL: str = "redis://redis:6379/0"
    # How long a finished export's result metadata stays in Redis before
    # expiring, in seconds. Long enough for a normal "click export, wait a
    # few seconds, download" flow with some slack for a slow connection;
    # short enough that finished exports don't sit around forever if nobody
    # downloads them. The actual FILE on disk (see EXPORT_RESULT_DIR below)
    # is swept out on the same schedule -- see tasks/export_tasks.py's
    # _sweep_expired_exports().
    EXPORT_RESULT_TTL_SECONDS: int = 3600
    # --- Export file storage (SPEED: disk instead of RAM) -------------------
    # Export files used to be embedded directly in the Celery result --
    # base64-encoded bytes sitting in Redis (an in-memory datastore) for up
    # to EXPORT_RESULT_TTL_SECONDS, for EVERY export, concurrently. A single
    # wide-date-range audit PDF can run into the tens of megabytes; base64
    # inflates that by ~33%, and Redis has to hold the whole thing in RAM
    # for the entire TTL window even after the file's been downloaded. Under
    # a few concurrent exports (easy to hit once this app is scaled to
    # multiple replicas, each capable of enqueuing its own jobs -- see
    # DEPLOYMENT.md's load balancing section) that RAM usage stacks up fast
    # and makes Redis itself slower for every OTHER thing it's doing at the
    # same time (session/rate-limit lookups, the job queue itself).
    #
    # Now, `tasks/export_tasks.py` writes the finished file to plain disk
    # under this directory and only stores a small JSON dict (filename,
    # content type, disk path) as the Celery result -- Redis goes back to
    # holding kilobytes instead of megabytes per export, and disk space is
    # far cheaper to burn through than RAM. This directory must be on a
    # volume shared between the `backend` and `worker` containers (see
    # docker-compose.yml's `export_data` volume) since `worker` writes the
    # file and `backend` is what serves it back for download; in the
    # single-container Render free-tier mode (Dockerfile.render) they're
    # already the same filesystem, so no extra configuration is needed
    # there.
    EXPORT_RESULT_DIR: str = "/app/export_results"

    # --- JWT / Auth -----------------------------------------------------
    JWT_SECRET_KEY: str = "dev-secret-change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = 12

    # --- Display timezone (Data Quality & Usability: UTC/local consistency) --
    # Every timestamp is STORED in the database as UTC (see models.py's
    # utc_now() docstring) -- that part is correct and stays unchanged. The
    # bug this setting fixes is at the DISPLAY layer: the live UI (Audit
    # Trail, My Items, etc.) already converts UTC -> the viewer's own
    # browser-local time via js/ui.js's formatTimestamp(), but the
    # server-generated CSV/PDF exports (audit ledger, properties-assigned
    # reports) used to print the raw UTC wall-clock numbers with a literal
    # "UTC" label instead -- correct in an absolute sense, but an hour (or
    # more) off from what the Audit Trail on-screen shows for anyone
    # outside UTC, e.g. Lagos/WAT (UTC+1). Since a static export file has
    # no browser to localize into, it needs ONE fixed timezone to render
    # into instead -- this is that timezone. Every export now converts UTC
    # timestamps into DISPLAY_TIMEZONE before formatting, and labels the
    # result with that zone's real abbreviation (e.g. "WAT") instead of a
    # hardcoded "UTC", so the exported hour always matches the Audit Trail
    # hour a person just looked at on screen. Must be an IANA tz name
    # (e.g. "Africa/Lagos", "America/New_York", "UTC").
    DISPLAY_TIMEZONE: str = "Africa/Lagos"

    # --- CORS -------------------------------------------------------------
    # Comma-separated list of origins allowed to call this API. Defaults
    # cover local Docker Compose usage out of the box.
    CORS_ORIGINS: str = "http://localhost:8080,http://127.0.0.1:8080,http://localhost,http://127.0.0.1"

    # --- Startup behavior (Operations & Observability requirement #1) -----
    # `main.py` used to call `init_db()` (create tables) and `seed_db()`
    # (insert demo accounts/data) unconditionally at MODULE IMPORT TIME --
    # meaning they ran the instant Python read the file, before FastAPI even
    # existed yet. That's dangerous in production for two reasons:
    #   1. Anything that merely *imports* main.py (a test runner, a one-off
    #      script, a second worker process) would silently touch the
    #      database as a side effect of importing, with no way to opt out.
    #   2. Once Alembic migrations are the source of truth for schema
    #      changes (see backend/alembic/), you do NOT want `create_all()`
    #      racing against `alembic upgrade head` on every deploy, and you
    #      almost certainly never want demo accounts (SuperAdmin123!, etc.)
    #      silently created against a real production database.
    #
    # These two flags move that decision out of the code and into
    # configuration: `main.py` now only calls init_db()/seed_db() from
    # inside a proper FastAPI startup hook, and only if the matching flag
    # here is true. In production, set both to "false" in your `.env` and
    # run `alembic upgrade head` as an explicit, separate deploy step
    # instead (see README.md's "Database Migrations" section).
    AUTO_INIT_DB: bool = True
    AUTO_SEED_DEMO_DATA: bool = True

    # --- Structured logging (Operations & Observability requirement #2) ---
    # LOG_LEVEL: standard Python logging level name (DEBUG/INFO/WARNING/...).
    # LOG_FORMAT: "json" (one structured JSON object per line -- what you
    #   want in any real deployment, since log aggregators like
    #   CloudWatch/Datadog/ELK can parse it directly) or "text" (a
    #   human-friendly single-line format, easier to eyeball while
    #   developing locally). See backend/logging_config.py.
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    # --- Login rate limiting (Operations & Observability requirement #3) --
    # POST /auth/login allows at most LOGIN_RATE_LIMIT_MAX attempts per
    # LOGIN_RATE_LIMIT_WINDOW_SECONDS from any single client IP address
    # before it starts returning HTTP 429. See
    # backend/middleware/rate_limit.py for the implementation and its
    # documented limitations (it's in-memory, per-process).
    LOGIN_RATE_LIMIT_MAX: int = 5
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 60

    # --- Per-account brute-force lockout (SECURITY) ------------------------
    # See models.py's User.failed_login_attempts/locked_until docstring for
    # the full rationale. After ACCOUNT_LOCKOUT_MAX_ATTEMPTS consecutive
    # wrong-password attempts against the SAME account, that account is
    # locked for ACCOUNT_LOCKOUT_DURATION_MINUTES -- independent of the IP
    # the attempts came from, and independent of/in addition to the
    # IP-based limiter above.
    ACCOUNT_LOCKOUT_MAX_ATTEMPTS: int = 5
    ACCOUNT_LOCKOUT_DURATION_MINUTES: int = 15

    # --- API documentation exposure (SECURITY) -----------------------------
    # Controls whether FastAPI's interactive docs (/docs -- Swagger UI),
    # /redoc, and the raw machine-readable schema (/openapi.json) exist at
    # all. Defaults to True so local `docker compose up` still gives you the
    # handy interactive docs at http://localhost:8080/docs out of the box.
    #
    # In Render or any other environment reachable from the public internet,
    # set this to "false". This is read by BOTH layers that currently expose
    # these routes, as defense in depth:
    #   1. backend/main.py passes it into FastAPI's docs_url/redoc_url/
    #      openapi_url constructor args -- when disabled, FastAPI doesn't
    #      just hide the UI, it never generates or serves the schema at all,
    #      so there's no lower-effort way to reconstruct your entire API
    #      surface (every route, every request/response field) than just
    #      reading this repo's source, which is no worse than any other
    #      open-source app.
    #   2. nginx/default.conf.template ALSO gates its own /docs, /redoc, and
    #      /openapi.json proxy block on this same flag (via the identically-
    #      named ENABLE_API_DOCS variable passed to the frontend container --
    #      see docker-compose.yml/render.yaml), so a misconfigured or
    #      out-of-date backend doesn't become the only thing standing
    #      between these routes and the public internet.
    ENABLE_API_DOCS: bool = True

    # --- Root Administrator identity (root account) -------------------------
    # "super_admin" IS a real row in the `users` table now -- exactly one,
    # bootstrapped by `alembic/versions/0002_bootstrap_root_admin.py` the
    # first time `alembic upgrade head` runs against a production database
    # (see that file's module docstring for the full rationale). Neither
    # value below is hardcoded/frozen identity anymore -- both are just the
    # INITIAL seed used the one time the bootstrap migration inserts the
    # row. The PASSWORD is never read from an environment variable at
    # runtime -- it's a normal Argon2id hash in `password_hash`, just like
    # every other account, so it can be rotated (self-service
    # change-password, or an Admin-issued reset) and every login/rotation
    # goes through the exact same audited code path
    # (services/auth_service.py -> login()/update_password(),
    # services/user_service.py -> reset_user_password()) as any other
    # user. There is deliberately no SUPER_ADMIN_PASSWORD setting anymore
    # -- see security.py's module docstring for what replaced it.
    #
    # SECURITY CHANGE: username/name/email used to be the one part of this
    # identity that truly never changed after bootstrap -- there was no
    # code path that could touch them. They're now rotatable too, the same
    # self-service way the password already was, via
    # PATCH /auth/me (services/auth_service.py -> update_identity()) --
    # re-confirming the CURRENT password, same as update_password(). This
    # is what finally makes the bootstrap-assigned "{username}@local"
    # placeholder email (see 0002_bootstrap_root_admin.py) replaceable
    # with a real, reachable mailbox -- which POST /auth/forgot-password
    # actually needs somewhere valid to send to.
    #
    # These two values are read directly by the bootstrap migration too
    # (via `os.environ`, NOT by importing this settings object -- see that
    # migration file's comment for why), so set them identically in your
    # `.env` if you want the migration and the running app to agree on the
    # root account's INITIAL username/display name -- whatever the account
    # is later rotated to in the database always wins over these at
    # runtime, exactly like the password already does.
    SUPER_ADMIN_USERNAME: str = "superadmin"
    SUPER_ADMIN_NAME: str = "Super Admin"

    # --- "Forgot password?" self-recovery -----------------------------------
    # Powers POST /auth/forgot-password / POST /auth/reset-password (see
    # services/auth_service.py's request_password_reset()/
    # confirm_password_reset() and models.py's PasswordResetToken). Exists
    # mainly so SUPER_ADMIN_ROLE -- the one account with no admin "above"
    # it to reset a forgotten password for it -- has a real self-recovery
    # path, but works for any account.
    #
    # How long a mailed reset link stays redeemable before it's treated as
    # expired, same "deliberately short-lived" reasoning as
    # security.py's _MFA_TOKEN_EXPIRY_MINUTES -- long enough for someone to
    # receive and click an email, short enough that a link sitting
    # unread/forwarded/leaked in an inbox doesn't stay a standing way into
    # the account indefinitely.
    PASSWORD_RESET_TOKEN_EXPIRY_MINUTES: int = 30

    # NOTE: there used to be a FRONTEND_BASE_URL setting here that
    # request_password_reset() used to build the mailed reset link's base
    # URL (e.g. "https://assets.corp.io" -> ".../reset-password?token=...").
    # It's gone -- a hardcoded/env-configured URL drifts out of sync the
    # moment a deployment's real domain changes (a custom domain gets
    # attached, a staging URL is renamed, etc.) unless someone remembers to
    # also update this setting and redeploy it. The reset link's base URL
    # is now derived directly from the incoming POST /auth/forgot-password
    # request itself -- see api/auth_api.py's _resolve_frontend_base_url()
    # -- so it always matches whatever address the person actually has
    # open, with no separate setting to keep in sync. CORS_ORIGINS is still
    # what makes that safe: the resolved value is checked against it before
    # being trusted (see that function's docstring), so this app's existing
    # "which origins do we trust" list is the only configuration a
    # deployment needs to get right for both CORS AND reset links now.

    # --- Email notifications (extension requests + overdue + due-soon) ----
    # This app sends exactly three kinds of email:
    #   1. Extension-request lifecycle emails (see
    #      services/extension_service.py) -- a Manager/Admin/Super Admin is
    #      emailed the moment a User requests more time on a checkout, and
    #      the requester is emailed back once that request is approved or
    #      denied.
    #   2. A once-a-day overdue-checkout digest (see
    #      backend/tasks/notification_tasks.py) -- emailed to every
    #      overdue item's own holder (if they're a logged-in User with an
    #      email address) AND to every Manager/Admin/the Super Admin
    #      (everything system-wide -- Managers have no department-scoping).
    #   3. A once-a-day "due soon" reminder digest (same file) -- the
    #      proactive counterpart to #2: emailed BEFORE a checkout goes
    #      overdue, to the same audience, for anything due within
    #      DUE_SOON_REMINDER_DAYS (see below) that hasn't passed its due
    #      date yet.
    #
    # NOTIFICATIONS_ENABLED is the single master switch: leave it "false"
    # (the default) for local development with no mail server configured --
    # every notification call below then just logs what it WOULD have sent
    # and returns, instead of failing the whole request/task over a
    # missing SMTP server. Flip it to "true" once SMTP_HOST/PORT/etc. below
    # point at a real mail server (your own Postfix, SendGrid, Mailgun,
    # AWS SES SMTP endpoint, etc. all work identically here -- this is
    # plain RFC 5321 SMTP, no vendor-specific SDK).
    NOTIFICATIONS_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    # STARTTLS (the standard for port 587) vs. a plain/unencrypted
    # connection (only ever appropriate for a mail relay on localhost/the
    # same private network -- never over the public internet).
    SMTP_USE_TLS: bool = True
    # Implicit SSL (the standard for port 465) -- the connection is
    # encrypted from the first byte, instead of starting plain and
    # upgrading via STARTTLS. Useful as a fallback when an ISP/network
    # blocks outbound 587 but allows 465 (common on residential/some
    # corporate networks). Takes priority over SMTP_USE_TLS when both are
    # set -- see send_email() in services/notification_service.py.
    SMTP_USE_SSL: bool = False
    # What shows up in the "From:" header. Many providers (SendGrid,
    # Mailgun, SES) reject sends where this doesn't match a verified
    # domain/sender, so this intentionally has no made-up default.
    SMTP_FROM_EMAIL: str = ""

    # Which transport send_email() (services/notification_service.py)
    # actually uses: "smtp" (default -- plain RFC 5321 SMTP against
    # SMTP_HOST/PORT above, works against ANY provider that speaks SMTP,
    # zero vendor lock-in) or an HTTP-API provider ("brevo"/"resend" below).
    #
    # WHY AN HTTP-API OPTION EXISTS AT ALL, GIVEN THE "BORING, NO VENDOR
    # SDK" PHILOSOPHY (see notification_service.py's own module docstring):
    # Render's Free web service instance type blocks ALL outbound traffic
    # on ports 25/465/587 at the network level -- see
    # https://render.com/docs/free#free-web-services -- so plain SMTP
    # cannot reach ANY provider from a Free Render service, full stop, no
    # amount of application-level retrying or DNS/IP-family fixing gets
    # around a port block. An HTTP-API provider sends over port 443
    # instead, which Free Render services can reach -- the only way to
    # keep email working AND stay on Render's Free plan (this repo's
    # entire premise -- see render.yaml's own top-of-file comment). Set
    # this to "smtp" (or leave unset) for every other deployment target
    # (deploy-azure-vm.yml/deploy-azure-aca.yml, local dev) -- SMTP has no
    # such restriction there.
    EMAIL_PROVIDER: str = "smtp"
    # https://app.brevo.com/settings/keys/api -- free tier: 300 emails/day,
    # forever, no credit card. The SENDER address (SMTP_FROM_EMAIL below)
    # must be verified in Brevo first -- a one-click confirmation link
    # sent to that address (Brevo dashboard > Senders, Domains & Dedicated
    # IPs), NOT full DNS/domain verification -- sends using an unverified
    # sender fail outright. Only read when EMAIL_PROVIDER="brevo".
    BREVO_API_KEY: str = ""
    # https://resend.com/api-keys -- free tier: 3,000 emails/month
    # (100/day). Sending FROM your own domain requires verifying it first
    # (Resend dashboard > Domains); until then, SMTP_FROM_EMAIL must stay
    # on their shared onboarding@resend.dev sender. Only read when
    # EMAIL_PROVIDER="resend".
    RESEND_API_KEY: str = ""

    # Extra recipients who should get EVERY new-extension-request alert
    # (see services/extension_service.py) regardless of role/department --
    # e.g. an IT-operations distribution list that isn't itself a `users`
    # row. Comma-separated, same pattern as CORS_ORIGINS above. Optional --
    # Admins/Managers/the Super Admin are already covered automatically for
    # THAT alert. This is env-level/restart-required config, distinct from
    # the DAILY DIGEST'S recipient list, which is its own runtime-editable
    # setting (PUT /settings/digest-recipients, Admin/Super Admin only --
    # see services/notification_service.py's get_digest_recipient_emails())
    # that is the digest's SOLE audience; being an Admin/Manager no longer
    # implies receiving the daily digest. Addresses here are still also
    # added on top of that list for the digest specifically (see
    # tasks/notification_tasks.py), for anyone who wants one ops address
    # wired into both without duplicating it in two places.
    ADMIN_NOTIFICATION_EMAILS: str = ""

    # What time of day the background worker checks for overdue checkouts
    # and sends the digest email described above. Comma-separated hours of
    # day (UTC, each 0-23), same pattern as BACKUP_HOURS_UTC below --
    # "8" for once a day at 08:00 UTC, or "8,20" for twice a day. Parsed/
    # validated by `overdue_digest_hours_utc_list` below; invalid values
    # (out of 0-23, non-numeric) raise a clear error at startup rather
    # than silently being ignored. See celery_app.py's `beat_schedule`
    # for where this is wired up as a `crontab` schedule (fires at
    # exactly this clock time daily, not "N hours after the worker
    # booted" -- see that file's comment for why a fixed time is what
    # you want for a digest email, same reasoning as the backup
    # scheduler's own BACKUP_HOURS_UTC).
    OVERDUE_DIGEST_HOURS_UTC: str = "8"

    # --- "Due Soon" reminder (a nudge BEFORE something goes overdue) ------
    # DUE_SOON_REMINDER_DAYS is the single source of truth for what counts
    # as "coming up soon": an active checkout with a due_date that's still
    # in the future, but no further out than this many days, is "due
    # soon". Both the dashboard-facing bits (the "Due Soon" banner on
    # admin.html/manager.html, GET /checkouts/due-soon; the amber "Due
    # Soon" badge on staff.html/customer.html's My Items -- see
    # services/user_service.py's `_days_until_due()`) AND the email
    # reminder below read this ONE setting, so raising or lowering the
    # window changes every one of them consistently.
    DUE_SOON_REMINDER_DAYS: int = 2

    # What time of day the background worker checks for checkouts about
    # to go overdue and sends the reminder email described above. Same
    # comma-separated-UTC-hours pattern as OVERDUE_DIGEST_HOURS_UTC just
    # above -- its own independent schedule (e.g. lower it to a couple
    # of minutes from now for local testing) without also changing when
    # the overdue digest fires.
    DUE_SOON_DIGEST_HOURS_UTC: str = "8"

    # Whether the individual "your item is overdue/due soon" reminder is
    # sent to the checkout's own holder (a logged-in User with an email
    # address), IN ADDITION TO the admin/manager digest above. Default
    # true (original behavior). Set to false to send ONLY the digest --
    # e.g. while still testing SMTP delivery/content and not ready for
    # end users to receive anything yet. Flip back to true any time with
    # no code changes -- see tasks/notification_tasks.py.
    SEND_INDIVIDUAL_HOLDER_REMINDERS: bool = True

    # --- Pending-approval SLA nudges (ExtensionRequest & Quotation) -------
    # Both `ExtensionRequest` ("pending" -> approved/denied) and
    # `Quotation` ("submitted" -> approved) sit in a decision queue with no
    # automatic escalation of their own -- a Manager/Admin who never opens
    # the Extension Requests panel or the Quotes tab could otherwise leave
    # one unanswered indefinitely. tasks/sla_tasks.py's two Celery Beat
    # jobs (`escalate_pending_extension_requests`/
    # `escalate_pending_quotations`) close that gap: anything still
    # waiting past the relevant *_SLA_HOURS setting below gets escalated
    # to the SAME notification-recipients audience as every other alert in
    # this app (the runtime-editable Digest Recipients list +
    # ADMIN_NOTIFICATION_EMAILS -- see get_digest_recipient_emails()), not
    # just left for someone to eventually notice.
    #
    # How many hours a `pending` ExtensionRequest can go without a
    # Manager/Admin/Super Admin decision before it's escalated.
    EXTENSION_REQUEST_SLA_HOURS: float = 24
    # Same idea for a `submitted` Quotation waiting on
    # quotation_service.approve_quotation() -- its own independent
    # threshold, since the two queues can reasonably need different
    # response-time expectations.
    QUOTATION_SLA_HOURS: float = 24
    # How often (in minutes) the worker checks both queues for anything
    # that's crossed its SLA threshold above. A plain timedelta-since-
    # last-tick (like AUDIT_PARTITION_CHECK_INTERVAL_HOURS), not a fixed
    # clock time like the daily digests -- "how promptly does an
    # escalation land after crossing the line" is the thing that matters
    # here, not a specific time of day. Cheap and idempotent (almost
    # always a no-op), so a fairly tight default is fine.
    APPROVAL_SLA_CHECK_INTERVAL_MINUTES: int = 60
    # Once a pending request/quote HAS been escalated, how long before
    # it's eligible to be escalated again if STILL nobody has decided it
    # (see models.py's `sla_last_reminded_at` columns) -- keeps a
    # long-neglected item nudging repeatedly rather than firing once and
    # going quiet, without re-sending on every single
    # APPROVAL_SLA_CHECK_INTERVAL_MINUTES tick.
    APPROVAL_SLA_ESCALATION_REPEAT_HOURS: float = 24

    # --- Equipment Quotation self-service (staff/customer asset catalog) --
    # ISO 4217 currency code applied to every price shown/exported anywhere
    # in the app (the Asset Inventory's per-unit `price`, the Quotation
    # Catalog's day-rate, and every line/subtotal/VAT/total on a Quotation
    # PDF export). Defaults to Naira since that's this deployment's home
    # currency; change it here (not in code) for a different market --
    # see js/ui.js's formatPrice() and services/export_service.py's
    # quotation PDF builder, both of which read this same value via
    # GET /config/public rather than hardcoding a symbol.
    CURRENCY_CODE: str = "NGN"

    # --- Brand name shown across the deployment: the on-screen navbar/login
    # brand AND the Quotation PDF's letterhead ---------------------------
    # The frontend's navbar brand + <title> (every page: index.html,
    # admin.html, manager.html, staff.html, customer.html) reads this value
    # at runtime from GET /config/public (see
    # services/quotation_service.py's get_public_config() and
    # js/ui.js's applySiteName(), called once on every page load including
    # the unauthenticated login page) -- there's no separate frontend-only
    # setting to keep in sync. The printable Quotation/Checkout Receipt PDF
    # (services/quotation_service.py's _build_quotation_pdf(), via
    # services/export_service.py's build_quotation_document_pdf()) sources
    # its own letterhead from this exact same setting. Change it here (not
    # in code) to rebrand the on-screen UI and every future PDF export at
    # once, following the exact same env-var pattern as CURRENCY_CODE above.
    SITE_NAME: str = "Snipe-IT Lite"

    # Whether a "staff" or "customer" account browsing the self-service
    # Quotation Catalog (GET /assets/catalog) can see each pool's
    # available-quantity count and in-stock/out-of-stock status.
    #
    # False (the default, and the recommended production value) shows
    # them ONLY name, category, and price -- exactly what's needed to
    # build a quotation -- hiding live stock levels from external/
    # unprivileged accounts. Flip to True if your organization wants
    # staff/customers to see stock availability before requesting it.
    # A Manager/Admin/Super Admin's own Asset Inventory view (GET /assets)
    # is completely unaffected by this flag either way -- they always see
    # full stock detail, same as today.
    CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER: bool = False

    # Whether services/quotation_service.py's _notify_quotation_recipient()
    # (line items added/changed/removed, notes/discount edits, assignment,
    # approval, fulfillment) emails the quote's own recipient (whoever it's
    # currently assigned to, else the original requester) on top of the
    # in-app QuotationNotification row it always creates regardless of this
    # setting. True (the default) preserves the original "email them every
    # time something changes" behavior. Some customers find a message for
    # every single line-item tweak intrusive -- set this False to keep the
    # in-app bell notification (still visible next time they log in/open
    # the Notification Center) while skipping the individual email per
    # update. Independent of NOTIFICATIONS_ENABLED above -- both must be
    # true for one of these emails to actually go out; this is the
    # quotation-specific on/off switch layered on top of that master one.
    SEND_QUOTATION_RECIPIENT_EMAILS: bool = True

    # --- Single-service (free-tier) deployment mode ------------------------
    # Render's Free instance type only supports Web Services, Postgres, and
    # Key Value (Redis) -- Private Services and Background Workers are NOT
    # available on the Free plan at all (see render.yaml's top-of-file
    # comment and README.md's "Deploying on Render's Free Plan" section for
    # the full reasoning). To fit the whole app onto ONE free web service,
    # this same FastAPI process can also serve the static frontend directly
    # and mount every API route under an /api prefix, instead of relying on
    # a separate nginx container to do both of those jobs.
    #
    # SERVE_FRONTEND=false (the default) preserves the original docker-
    # compose.yml behavior, where nginx (built from frontend/Dockerfile) serves
    # frontend/*.html/js/css itself and this backend container only ever
    # answers API requests. render-start.sh sets SERVE_FRONTEND=true for the
    # combined single-service Render image built from Dockerfile.render,
    # which COPYs frontend/ into the image at FRONTEND_DIR.
    SERVE_FRONTEND: bool = False
    FRONTEND_DIR: str = "/app/frontend"

    # --- Embedded Celery worker/beat (no separate worker container) -------
    # Two deployment shapes read this flag and both avoid running a
    # dedicated `worker`/`beat` service the way docker-compose.yml does:
    #   - Render's Free plan has no Background Worker service type at all
    #     (see SERVE_FRONTEND above) -- render-start.sh launches
    #     `celery -A celery_app worker -B` as a background process INSIDE
    #     this web service's single free container when
    #     RUN_EMBEDDED_WORKER=true, sharing this process's Redis (a free
    #     Render Key Value instance) and Postgres connections.
    #   - Azure's cost-optimized layout (infra/main.bicep's `backendApp`)
    #     sets this same flag for the same reason -- one fewer Container
    #     App to pay for -- and backend/start.sh launches the identical
    #     embedded worker command.
    # BUG FIX: backend/start.sh used to not read this variable at all, so
    # setting it on Azure's `backendApp` had no effect -- `.delay(...)`
    # calls (audit export, extension emails) queued into Redis with
    # nothing ever consuming them. start.sh now launches the same embedded
    # worker render-start.sh does.
    #
    # Both scripts pass `-B` (embedded Beat) unconditionally, including on
    # Azure where `backendApp` can scale to more than one replica -- that's
    # safe without any further configuration here because celery_app.py
    # configures RedBeat as the Beat scheduler (`beat_scheduler` +
    # `redbeat_redis_url`), which keeps its own distributed lock in Redis:
    # only one replica is ever the active scheduler at a time, regardless
    # of how many replicas have RUN_EMBEDDED_WORKER=true.
    RUN_EMBEDDED_WORKER: bool = False

    # --- Database backups (pg_dump/pg_restore + optional Google Drive) ----
    # See services/backup_service.py for the full implementation. Backups
    # are gzip-compressed `pg_dump` SQL files. On Render's Free plan the
    # web service's own disk is EPHEMERAL -- it's wiped on every restart
    # and every redeploy -- so a local file is convenient for a quick
    # "oops, undo the last five minutes" restore, but is NOT durable
    # storage on its own. BACKUP_GDRIVE_ENABLED is what makes a backup
    # survive a redeploy/spin-down: every backup this app creates (whether
    # on the daily schedule or via the "Backup Now" button) is uploaded to
    # Google Drive right after it's written locally, and the Admin
    # dashboard's Restore flow accepts an uploaded backup file specifically
    # so you can pull the last good file back down from Drive and restore
    # it even if local disk was wiped in between.
    #
    # ENABLE_AUTO_BACKUP: master switch for the daily scheduled backup (see
    # backup_service.start_backup_scheduler(), called from main.py's
    # startup event). This runs as a plain daemon thread inside THIS same
    # uvicorn process -- unlike RUN_EMBEDDED_WORKER's Celery worker above,
    # it does not depend on Celery/Redis being configured at all, so it
    # works identically whether or not the embedded worker is enabled.
    ENABLE_AUTO_BACKUP: bool = True
    # Comma-separated hours of day (UTC, each 0-23) the scheduled backup
    # runs at -- e.g. "3" for once a day, or "3,15,21" for three times a
    # day. Parsed/validated by `backup_hours_utc_list` below; invalid
    # values (out of 0-23, non-numeric) raise a clear error at startup
    # rather than silently being ignored.
    BACKUP_HOURS_UTC: str = "3"
    # DEPRECATED, kept only for existing deployments that already set this
    # single-value var -- if set, it OVERRIDES BACKUP_HOURS_UTC entirely
    # (so upgrading this app doesn't silently change an existing schedule).
    # New setups should use BACKUP_HOURS_UTC instead. Leave unset (None) to
    # use BACKUP_HOURS_UTC.
    BACKUP_HOUR_UTC: Optional[int] = None
    # Where local backup files (and their index.json metadata file) live
    # inside the container.
    BACKUP_DIR: str = "/app/backups"
    # How many local backup files to keep on disk before deleting the
    # oldest -- keeps the ephemeral disk from filling up. Google Drive
    # (if enabled) keeps its own independent copy, unaffected by this.
    BACKUP_RETENTION_COUNT: int = 7

    # --- Google Drive upload -------------------------------------------
    # TWO auth modes are supported -- pick whichever matches your Google
    # account:
    #
    # MODE 1: OAuth as a real Google user (BACKUP_GDRIVE_OAUTH_* below) --
    # what you want for a PERSONAL Gmail account. Uploads count against
    # that person's own 15GB Drive quota, same as uploading through the
    # Drive web UI by hand. See backend/scripts/gdrive_oauth_setup.py for
    # the one-time setup that gets you a refresh token.
    #
    # MODE 2: a Google Cloud "service account" (BACKUP_GDRIVE_CREDENTIALS_
    # JSON below) -- only works if BACKUP_GDRIVE_FOLDER_ID points at a
    # SHARED DRIVE (Google Workspace only; personal/consumer Google
    # accounts cannot create Shared Drives at all). Service accounts have
    # ZERO Drive storage quota of their own -- even a folder a real person
    # shares with the service account as "Editor" doesn't help, because
    # any file the service account creates there is still OWNED by the
    # service account and billed against ITS (nonexistent) quota. That's
    # exactly the "Service Accounts do not have storage quota... 
    # storageQuotaExceeded" error this setup produces on a personal
    # account -- a Shared Drive is the one place storage is billed to the
    # drive itself rather than the file's creator, which is why this mode
    # needs one.
    #
    # If BOTH are configured, OAuth (Mode 1) takes priority -- see
    # backup_service.py's upload_to_gdrive().
    BACKUP_GDRIVE_ENABLED: bool = False

    # --- Mode 1: OAuth (personal Google account) ---
    BACKUP_GDRIVE_OAUTH_CLIENT_ID: str = ""
    BACKUP_GDRIVE_OAUTH_CLIENT_SECRET: str = ""
    BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN: str = ""

    # --- Mode 2: service account (Google Workspace + Shared Drive only) ---
    BACKUP_GDRIVE_CREDENTIALS_JSON: str = ""

    # The destination folder's Drive ID (the long ID in the folder's URL:
    # https://drive.google.com/drive/folders/<THIS_PART>). Required either
    # way. For Mode 1, any regular "My Drive" folder works fine -- create
    # one, grab its ID from the URL, done (no sharing step needed, since
    # you're uploading as yourself). For Mode 2 it must be a folder INSIDE
    # a Shared Drive, per the docstring above.
    BACKUP_GDRIVE_FOLDER_ID: str = ""

    # --- Distributed tracing (OpenTelemetry) -------------------------------
    # See backend/telemetry.py's module docstring for the full "why" and
    # how these wire up. Short version: OTEL_ENABLED is the single master
    # switch (default OFF -- zero cost, zero behavior/performance change,
    # matching every other opt-in flag in this file). Flip it on and point
    # OTEL_EXPORTER_OTLP_ENDPOINT at ANY OTLP/HTTP-compatible backend --
    # a local Jaeger/otel-collector (see docker-compose.yml's `jaeger`
    # service), Application Insights' own OTLP ingestion endpoint, Grafana
    # Cloud, Honeycomb, etc. -- and every FastAPI request, SQLAlchemy
    # query, Celery task, and Redis command in this process starts
    # emitting spans automatically, correlated with the existing
    # structured logs (see logging_config.py's module docstring) via
    # trace_id/span_id.
    OTEL_ENABLED: bool = False
    # service.name resource attribute every span from this process carries
    # -- what you'll see identifying this app in your tracing backend's UI.
    # main.py appends nothing to this (it's already "the backend"); the
    # embedded Celery worker/beat processes append their own "-worker"/
    # "-beat" suffix on top of this value (see celery_app.py) so a trace
    # that crosses from an API request into a queued background task is
    # still easy to tell apart by service name in a trace waterfall view.
    OTEL_SERVICE_NAME: str = "snipeit-lite-backend"
    # Free-text version tag attached as the service.version resource
    # attribute -- bump this alongside CHANGELOG.md/git tags if you want
    # traces groupable by release; purely cosmetic otherwise.
    OTEL_SERVICE_VERSION: str = "0.1.0"
    # The OTLP/HTTP collector endpoint spans are exported to, e.g.
    # "http://localhost:4318" for a local Jaeger/otel-collector, or your
    # tracing backend's own OTLP ingestion URL. Empty (the default) means
    # "nowhere to export to" -- see OTEL_CONSOLE_EXPORTER below for a
    # zero-infrastructure way to see spans locally instead. Only read at
    # all when OTEL_ENABLED is true.
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    # Comma-separated key=value pairs sent as extra HTTP headers on every
    # export request -- e.g. "x-honeycomb-team=<api-key>" or
    # "Authorization=Bearer <token>", whatever your tracing backend's OTLP
    # endpoint requires for auth. Treated as a secret everywhere this app
    # is deployed (Container Apps `secrets`, a git-ignored `.env`) since it
    # commonly carries an API key.
    OTEL_EXPORTER_OTLP_HEADERS: str = ""
    # "http/protobuf" (the default, talks to the endpoint above over plain
    # HTTPS/HTTP -- no extra native dependency, works through any outbound
    # HTTP proxy/firewall) or "grpc" (lower overhead, but requires the
    # grpcio package and a raw HTTP/2 connection some corporate networks
    # block). See telemetry.py's setup_tracing() for where this is read.
    OTEL_EXPORTER_OTLP_PROTOCOL: str = "http/protobuf"
    # Fraction of traces actually sampled and exported, applied at the ROOT
    # span of each trace (ParentBased -- any request whose parent span was
    # already sampled upstream is always sampled too, regardless of this
    # ratio, so a trace is never split across sampled/unsampled pieces).
    # 1.0 (the default) exports every trace; lower it (e.g. 0.1 for ~10%)
    # if export volume/cost ever becomes a concern at higher traffic.
    OTEL_TRACES_SAMPLE_RATIO: float = 1.0
    # Also print every span to stdout as it finishes, in addition to (or
    # instead of) exporting to OTEL_EXPORTER_OTLP_ENDPOINT. Handy for a
    # first local smoke-test ("is instrumentation actually firing at all")
    # with zero collector/Jaeger setup -- noisy, so leave this off anywhere
    # you're also shipping structured logs to a real aggregator.
    OTEL_CONSOLE_EXPORTER: bool = False
    # Routes spans straight to an Azure Application Insights resource
    # instead of (or alongside) OTEL_EXPORTER_OTLP_ENDPOINT -- the
    # standard env var name Azure's own tooling (App Service, Azure
    # Functions, the Azure Monitor OpenTelemetry Distro) already looks
    # for, kept identical here on purpose so a value copied from the
    # Azure Portal or `az monitor app-insights component show` just
    # works. Looks like
    # "InstrumentationKey=<guid>;IngestionEndpoint=https://<region>.in.applicationinsights.azure.com/".
    # See infra/main.bicep's `otelAzureMonitorEnabled` param for the
    # one-line way to have Azure provision the resource and this value
    # for you, and README.md's "Distributed Tracing" section for how to
    # actually find your traces once they're flowing. Empty (the
    # default) skips this exporter entirely -- only read at all when
    # OTEL_ENABLED is true.
    APPLICATIONINSIGHTS_CONNECTION_STRING: str = ""

    # --- Audit log partition maintenance (services/audit_partition_service.py) --
    # `audit_logs` is a native Postgres table PARTITIONED BY RANGE on
    # `timestamp`, one partition per calendar year (see
    # alembic/versions/0010_partition_audit_logs.py's module docstring for
    # the full "why"). These two settings control the ONE automated part
    # of that system -- keeping future years' partitions pre-created so
    # writes never fail once the calendar rolls over. They do NOT control
    # retiring old years: that stays a deliberate, manual, once-a-year (or
    # "whenever disk space actually requires it") DB-ops action -- see
    # SRE_STRATEGY.md's "Audit log partitioning & annual archive" section
    # for that runbook. Nothing in this app ever drops a partition on its
    # own.
    #
    # How many years of FUTURE partitions to keep pre-created at all times,
    # on top of the current year -- e.g. 2 means "this year, plus the next
    # two" always exist before they're needed. A generous buffer costs
    # nothing (an empty partition is just a few hundred bytes of catalog
    # metadata) and means the scheduled check below can miss a run or two
    # (a redeploy, Redis being down, etc.) without ever risking an insert
    # falling through to the DEFAULT catch-all partition (see that
    # migration's docstring for why the default partition exists as a
    # safety net even so).
    AUDIT_PARTITION_YEARS_AHEAD: int = 2
    # How often (in hours) the worker checks that the partitions above
    # still exist and creates any that don't -- see
    # tasks/audit_partition_tasks.py and celery_app.py's beat_schedule.
    # Cheap and idempotent (a no-op almost every time it runs), so a
    # once-a-day cadence is generous, not aggressive.
    AUDIT_PARTITION_CHECK_INTERVAL_HOURS: float = 24

    # Tell Pydantic Settings v2 to also look for a `.env` file (useful when
    # running the backend directly with `uvicorn main:app` outside Docker,
    # where environment variables aren't injected by docker-compose.yml).
    # env_ignore_empty: docker-compose.yml passes through vars like
    # `BACKUP_HOUR_UTC: ${BACKUP_HOUR_UTC:-}` so an unset .env value arrives
    # here as a literal empty string, not as "truly absent" -- without this,
    # that empty string would fail to parse as int/bool for fields like
    # BACKUP_HOUR_UTC below and crash the app on boot. Every other field in
    # this file already treats an empty string the same as its default
    # value, so this has no effect anywhere else.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    @property
    def cors_origin_list(self) -> list[str]:
        """Split the comma-separated CORS_ORIGINS string into a clean list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def admin_notification_email_list(self) -> list[str]:
        """Split the comma-separated ADMIN_NOTIFICATION_EMAILS string into a clean list."""
        return [email.strip() for email in self.ADMIN_NOTIFICATION_EMAILS.split(",") if email.strip()]

    @property
    def backup_hours_utc_list(self) -> list[int]:
        """
        Parses BACKUP_HOURS_UTC ("3" or "3,15,21") into a sorted, deduped
        list of ints, same comma-separated-string pattern as
        cors_origin_list/admin_notification_email_list above. If the
        deprecated single-value BACKUP_HOUR_UTC is set, it wins outright
        (see that field's own docstring for why) and BACKUP_HOURS_UTC is
        ignored entirely.
        """
        if self.BACKUP_HOUR_UTC is not None:
            return [self.BACKUP_HOUR_UTC]
        return _parse_utc_hours_csv(self.BACKUP_HOURS_UTC, "BACKUP_HOURS_UTC", default_hour=3)

    @property
    def overdue_digest_hours_utc_list(self) -> list[int]:
        """Parses OVERDUE_DIGEST_HOURS_UTC -- see backup_hours_utc_list above for the shared parsing rules."""
        return _parse_utc_hours_csv(self.OVERDUE_DIGEST_HOURS_UTC, "OVERDUE_DIGEST_HOURS_UTC", default_hour=8)

    @property
    def due_soon_digest_hours_utc_list(self) -> list[int]:
        """Parses DUE_SOON_DIGEST_HOURS_UTC -- see backup_hours_utc_list above for the shared parsing rules."""
        return _parse_utc_hours_csv(self.DUE_SOON_DIGEST_HOURS_UTC, "DUE_SOON_DIGEST_HOURS_UTC", default_hour=8)

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() in ("prod", "production")

    # -----------------------------------------------------------------
    # STARTUP CHECK (Critical Stability & Security requirement #5)
    # -----------------------------------------------------------------
    # Runs once, the moment `Settings()` is instantiated below -- i.e. at
    # process import time, before FastAPI has accepted a single request.
    # If ENVIRONMENT=production and JWT_SECRET_KEY is still a placeholder
    # (or is simply too short/guessable), we raise here so the container
    # fails fast and loudly on boot instead of silently running with a
    # forgeable session-signing key.
    @model_validator(mode="after")
    def _enforce_prod_jwt_secret(self) -> "Settings":
        if not self.is_production:
            return self

        if self.JWT_SECRET_KEY in _INSECURE_JWT_SECRETS:
            raise ValueError(
                "Refusing to start: ENVIRONMENT=production but JWT_SECRET_KEY is still "
                "a placeholder/default value. Generate a real secret and set it in your "
                "environment/.env file, e.g.: "
                'python3 -c "import secrets; print(secrets.token_hex(32))"'
            )

        if len(self.JWT_SECRET_KEY) < _MIN_PROD_JWT_SECRET_LENGTH:
            raise ValueError(
                f"Refusing to start: ENVIRONMENT=production but JWT_SECRET_KEY is only "
                f"{len(self.JWT_SECRET_KEY)} character(s) long. It must be at least "
                f"{_MIN_PROD_JWT_SECRET_LENGTH} characters of random data, e.g.: "
                'python3 -c "import secrets; print(secrets.token_hex(32))"'
            )

        return self

    # -----------------------------------------------------------------
    # STARTUP CHECK: DISPLAY_TIMEZONE is a real IANA zone name
    # -----------------------------------------------------------------
    # Same fail-fast-at-import-time reasoning as _enforce_prod_jwt_secret
    # above: a typo'd zone name (e.g. "Africa/Lagoss") would otherwise only
    # blow up the first time someone generates an export, hours or days
    # after deploy. Resolving it once here means the container refuses to
    # boot instead.
    @model_validator(mode="after")
    def _validate_display_timezone(self) -> "Settings":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.DISPLAY_TIMEZONE)
        except ZoneInfoNotFoundError:
            raise ValueError(
                f"Refusing to start: DISPLAY_TIMEZONE '{self.DISPLAY_TIMEZONE}' is not a "
                "recognized IANA timezone name (e.g. 'Africa/Lagos', 'America/New_York', 'UTC')."
            )
        return self

    # -----------------------------------------------------------------
    # STARTUP CHECK: OTEL_TRACES_SAMPLE_RATIO is a valid fraction
    # -----------------------------------------------------------------
    # Same fail-fast-at-import-time reasoning as _enforce_prod_jwt_secret
    # above: TraceIdRatioBased (see telemetry.py) raises its own ValueError
    # for an out-of-range ratio, but only the first time OTEL_ENABLED=true
    # actually builds a TracerProvider -- which could be well after boot.
    # Checking it here means a typo'd value (e.g. "1.5" or a negative
    # number) fails the container at startup instead, regardless of
    # whether OTEL_ENABLED even ends up true.
    @model_validator(mode="after")
    def _validate_otel_sample_ratio(self) -> "Settings":
        if not 0.0 <= self.OTEL_TRACES_SAMPLE_RATIO <= 1.0:
            raise ValueError(
                f"Refusing to start: OTEL_TRACES_SAMPLE_RATIO must be between 0.0 and "
                f"1.0 (got {self.OTEL_TRACES_SAMPLE_RATIO})."
            )
        return self

    # -----------------------------------------------------------------
    # STARTUP CHECK: BACKUP_HOURS_UTC is well-formed
    # -----------------------------------------------------------------
    # backup_hours_utc_list (above) is a lazy @property -- without this,
    # a typo like BACKUP_HOURS_UTC="3,25" would only surface when the
    # scheduler thread first reads it, which could be hours after boot
    # (whenever the next scheduled run was supposed to be). Same
    # fail-fast-at-import-time approach as _enforce_prod_jwt_secret above.
    @model_validator(mode="after")
    def _validate_backup_hours(self) -> "Settings":
        self.backup_hours_utc_list
        return self

    # -----------------------------------------------------------------
    # STARTUP CHECK: OVERDUE_DIGEST_HOURS_UTC / DUE_SOON_DIGEST_HOURS_UTC
    # are well-formed
    # -----------------------------------------------------------------
    # Same fail-fast-at-import-time reasoning as _validate_backup_hours
    # just above -- overdue_digest_hours_utc_list/due_soon_digest_hours_utc_list
    # are lazy @properties that celery_app.py's beat_schedule only reads
    # once, at worker/beat process boot, so without this a typo would
    # otherwise surface as an opaque crontab ValueError from deep inside
    # Celery's own startup instead of this app's own clear message.
    @model_validator(mode="after")
    def _validate_digest_hours(self) -> "Settings":
        self.overdue_digest_hours_utc_list
        self.due_soon_digest_hours_utc_list
        return self


# A single, shared instance every other module imports. Pydantic Settings
# reads real process environment variables (set by docker-compose.yml) and
# falls back to the ".env" file, and finally to the defaults above --
# in that order of precedence.
settings = Settings()
