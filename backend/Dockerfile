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
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Copy requirements and install dependencies first so Docker can cache this
# layer and skip re-installing packages every time only the app code changes.
COPY requirements.txt /app/
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libpq-dev && \
    pip install --upgrade pip && \
    pip install --default-timeout=100 -r requirements.txt && \
    apt-get remove -y build-essential libpq-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

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
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# postgresql-client provides pg_dump/psql -- required by
# services/backup_service.py (database backup/restore). psycopg2-binary
# (in requirements.txt) is a Python DB driver and does NOT include these
# command-line binaries.
RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# The installed dependencies -- and ONLY the installed dependencies, none
# of pip's own cache/build metadata -- copied over from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# The application code.
COPY . /app/

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
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh /app/start.sh
ENTRYPOINT ["/docker-entrypoint.sh"]

# start.sh picks the right uvicorn invocation for ENVIRONMENT at container
# start (no --reload + multiple workers for production, --reload for
# development/local) -- see that script's own header comment for the full
# reasoning, including why the OLD unconditional --reload here was pure
# overhead in production.
CMD ["/app/start.sh"]
