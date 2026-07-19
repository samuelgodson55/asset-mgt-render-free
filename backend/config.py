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

        raw_lean_mode = data.get("LEAN_MODE")
        lean_mode = None
        if raw_lean_mode is None:
            lean_mode = is_production
        else:
            lean_mode = str(raw_lean_mode).strip().lower() in {"1", "true", "yes", "on"}

        if "LEAN_MODE" not in data:
            data["LEAN_MODE"] = lean_mode
        if "ENABLE_API_DOCS" not in data:
            data["ENABLE_API_DOCS"] = False if lean_mode else True
        if "AUTO_INIT_DB" not in data:
            data["AUTO_INIT_DB"] = False if lean_mode else True
        if "AUTO_SEED_DEMO_DATA" not in data:
            data["AUTO_SEED_DEMO_DATA"] = False if lean_mode else True
        if "ENABLE_AUTO_BACKUP" not in data:
            data["ENABLE_AUTO_BACKUP"] = False if lean_mode else True
        if "LOG_LEVEL" not in data:
            data["LOG_LEVEL"] = "WARNING" if lean_mode else "INFO"
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
    LEAN_MODE: bool = False

    # --- Database -----------------------------------------------------
    # Full SQLAlchemy connection string. In Docker Compose, "db" is the
    # hostname because Compose puts every service on the same network and
    # lets them reach each other by service name.
    DATABASE_URL: str = "postgresql://admin:supersecret@db:5432/asset_db"

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
    # (see that file's module docstring for the full rationale). Only the
    # IDENTITY (username/display name) is hardcoded/configured here; the
    # PASSWORD is never read from an environment variable at runtime --
    # it's a normal Argon2id hash in `password_hash`, just like every other
    # account, so it can be rotated (self-service change-password, or an
    # Admin-issued reset) and every login/rotation goes through the exact
    # same audited code path (services/auth_service.py -> login()/
    # update_password(), services/user_service.py -> reset_user_password())
    # as any other user. There is deliberately no SUPER_ADMIN_PASSWORD
    # setting anymore -- see security.py's module docstring for what
    # replaced it.
    #
    # These two values are read directly by the bootstrap migration too
    # (via `os.environ`, NOT by importing this settings object -- see that
    # migration file's comment for why), so set them identically in your
    # `.env` if you want the migration and the running app to agree on the
    # root account's username/display name.
    SUPER_ADMIN_USERNAME: str = "superadmin"
    SUPER_ADMIN_NAME: str = "Super Admin"

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

    # How often the background worker checks for overdue checkouts and
    # sends the digest email described above. See celery_app.py's
    # `beat_schedule` for where this is wired up. 24 hours is a sane
    # default for a "your item is overdue" reminder -- lower it for
    # testing (e.g. to a few minutes) if you want to see it fire sooner.
    OVERDUE_NOTIFICATION_INTERVAL_HOURS: float = 24

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

    # How often the background worker checks for checkouts about to go
    # overdue and sends the reminder email described above. Same
    # timedelta-since-boot reasoning as OVERDUE_NOTIFICATION_INTERVAL_HOURS
    # just above -- see celery_app.py's `beat_schedule` comment.
    DUE_SOON_NOTIFICATION_INTERVAL_HOURS: float = 24

    # Whether the individual "your item is overdue/due soon" reminder is
    # sent to the checkout's own holder (a logged-in User with an email
    # address), IN ADDITION TO the admin/manager digest above. Default
    # true (original behavior). Set to false to send ONLY the digest --
    # e.g. while still testing SMTP delivery/content and not ready for
    # end users to receive anything yet. Flip back to true any time with
    # no code changes -- see tasks/notification_tasks.py.
    SEND_INDIVIDUAL_HOLDER_REMINDERS: bool = True

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

        hours: set[int] = set()
        for part in self.BACKUP_HOURS_UTC.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                hour = int(part)
            except ValueError:
                raise ValueError(f"BACKUP_HOURS_UTC contains a non-numeric hour: '{part}'.")
            if not 0 <= hour <= 23:
                raise ValueError(f"BACKUP_HOURS_UTC contains an out-of-range hour: {hour} (must be 0-23).")
            hours.add(hour)
        return sorted(hours) or [3]

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


# A single, shared instance every other module imports. Pydantic Settings
# reads real process environment variables (set by docker-compose.yml) and
# falls back to the ".env" file, and finally to the defaults above --
# in that order of precedence.
settings = Settings()
