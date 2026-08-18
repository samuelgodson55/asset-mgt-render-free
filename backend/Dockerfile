# syntax=docker/dockerfile:1
# =============================================================================
# backend/Dockerfile
# -----------------------------------------------------------------------------
# Multi-stage build:
#   1. "builder" installs Python deps into a venv (has network access, no
#      need to be lean -- it's discarded and never shipped).
#   2. The final stage only copies that venv + the app code into a fresh,
#      minimal base -- no pip download cache, no build metadata, no
#      requirements.txt-induced layer churn ever reaches the image that
#      actually gets deployed/pulled/started.
# See start.sh for the other half of this change (ENVIRONMENT-aware CMD,
# replacing an unconditional --reload that was slowing down every startup,
# including production's).
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1/2: builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# A venv (rather than installing into the system site-packages) is what
# makes "just copy this one directory into the final stage" possible below
# -- system site-packages would be entangled with the base image's own
# Python install.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Copy requirements and install dependencies first so Docker can cache this
# layer and skip re-installing packages every time only the app code changes.
COPY backend/requirements.txt /app/
#
# BUG FIX -- this RUN used to `apt-get install build-essential libpq-dev`
# before `pip install`, on the assumption that some dependency needed a C
# compiler to build. On a from-scratch build that apt-get step alone was
# the single biggest cost in the entire image build (~87 MB of gcc/g++/
# binutils/etc. downloaded, installed, then immediately removed again a few
# lines later -- 10+ minutes on an ordinary connection, all of it pure
# overhead). It bought nothing: every package this project actually
# depends on -- psycopg2-binary (bundles libpq itself, doesn't need
# libpq-dev at all), cryptography, argon2-cffi-bindings, pydantic-core,
# etc. -- ships a prebuilt manylinux wheel for both amd64 and arm64, so
# pip was never compiling anything; it was only ever installing wheels.
# If a future `requirements.txt` addition genuinely has no wheel for your
# platform, pip's error will say so explicitly ("Failed building wheel
# for X" / "no such option" about a missing compiler) -- that's the
# signal to reintroduce a scoped `apt-get install -y --no-install-
# recommends build-essential <only what X needs>` for that one package,
# not to bring this back unconditionally for everything.
#
# `--mount=type=cache,target=/root/.cache/pip` (needs the `# syntax=`
# line at the top of this file) persists pip's download cache in a
# BuildKit cache volume OUTSIDE this layer -- so it survives even when
# Docker's own layer cache doesn't (e.g. requirements.txt actually
# changed). Editing one line in requirements.txt no longer means
# re-downloading the other ~90 packages from PyPI; only whatever
# actually changed gets fetched. (The final image is unaffected either
# way -- this cache lives in the builder stage, which is discarded, and
# PIP_NO_CACHE_DIR=1 in the runtime stage below still applies to
# anything installed there.)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install --default-timeout=100 -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2/2: runtime -- this is the image that actually ships.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Prevent Python from writing .pyc files to disk on every future import,
# and don't buffer stdout/stderr (so logs show up immediately, not only
# once a buffer fills or the process exits).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# postgresql-client provides pg_dump/psql -- required by
# services/backup_service.py (database backup/restore). psycopg2-binary
# (in requirements.txt) is a Python DB driver and does NOT include these
# command-line binaries.
#
# BUG FIX -- restores failing with "unrecognized configuration parameter
# \"transaction_timeout\"" (see backup_service.py's restore_backup() for
# the full write-up). Root cause: this used to install the generic,
# unversioned `postgresql-client` package, which just pulls in whatever
# major version is newest in the base image's own apt repos at build
# time -- NOT necessarily the version docker-compose.yml's `db` service
# (postgres:16-alpine) actually runs. When that resolved to a 17.x
# client, its `pg_dump` started writing `SET transaction_timeout = 0;`
# into every dump's preamble (that GUC is PG17+-only), which a 16.x
# server then rejects outright on restore.
#
# Fix: add the official PostgreSQL (PGDG) apt repo and install the
# EXACT major version `postgresql-client-16`, so the client always
# matches the server regardless of what the base image's own repos
# happen to carry on any given build day. If docker-compose.yml's `db`
# image tag above the `db:` service is ever bumped to a different major
# version, this "16" must be bumped to match -- a mismatch here is
# exactly the bug this fix exists to prevent.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates wget gnupg lsb-release && \
    install -d /usr/share/postgresql-common/pgdg && \
    wget -qO /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client-16 && \
    apt-get purge -y --auto-remove wget gnupg lsb-release && \
    rm -rf /var/lib/apt/lists/*

# The installed dependencies -- and ONLY the installed dependencies, none
# of pip's own cache/build metadata -- copied over from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# The application code.
COPY backend/ /app/
COPY shared/ /app/shared/

# Precompile every .py file to .pyc now, at build time, instead of paying
# that cost on the container's first request/import at startup. This is
# what actually makes cold starts faster -- PYTHONDONTWRITEBYTECODE above
# only stops the interpreter from writing NEW .pyc files at runtime, it
# doesn't stop it reading ones that already exist, so these still get used.
# Covers both the app code (/app) and every installed dependency
# (/opt/venv) -- fastapi/sqlalchemy/pydantic etc. are the bulk of what's
# actually imported at startup.
RUN python -m compileall -q /app /opt/venv

# SECURITY: run the app as a dedicated, unprivileged user instead of root.
# By default Docker containers run as root, which means anyone who manages
# to achieve code execution inside this container (e.g. via a future
# dependency vulnerability) would have full root privileges within it.
# Creating this user drastically reduces the blast radius of that scenario,
# at essentially zero cost for this app (it doesn't need to bind to a
# privileged port or write outside /app).
#
# NOTE: we deliberately do NOT `USER appuser` here anymore. The container
# now starts as root so docker-entrypoint.sh can fix ownership of the
# `backup_data` volume (mounted at /app/backups by docker-compose.yml,
# which always arrives owned by root -- see that script's own docstring for
# the full "Errno 13 Permission denied" explanation) before dropping down
# to appuser for the actual app process.
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
COPY backend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh /app/start.sh
ENTRYPOINT ["/docker-entrypoint.sh"]

# ZERO-DOWNTIME: without this, nothing running the container (Docker
# Compose, Container Apps, Kubernetes, Render, ...) has any way to detect
# "the process is up but hung/deadlocked" -- e.g. stuck inside a database
# call that never times out, a poisoned event loop, a livelocked thread
# pool -- distinct from "the process crashed", which restart policies
# already handle fine on their own. A hung-but-still-running container
# keeps its port open and its PID alive, so naive process-liveness checks
# see nothing wrong, while every real request against it just hangs.
#
# Hits GET /healthz (see main.py's health_check() docstring) -- the pure
# LIVENESS probe with zero database dependency, deliberately NOT /readyz
# (see that same docstring for why: a slow/unreachable database should
# stop traffic being routed here, not get this container killed and
# restarted, which wouldn't fix a database problem anyway). Azure
# Container Apps' own liveness probe (infra/main.bicep) already points at
# /healthz for the SAME reason; this HEALTHCHECK is what gives that same
# signal to `docker compose ps`/`docker ps` and any platform (like plain
# Docker Compose) that relies on the image's own HEALTHCHECK instead of a
# separately-configured platform-level probe.
#
# No curl/wget in this slim image (see the postgresql-client apt-get step
# above -- deliberately minimal), so this uses Python's own stdlib
# (already installed, zero extra image size) instead of adding a new
# package just for this. Exits non-zero (crashes the one-liner) on any
# connection error, timeout, or non-2xx status -- all of which Docker
# treats as "unhealthy".
#   --interval=30s   check every 30s once healthy (frequent enough to
#                     catch a hang quickly, not so frequent it adds
#                     meaningful load)
#   --timeout=5s      /healthz has zero I/O of its own (see its docstring)
#                     -- a real response taking anywhere near this long
#                     already indicates the event loop is unhealthy
#   --start-period=15s gives the app time to actually finish importing/
#                     starting (init_db()/seed_db() when enabled, backup
#                     scheduler thread, etc. -- see main.py's on_startup())
#                     before a slow-but-legitimate boot counts as a
#                     failure
#   --retries=3       three consecutive failures (~90s of genuinely bad
#                     responses at the interval above) before Docker
#                     marks the container unhealthy -- tolerates one-off
#                     blips without flapping
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)" || exit 1

# start.sh picks the right uvicorn invocation for ENVIRONMENT at container
# start (no --reload + multiple workers for production, --reload for
# development/local) -- see that script's own header comment for the full
# reasoning, including why the OLD unconditional --reload here was pure
# overhead in production.
CMD ["/app/start.sh"]
