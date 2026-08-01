#!/bin/sh
# =============================================================================
# nginx/docker-entrypoint.d/25-fetch-deploy-status-htpasswd.sh
# -----------------------------------------------------------------------------
# nginx's `auth_basic_user_file` directive (used by the /_deploy/ dashboard's
# Basic Auth gate -- see default.conf.template's own /_deploy/ comment)
# requires a real LOCAL file; it cannot read over HTTP. status.json/
# checks.log get proxied live to Blob Storage per-request instead (no local
# copy needed), but .htpasswd genuinely has to land on disk somewhere first.
#
# This script downloads it ONCE, here, at container boot -- from the same
# `deploy-status` Blob container that .github/scripts/aca-deploy-status.sh's
# `init` subcommand uploads it to (over the Blob REST API, via `az storage
# blob upload`) -- into /tmp/deploy-status/.htpasswd, a container-scoped
# ephemeral path (see infra/main.bicep's frontend `resources` -- ephemeral
# storage is always available, no volume/mount required). nginx re-reads
# this file on every Basic Auth attempt (no nginx restart needed to pick up
# a change to its CONTENTS), so a credentials rotation only actually takes
# effect on this CONTAINER's next restart/revision -- same latency as when
# this lived on the old Azure Files share.
#
# BEST-EFFORT, FAIL CLOSED: if DEPLOY_STATUS_ACCOUNT/DEPLOY_STATUS_SAS
# aren't set at all (e.g. local Docker Compose, which has no Blob Storage
# and doesn't wire up /_deploy/ in the first place -- see docker-compose.yml)
# or the download fails (e.g. no .htpasswd has ever been uploaded yet --
# see aca-deploy-status.sh's own `init` comment for what happens then, and
# deploy-azure-aca.yml's own comment on DEPLOY_STATUS_USER/
# DEPLOY_STATUS_PASSWORD_APR1_HASH), this writes an EMPTY file rather than
# leaving none at all. An empty .htpasswd has zero valid entries for
# auth_basic_user_file to check against, so EVERY Basic Auth attempt fails
# (401) -- fail closed, rather than nginx refusing to boot entirely over a
# missing auth_basic_user_file target (a hard config-time error: "no such
# file or directory").
#
# Plain, non-".envsh" script -- no env var needs to survive into a LATER
# hook script (contrast nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh's
# own comment, which explains why THAT one has to be ".envsh"). nginx's
# entrypoint execs this as an ordinary child process, same as any other
# executable *.sh hook -- executable bit set via frontend/Dockerfile's COPY
# --chmod, same as that other script.
#
# nginx:alpine ships BusyBox wget (no curl) -- see frontend/Dockerfile's own
# HEALTHCHECK comment for the same observation.
# =============================================================================
set -u

mkdir -p /tmp/deploy-status
: > /tmp/deploy-status/.htpasswd  # fail-closed default -- see header comment

if [ -z "${DEPLOY_STATUS_ACCOUNT:-}" ] || [ -z "${DEPLOY_STATUS_SAS:-}" ]; then
    echo "25-fetch-deploy-status-htpasswd.sh: DEPLOY_STATUS_ACCOUNT/DEPLOY_STATUS_SAS not set -- /_deploy/ will 401 on every request until they're configured (see infra/main.bicep's frontendApp)."
else
    # DEPLOY_STATUS_SAS already includes its own leading '?' -- see
    # infra/main.bicep's deployStatusSas comment.
    url="https://${DEPLOY_STATUS_ACCOUNT}.blob.core.windows.net/deploy-status/.htpasswd${DEPLOY_STATUS_SAS}"
    if wget -q -O /tmp/deploy-status/.htpasswd.tmp "$url"; then
        mv /tmp/deploy-status/.htpasswd.tmp /tmp/deploy-status/.htpasswd
        echo "25-fetch-deploy-status-htpasswd.sh: fetched .htpasswd for the /_deploy/ dashboard."
    else
        rm -f /tmp/deploy-status/.htpasswd.tmp
        echo "25-fetch-deploy-status-htpasswd.sh: no .htpasswd found on Blob Storage yet (expected before the first deploy has ever run aca-deploy-status.sh init) -- /_deploy/ will 401 on every request until one exists AND this container restarts."
    fi
fi

# BUG FIX -- DO NOT add an early `exit` above. Same subshell gotcha
# 15-detect-resolver-ip.envsh's own comment explains in detail: nginx's
# entrypoint runs every /docker-entrypoint.d/ script inside one shared
# `while read` loop, so an `exit` from a SOURCED (".envsh") script would
# silently abort every script after it, including nginx's own
# 20-envsubst-on-templates.sh. This script is plain ".sh" (exec'd as a
# child process, not sourced), so that specific failure mode doesn't apply
# here -- but avoiding `exit` anyway costs nothing and keeps this script
# consistent with the one lesson-learned rule every hook script here
# follows.
