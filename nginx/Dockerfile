# =============================================================================
# nginx/Dockerfile
# -----------------------------------------------------------------------------
# Builds ONE image that serves the static frontend AND reverse-proxies /api/*
# to the FastAPI backend -- the same image is used unmodified in local Docker
# Compose, Render, and any cloud environment (see nginx/default.conf.template
# for the full explanation of how per-environment config is injected).
#
# Build context: this Dockerfile expects to be built from the PROJECT ROOT
# (not from inside nginx/), because it needs to COPY the frontend/ folder
# too. docker-compose.yml is already set up this way:
#     frontend:
#       build:
#         context: .
#         dockerfile: nginx/Dockerfile
# If building manually instead of via Compose, run this from the project
# root:
#     docker build -f nginx/Dockerfile -t snipeit-lite-frontend .
# =============================================================================

FROM nginx:1.27-alpine

# Drop nginx's stock default site -- we ship our own (see below).
RUN rm -f /etc/nginx/conf.d/default.conf

# This is a *.template file, not a finished config. nginx's own
# docker-entrypoint.sh automatically envsubst's every ${VARIABLE} in any
# /etc/nginx/templates/*.template file into /etc/nginx/conf.d/<name>.conf
# each time the container starts -- no custom entrypoint script needed.
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template

# Auto-detects RESOLVER_IP from the container's own /etc/resolv.conf at
# boot if it isn't already set -- see the script's own header comment for
# why (short version: not every platform publishes a fixed, documented
# internal-DNS IP the way Docker Compose does, so guessing one into a
# platform's config risks silently going stale). Deliberately left
# NON-executable (do not chmod +x) so nginx's entrypoint sources it into
# the same shell instead of running it as a throwaway subprocess -- see
# that file's own comment for why that distinction matters here.
COPY nginx/docker-entrypoint.d/15-detect-resolver-ip.sh /docker-entrypoint.d/15-detect-resolver-ip.sh

# The plain static site: index.html/admin.html/staff.html/manager.html/
# customer.html and js/*. Nothing here is built/compiled -- it's just
# copied as-is into nginx's web root.
COPY frontend/ /usr/share/nginx/html/

# ---------------------------------------------------------------------------
# Default values baked into the image, so it runs correctly out of the box
# with ZERO environment configuration (e.g. `docker run` with no -e flags).
# Every one of these is meant to be overridden per-environment -- see
# docker-compose.yml for local Docker Compose, and README.md for the
# Render/cloud equivalents.
# ---------------------------------------------------------------------------
# Port nginx listens on inside the container. Local Docker Compose leaves
# this at 80 and maps it to a host port; platforms like Render instead
# inject their OWN $PORT at runtime and expect the app to bind to it --
# because this is a real environment variable (not an nginx-only setting),
# Render's injected value simply overrides this default automatically.
ENV PORT=80

# Where the FastAPI backend lives. "backend" is the Docker Compose service
# name for local dev (Compose's built-in DNS resolves it automatically). In
# Render/cloud, override this to that environment's backend hostname.
ENV BACKEND_HOST=backend
ENV BACKEND_PORT=8000

# Docker Compose's embedded DNS server is 127.0.0.11 -- and docker-compose.yml
# pins RESOLVER_IP to exactly that for local dev, so nothing changes there.
# For every OTHER platform (Render, Kubernetes, ECS, ...), we deliberately do
# NOT bake a guessed default here: not all of them publish a fixed, documented
# internal-DNS address, and a wrong hardcoded guess would fail silently. If
# RESOLVER_IP isn't set at all, nginx/docker-entrypoint.d/15-detect-resolver-ip.sh
# reads the real one straight out of the container's own /etc/resolv.conf at
# boot instead -- see that script for the full reasoning. Only set this ENV
# yourself if you've confirmed your platform needs something different.

# Gates the /docs, /redoc, /openapi.json passthrough block in
# default.conf.template -- see that block's own comment and config.py's
# ENABLE_API_DOCS docstring for the full reasoning. Defaults to "true" here
# purely for local-dev convenience (matches backend/config.py's own default
# so `docker compose up` gives you working docs out of the box with zero
# configuration). SECURITY: override to "false" in Render/cloud -- see
# render.yaml and README.md's "Deploying Across Environments" section,
# which both already do this for you.
ENV ENABLE_API_DOCS=true

EXPOSE 80

# nginx:alpine's base image already defines the correct ENTRYPOINT
# (docker-entrypoint.sh, which runs the envsubst templating step and then
# execs nginx) and CMD (["nginx", "-g", "daemon off;"]) -- nothing to add.
