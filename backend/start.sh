#!/bin/sh
# backend/start.sh
# -----------------------------------------------------------------------------
# Picks the right uvicorn invocation for the CURRENT environment. This is the
# Dockerfile's CMD, run via docker-entrypoint.sh (which execs this in place
# of itself, as PID 1, after fixing volume ownership and dropping from root
# to appuser -- see that script's own docstring for why).
#
# Previously the Dockerfile's CMD hardcoded `--reload` unconditionally,
# including for ENVIRONMENT=production. That's strictly worse there:
#   - It re-imports the entire app on every detected filesystem change --
#     pure overhead once you're not actively editing code, and on some
#     platforms can even fire from filesystem noise unrelated to a real
#     edit.
#   - uvicorn's reload mode also starts an extra supervisor/reloader
#     process on top of the actual server process, which is slower to boot
#     than just starting the server directly.
#   - docker-compose.yml doesn't bind-mount backend/ into the container at
#     all (the image bakes in a COPY of the code at build time), so
#     --reload was never even doing anything useful to begin with -- just
#     paying its startup cost for zero benefit.
#
# ENVIRONMENT=production (or "prod"):
#   No --reload, and multiple worker processes (UVICORN_WORKERS, default 2)
#   so the app can use more than one CPU core for concurrent requests.
#
# Anything else (development/local):
#   Single worker, --reload enabled, in case you DO wire up a bind mount
#   for live-editing.
# -----------------------------------------------------------------------------
set -e

# -----------------------------------------------------------------------------
# BUG FIX: embedded Celery worker/beat wasn't actually wired up here
# -----------------------------------------------------------------------------
# infra/main.bicep sets RUN_EMBEDDED_WORKER=true on the Azure `backend`
# Container App (there's no separate `worker`/`beat` Container App in this
# cost-optimized layout -- see that file's comments), but until now nothing
# in this script ever read that variable: it only ever started uvicorn.
# The result was silent, not loud -- `celery_app.py`'s `.delay(...)` calls
# (audit export, extension-request emails) queued jobs into Redis with
# NO worker ever consuming them, so exports hung forever and the
# overdue/due-soon notification digest (celery_app.py's `beat_schedule`)
# never fired. This launches that same embedded worker+beat command
# render-start.sh already uses for the Render free-tier image, applying
# the same fixes: bounded Redis connection timeouts (celery_app.py), low
# scheduling priority + no gossip/mingle/heartbeat (this is always a
# solo, never-clustered worker regardless of how many uvicorn
# processes/replicas exist), and RedBeat as the Beat scheduler
# (celery_app.py's `beat_scheduler`/`redbeat_redis_url` config) so `-B`
# is safe to pass unconditionally here even when Azure's `backendApp`
# scales to more than one replica -- RedBeat's Redis-backed lock ensures
# only one replica is ever the active scheduler at a time, automatically
# failing over if that replica dies. No manual per-replica bookkeeping
# needed.
if [ "${RUN_EMBEDDED_WORKER:-false}" = "true" ]; then
    echo "start.sh: RUN_EMBEDDED_WORKER=true -- launching embedded Celery worker+beat in the background (low priority)"
    nice -n 19 celery -A celery_app worker -B --loglevel=info --concurrency=1 \
        --without-gossip --without-mingle --without-heartbeat &
else
    echo "start.sh: RUN_EMBEDDED_WORKER is not 'true' -- skipping the embedded Celery worker"
fi

ENV_LOWER=$(echo "${ENVIRONMENT:-development}" | tr '[:upper:]' '[:lower:]')

IS_PRODUCTION=false
if [ "$ENV_LOWER" = "production" ] || [ "$ENV_LOWER" = "prod" ]; then
    IS_PRODUCTION=true
fi

if [ "$IS_PRODUCTION" = true ]; then
    WORKERS="${UVICORN_WORKERS:-1}"
    echo "start.sh: ENVIRONMENT=${ENVIRONMENT} -- starting uvicorn with ${WORKERS} worker(s), no --reload"
    exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS" --log-level warning
else
    echo "start.sh: ENVIRONMENT=${ENVIRONMENT} -- starting uvicorn with --reload (single worker) for local development"
    exec uvicorn main:app --host 0.0.0.0 --port 8000 --reload --log-level info
fi
