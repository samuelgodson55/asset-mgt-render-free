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

# Same idea as _INSECURE_JWT_SECRETS above, but for the hardcoded Super
# Admin's password (see the SUPER_ADMIN_* settings below and
# security.py's super_admin_principal()). If ANY of these is still the
# active SUPER_ADMIN_PASSWORD while ENVIRONMENT=production, anyone who has
# read this public repo can log in as the one account that can never be
# deleted and always has full privileges.
_INSECURE_SUPER_ADMIN_PASSWORDS = {
    "",
    "change-this-super-admin-password",
    "RootAccess123!",
}
_MIN_PROD_SUPER_ADMIN_PASSWORD_LENGTH = 12


class Settings(BaseSettings):
    """
    Each attribute below maps 1:1 to an environment variable of the same
    name (case-insensitive). Pydantic Settings v2 validates types
    automatically -- e.g. if JWT_EXPIRY_HOURS isn't a valid integer, the
    app will fail fast at startup with a clear error instead of crashing
    later mid-request.
    """

    # --- Environment ----------------------------------------------------
    # "development" (default, safe for local docker compose) or
    # "production". Drives the JWT-secret startup check below -- it never
    # changes runtime behavior beyond that, so it's safe to leave unset
    # locally.
    ENVIRONMENT: str = "development"

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
    # REDIS_URL is used as BOTH the Celery broker (where jobs queue up) and
    # the Celery result backend (where a finished job's file bytes are
    # stashed, base64-encoded, until the frontend downloads them). "redis"
    # is the docker-compose service name below, same pattern as
    # DATABASE_URL's "db" hostname above.
    REDIS_URL: str = "redis://redis:6379/0"
    # How long a finished export's result (and its file bytes) stays in
    # Redis before expiring, in seconds. Long enough for a normal
    # "click export, wait a few seconds, download" flow with some slack
    # for a slow connection; short enough that finished export files
    # don't sit in Redis forever if nobody downloads them.
    EXPORT_RESULT_TTL_SECONDS: int = 3600

    # --- JWT / Auth -----------------------------------------------------
    JWT_SECRET_KEY: str = "dev-secret-change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = 12

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

    # --- Hardcoded Super Admin (root account) ------------------------------
    # Unlike every other role (manager/admin/staff/customer), "super_admin"
    # is NOT a row in the `users` table -- it's a single fixed identity
    # defined entirely by these three settings, checked directly against
    # the login form's `identifier` field before the database is ever
    # queried (see services/auth_service.py -> login()). This is what
    # guarantees there is always EXACTLY one Super Admin, that it can never
    # be created/edited/deleted through the app (there's no row to delete),
    # and that it never shows up in the User Directory or any other listing
    # (those all query the `users` table, which this account never touches).
    #
    # SUPER_ADMIN_PASSWORD has NO safe built-in default the way most other
    # settings do -- see _enforce_prod_super_admin_password below, which
    # refuses to boot in production if this is still empty or one of the
    # obviously-public placeholder values. Locally, leave it unset (or use
    # the placeholder in .env.example) and this login path simply won't
    # activate -- normal DB-backed accounts (see database.py's seed_db())
    # still work fine without it.
    SUPER_ADMIN_USERNAME: str = "superadmin"
    SUPER_ADMIN_NAME: str = "Super Admin"
    SUPER_ADMIN_PASSWORD: str = ""

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
    # What shows up in the "From:" header. Many providers (SendGrid,
    # Mailgun, SES) reject sends where this doesn't match a verified
    # domain/sender, so this intentionally has no made-up default.
    SMTP_FROM_EMAIL: str = ""

    # Extra recipients who should get EVERY overdue-checkout digest and
    # EVERY new-extension-request alert, regardless of role/department --
    # e.g. an IT-operations distribution list that isn't itself a `users`
    # row. Comma-separated, same pattern as CORS_ORIGINS above. Optional --
    # Admins/Managers/the Super Admin are already covered automatically
    # (see tasks/notification_tasks.py and services/extension_service.py).
    ADMIN_NOTIFICATION_EMAILS: str = ""

    # How often the background worker checks for overdue checkouts and
    # sends the digest email described above. See celery_app.py's
    # `beat_schedule` for where this is wired up. 24 hours is a sane
    # default for a "your item is overdue" reminder -- lower it for
    # testing (e.g. to a few minutes) if you want to see it fire sooner.
    OVERDUE_NOTIFICATION_INTERVAL_HOURS: int = 24

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
    DUE_SOON_NOTIFICATION_INTERVAL_HOURS: int = 24

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
    # compose.yml behavior, where nginx (built from nginx/Dockerfile) serves
    # frontend/*.html/js/css itself and this backend container only ever
    # answers API requests. render-start.sh sets SERVE_FRONTEND=true for the
    # combined single-service Render image built from Dockerfile.render,
    # which COPYs frontend/ into the image at FRONTEND_DIR.
    SERVE_FRONTEND: bool = False
    FRONTEND_DIR: str = "/app/frontend"

    # --- Embedded Celery worker/beat (free-tier deployment mode) -----------
    # Render's Free plan has no Background Worker service type at all (see
    # SERVE_FRONTEND above), so there's nowhere to run `celery -A celery_app
    # worker -B` as its own service the way docker-compose.yml's `worker`
    # container does. render-start.sh instead launches that same Celery
    # command as a background process INSIDE this web service's single
    # container when RUN_EMBEDDED_WORKER=true, sharing this process's Redis
    # (a free Render Key Value instance) and Postgres connections. This is a
    # deliberate free-tier tradeoff, not a general recommendation -- see
    # README.md for its caveats (the worker dies/restarts along with the web
    # service on every free-instance spin-down and redeploy).
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
    # Hour of day (UTC, 0-23) the daily scheduled backup runs at.
    BACKUP_HOUR_UTC: int = 3
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
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        """Split the comma-separated CORS_ORIGINS string into a clean list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def admin_notification_email_list(self) -> list[str]:
        """Split the comma-separated ADMIN_NOTIFICATION_EMAILS string into a clean list."""
        return [email.strip() for email in self.ADMIN_NOTIFICATION_EMAILS.split(",") if email.strip()]

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
    # STARTUP CHECK: hardcoded Super Admin password
    # -----------------------------------------------------------------
    # Same rationale as _enforce_prod_jwt_secret above: refuse to boot in
    # production if SUPER_ADMIN_PASSWORD is still empty or a placeholder
    # value anyone can read straight out of this public repo. Unlike the
    # JWT secret, this one is allowed to be empty/placeholder in
    # development -- an empty value simply disables the Super Admin login
    # path entirely (see auth_service.py -> login()), rather than logging
    # anyone in with a blank password.
    @model_validator(mode="after")
    def _enforce_prod_super_admin_password(self) -> "Settings":
        if not self.is_production:
            return self

        if self.SUPER_ADMIN_PASSWORD in _INSECURE_SUPER_ADMIN_PASSWORDS:
            raise ValueError(
                "Refusing to start: ENVIRONMENT=production but SUPER_ADMIN_PASSWORD is "
                "still empty or a placeholder/default value. Set a real, unique password "
                "for the hardcoded Super Admin account in your environment/.env file."
            )

        if len(self.SUPER_ADMIN_PASSWORD) < _MIN_PROD_SUPER_ADMIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Refusing to start: ENVIRONMENT=production but SUPER_ADMIN_PASSWORD is "
                f"only {len(self.SUPER_ADMIN_PASSWORD)} character(s) long. It must be at "
                f"least {_MIN_PROD_SUPER_ADMIN_PASSWORD_LENGTH} characters long."
            )

        return self


# A single, shared instance every other module imports. Pydantic Settings
# reads real process environment variables (set by docker-compose.yml) and
# falls back to the ".env" file, and finally to the defaults above --
# in that order of precedence.
settings = Settings()
