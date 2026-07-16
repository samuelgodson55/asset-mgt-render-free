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

ENV_LOWER=$(echo "${ENVIRONMENT:-development}" | tr '[:upper:]' '[:lower:]')

LEAN_MODE_VALUE="${LEAN_MODE:-}"
IS_LEAN_MODE=false
if [ "$ENV_LOWER" = "production" ] || [ "$ENV_LOWER" = "prod" ]; then
    IS_LEAN_MODE=true
elif [ "$LEAN_MODE_VALUE" = "1" ] || [ "$LEAN_MODE_VALUE" = "true" ] || [ "$LEAN_MODE_VALUE" = "True" ]; then
    IS_LEAN_MODE=true
fi

if [ "$IS_LEAN_MODE" = true ]; then
    WORKERS="${UVICORN_WORKERS:-1}"
    echo "start.sh: lean mode enabled (ENVIRONMENT=${ENVIRONMENT}, LEAN_MODE=${LEAN_MODE_VALUE:-false}) -- starting uvicorn with ${WORKERS} worker(s), no --reload"
    exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS" --log-level warning
else
    echo "start.sh: ENVIRONMENT=${ENVIRONMENT} -- starting uvicorn with --reload (single worker) for local development"
    exec uvicorn main:app --host 0.0.0.0 --port 8000 --reload --log-level info
fi
