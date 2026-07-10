# =============================================================================
# Dockerfile (repo root)
# -----------------------------------------------------------------------------
# Builds ONE image containing the FastAPI backend AND the static frontend,
# served together by a single uvicorn process (see backend/main.py's module
# docstring for the full "why one process, not three" explanation).
#
# WHY THIS REPLACED backend/Dockerfile + nginx/Dockerfile
# ----------------------------------------------------------------------------
# This app used to build two separate images: a private FastAPI `backend`
# and a public `nginx` image that served the static frontend AND reverse-
# proxied /api/* to that backend over a private network, plus a third
# Celery `worker` image sharing the backend's Dockerfile. That's three
# services (plus Redis) for a platform's Blueprint to provision.
#
# Render's (and most platforms') FREE tier only covers Web Services,
# Postgres, and Key Value/Redis -- private services and background
# workers always require a paid plan (see https://render.com/docs/free).
# Free web services also can't receive private-network traffic from
# other services, so even the nginx-in-front-of-a-private-backend split
# wouldn't work between two free services. The fix: ONE image, ONE free
# Web Service, serving both the API and the frontend directly -- see
# render.yaml and docker-compose.yml, both updated to match.
#
# BUILD CONTEXT: this Dockerfile must be built from the PROJECT ROOT (not
# from inside backend/), because it COPYs both backend/ and frontend/.
#     docker build -t snipeit-lite .
# docker-compose.yml and render.yaml are already configured this way.
# =============================================================================

FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing .pyc files to disk and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy requirements first so Docker can cache this layer and skip
# re-installing packages every time only the app code changes.
COPY backend/requirements.txt /app/backend/requirements.txt

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --default-timeout=100 -r /app/backend/requirements.txt

# Copy the backend (FastAPI app) and the frontend (static site) into the
# image, preserving the same relative layout this repo already uses --
# backend/main.py locates the frontend via
# `os.path.dirname(os.path.dirname(__file__)) / "frontend"`, i.e. one
# level up from backend/, so /app/backend + /app/frontend must both exist.
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# SECURITY: run the app as a dedicated, unprivileged user instead of root.
# By default Docker containers run as root, which means anyone who manages
# to achieve code execution inside this container (e.g. via a future
# dependency vulnerability) would have full root privileges within it.
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/backend

# Render (and most platforms) inject their OWN $PORT at runtime and expect
# the app to bind to it; 8000 is just the local-dev/docker-compose
# default. Using shell form (not exec-array form) for CMD is deliberate
# here so $PORT is expanded by the shell at container start -- exec form
# would pass the literal string "$PORT" to uvicorn instead of its value.
ENV PORT=8000
EXPOSE 8000

# Single worker process, no --reload: this is the production entrypoint
# (docker-compose.yml overrides `command:` for local dev with --reload
# instead -- see that file). A free instance has limited RAM/CPU and no
# horizontal scaling anyway, so one uvicorn worker process is the right
# fit; see README.md's "Running In Production" section if you later move
# to a paid plan with more resources and want multiple workers.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
