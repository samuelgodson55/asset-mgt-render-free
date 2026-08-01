# =============================================================================
# frontend/Dockerfile
# -----------------------------------------------------------------------------
# Builds ONE image that serves the static frontend AND reverse-proxies /api/*
# to the FastAPI backend -- the same image is used unmodified in local Docker
# Compose, Render, and any cloud environment (see nginx/default.conf.template
# for the full explanation of how per-environment config is injected).
#
# This file lives in frontend/ (alongside the static site it builds) even
# though the runtime half of the image is nginx -- that mirrors
# backend/Dockerfile living in backend/: each service owns its own
# Dockerfile, one per directory.
#
# Build context: this Dockerfile still expects to be built from the PROJECT
# ROOT (not from inside frontend/), because it also needs to COPY the
# nginx/ folder (default.conf.template, docker-entrypoint.d) and
# build-frontend/ (the JS/HTML processing step). docker-compose.yml is
# already set up this way:
#     frontend:
#       build:
#         context: .
#         dockerfile: frontend/Dockerfile
# If building manually instead of via Compose, run this from the project
# root:
#     docker build -f frontend/Dockerfile -t snipeit-lite-frontend \
#         --build-arg BUILD_ENV=production .
# =============================================================================

# =============================================================================
# Stage 1/2: frontend-build
# -----------------------------------------------------------------------------
# Runs build-frontend/build.js against frontend/ BEFORE anything reaches the
# actual nginx image below. What it does to the JS and HTML depends entirely
# on BUILD_ENV (passed in as a Docker build ARG -- see docker-compose.yml's
# `frontend.build.args`, which sets it from .env's ENVIRONMENT value, so
# changing ENVIRONMENT in .env and rebuilding is all you need to do):
#
#   BUILD_ENV=local        -- untouched: byte-for-byte copy of frontend/
#                              ("personal PC" -- readable source in DevTools)
#   BUILD_ENV=development  -- JS minified (Terser: whitespace/comments gone,
#                              identifiers shortened, dead code folded); HTML
#                              minified (html-minifier-terser: whitespace
#                              collapsed, comments stripped)
#   BUILD_ENV=production   -- same HTML minification as development, PLUS
#                              the minified JS is run through
#                              javascript-obfuscator (control-flow
#                              flattening, string-array encoding, dead-code
#                              injection, self-defending output). HTML isn't
#                              obfuscated, just minified -- every <script>
#                              tag here loads an external .js file, so
#                              there's no inline JS in the markup to protect.
#
# See build-frontend/build.js's own header comment for the full reasoning.
# This stage prints a per-file size report to stdout, so `docker compose
# build` (or `docker build`) output shows exactly what happened for the
# mode you just built -- that's the "real-time build characteristics" this
# stage exists to surface.
# =============================================================================
# Node 20 reached end-of-life 2026-04-30 -- no more upstream security
# patches for it or its Alpine base. Node 22 is the current LTS. This stage
# is discarded before the image ships (see Stage 2 below), so it never
# itself sets off the release pipeline's Trivy scan on the final image --
# bumped anyway so an EOL toolchain isn't quietly used to build every
# release.
FROM node:22-alpine AS frontend-build

ARG BUILD_ENV=development
ENV BUILD_ENV=${BUILD_ENV}

WORKDIR /build

# Install build-frontend's own deps (terser, javascript-obfuscator,
# html-minifier-terser) first, separately from copying frontend/ itself, so
# this layer stays cached and is only re-run when build-frontend/package.json
# actually changes -- not every time an application JS/HTML file changes.
COPY build-frontend/package.json build-frontend/package-lock.json* /build/build-frontend/
RUN cd build-frontend && npm ci --no-audit --no-fund && npm cache clean --force

COPY build-frontend/build.js /build/build-frontend/build.js

# The whole frontend/ tree (html/css/js/images) goes in as the source dir;
# build.js copies non-.js/non-.html assets through untouched and transforms
# .js and .html files -- see that script's own comment for why it's written
# that way.
COPY frontend/ /build/frontend-src/

RUN node build-frontend/build.js /build/frontend-src /build/frontend-out


# =============================================================================
# Stage 2/2: nginx
# -----------------------------------------------------------------------------
# Unchanged from before this build step was introduced, except the frontend
# COPY now pulls from the frontend-build stage's *output* directory instead
# of the raw frontend/ folder on disk -- everything else about how this
# image serves the site and proxies /api/* is identical.
#
# THIS is the image release.yml's Trivy scan actually checks (backend/
# Dockerfile succeeded fine -- python:3.12-slim is still actively
# maintained). `nginx:1.27-alpine` is a retired line: nginx has moved on to
# 1.30 (stable)/1.31 (mainline), so that tag stopped receiving fresh Alpine
# base rebuilds, and its now-frozen Alpine base accumulated a fixable
# CRITICAL CVE that a currently-maintained line doesn't have. Pinned to the
# current stable line instead -- ci.yml's Trivy step now blocks on CRITICAL
# too (was report-only), so this class of failure surfaces on a normal push
# instead of only at `git tag` time.
# =============================================================================
FROM nginx:1.30-alpine

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
# platform's config risks silently going stale).
#
# BUG FIX: this used to be named "*.sh" and was deliberately left
# non-executable, on the (incorrect) assumption that nginx's entrypoint
# sources non-executable ".sh" files. It doesn't -- it skips them
# entirely, so RESOLVER_IP was never actually being exported, and
# envsubst was leaving the literal "${RESOLVER_IP}" text in the rendered
# config (nginx: "host not found in resolver \"${RESOLVER_IP}\""). Now
# named ".envsh" (nginx's actual convention for "source this into my own
# shell so its exports survive into later hook scripts") AND explicitly
# made executable via --chmod, since nginx's entrypoint requires the
# executable bit for BOTH ".sh" and ".envsh" alike -- see the script's own
# header comment for the full explanation, including the exact upstream
# source line that confirms this.
COPY --chmod=755 nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh /docker-entrypoint.d/15-detect-resolver-ip.envsh

# Fetches the /_deploy/ dashboard's .htpasswd from Blob Storage into a
# local file at boot -- auth_basic_user_file (nginx/default.conf.template's
# own /_deploy/ location) can't read it over HTTP directly. Plain
# executable ".sh" (exec'd as a child process, not sourced -- no env var
# needs to survive into a later hook script here, unlike the ".envsh"
# above) -- see the script's own header comment for the full reasoning.
COPY --chmod=755 nginx/docker-entrypoint.d/25-fetch-deploy-status-htpasswd.sh /docker-entrypoint.d/25-fetch-deploy-status-htpasswd.sh

# The /_deploy/ dashboard's own shell (status.json/checks.log are proxied
# LIVE to Blob Storage at request time instead -- see
# nginx/default.conf.template's own /_deploy/ comment for why only this
# static shell, not the data, is baked into the image). Deliberately
# separate from the `frontend-build` stage's output below -- this file
# isn't part of the app's own frontend/ tree and doesn't go through
# build-frontend/build.js's minification/obfuscation pipeline at all;
# nginx/default.conf.template's `alias` for `location = /_deploy/` points
# straight at this exact path.
COPY scripts/deploy-status-aca/index.html /usr/share/nginx/deploy-status/index.html

# The static site: index.html/admin.html/staff.html/manager.html/
# customer.html and js/*. CSS/images pass through the frontend-build stage
# untouched; HTML has been minified (development/production) and js/* has
# been minified and/or obfuscated depending on BUILD_ENV -- see that
# stage's own comment above.
COPY --from=frontend-build /build/frontend-out/ /usr/share/nginx/html/

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

# DEPLOY_STATUS_ACCOUNT / DEPLOY_STATUS_SAS default to an EMPTY STRING here
# -- NOT "no default" as before. Local Docker Compose, the VM path, and
# Render genuinely have no Blob Storage and never set these; the sole
# purpose of this ENV is to make sure the two names are still PRESENT in
# the container's environment even then.
#
# BUG FIX: leaving them fully unset broke `nginx -t` (and therefore the
# container's boot) on every one of those platforms, not just "degraded
# gracefully" as originally intended. The official nginx image's own
# docker-entrypoint.d/20-envsubst-on-templates.sh restricts envsubst to
# ONLY the variable names actually present in the environment -- so when a
# var is genuinely absent (not just empty), envsubst leaves the literal
# "${DEPLOY_STATUS_ACCOUNT}"/"${DEPLOY_STATUS_SAS}" text sitting in the
# rendered nginx/default.conf.template's `set`/`proxy_pass` lines. nginx's
# OWN config parser then reads that literal "${...}" as a reference to an
# nginx variable that was never `set` anywhere -- "unknown
# \"deploy_status_account\" variable" -- a hard `nginx -t` failure, not a
# runtime 401. Giving both an explicit empty-string default here makes
# them show up in `env`, which is all envsubst's restricted whitelist
# needs to substitute them -- with an empty string, same as before this
# fix wherever they're genuinely unused. nginx/test-config.sh mirrors this
# same default so the CI check that caught this can't regress silently.
# The REST of the original reasoning is unchanged: with an empty account
# name, the /_deploy/ dashboard shell still loads (frontend/Dockerfile's
# own baked-in index.html) and every fetch()/Basic-Auth attempt against it
# still fails closed -- nothing here grants real Blob Storage access.
# infra/main.bicep's `frontendApp` overrides both to their real values in
# Azure -- see that resource's own env comment.
ENV DEPLOY_STATUS_ACCOUNT=""
ENV DEPLOY_STATUS_SAS=""

EXPOSE 80

# ZERO-DOWNTIME: same reasoning as backend/Dockerfile's HEALTHCHECK -- lets
# `docker compose ps`/any orchestrator detect "nginx is running but not
# actually answering" (a bad envsubst render, a config nginx -t would have
# caught but didn't get run, etc.), not just "the process crashed" (which
# `restart: unless-stopped` in docker-compose.yml already covers on its
# own). Hits "/" -- served by the `location /` block in
# nginx/default.conf.template, which resolves to index.html and needs no
# backend connectivity at all, so this stays a pure LIVENESS check for
# THIS container, mirroring the backend's own /healthz-not-/readyz split
# (see main.py's health_check() docstring) -- nginx being unable to reach
# the backend container is a separate, already-handled failure mode (the
# backend's own healthcheck/restart policy), not a reason to also kill and
# restart nginx.
#
# nginx:alpine ships BusyBox wget (no curl) -- shell form (not exec form)
# so `$PORT` expands; defaults to 80 to match this Dockerfile's own
# ENV PORT=80 above if a platform never overrides it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --tries=1 --spider "http://127.0.0.1:${PORT:-80}/" || exit 1

# nginx:alpine's base image already defines the correct ENTRYPOINT
# (docker-entrypoint.sh, which runs the envsubst templating step and then
# execs nginx) and CMD (["nginx", "-g", "daemon off;"]) -- nothing to add.
