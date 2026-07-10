#!/bin/sh
# =============================================================================
# render-start.sh
# -----------------------------------------------------------------------------
# The CMD for Dockerfile.render -- the combined single-service image used by
# the free-tier Render deployment (see render.yaml and README.md's
# "Deploying on Render's Free Plan" section).
#
# WHY THIS FILE EXISTS
# Render's Free instance type has no Background Worker service type at all
# (see render.yaml's top-of-file comment), so there's nowhere to run
# `celery -A celery_app worker -B` as its own service the way
# docker-compose.yml's `worker` container does. This script is the
# workaround: it launches that exact same Celery command as a background
# process INSIDE this one web service's container (only if
# RUN_EMBEDDED_WORKER=true), then hands off to uvicorn as the container's
# main (PID 1) process via `exec`.
#
# CAVEATS (see README.md for the full writeup):
#   - Both processes live and die together. Every free-instance spin-down
#     (15 minutes idle -- see render.yaml's comment) or redeploy kills the
#     embedded worker along with the web server; it comes back on the next
#     request/redeploy, same as everything else on a Free instance.
#   - This does NOT scale past one instance: if you ever move this app off
#     the Free plan and turn on horizontal scaling, EVERY instance would
#     start its own embedded worker/beat, and every scheduled task in
#     celery_app.py's beat_schedule would fire once per instance --
#     duplicate emails. Split the worker back out into its own dedicated
#     `worker` service (see render.yaml's comments for how) before scaling
#     beyond a single instance.
#   - set -e is deliberately NOT used here for the `&`-backgrounded celery
#     line: if the worker fails to start (e.g. it can't reach Redis yet),
#     that should show up in the logs, not crash the whole container before
#     uvicorn even gets a chance to start serving requests.
# =============================================================================

if [ "$RUN_EMBEDDED_WORKER" = "true" ]; then
    echo "render-start.sh: RUN_EMBEDDED_WORKER=true -- launching embedded Celery worker+beat in the background"
    celery -A celery_app worker -B --loglevel=info --concurrency=1 &
else
    echo "render-start.sh: RUN_EMBEDDED_WORKER is not 'true' -- skipping the embedded Celery worker"
fi

echo "render-start.sh: starting uvicorn on port ${PORT:-8000}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
