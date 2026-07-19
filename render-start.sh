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
#   - If you ever move this app off the Free plan and turn on horizontal
#     scaling, every instance would start its own embedded worker+beat --
#     celery_app.py's RedBeat config (`beat_scheduler`/`redbeat_redis_url`)
#     is what keeps that safe: only one instance is ever the active Beat
#     scheduler at a time (a Redis-backed lock), so scheduled tasks still
#     fire once, not once per instance, with no config change needed here.
#   - set -e is deliberately NOT used here for the `&`-backgrounded celery
#     line: if the worker fails to start (e.g. it can't reach Redis yet),
#     that should show up in the logs, not crash the whole container before
#     uvicorn even gets a chance to start serving requests.
# =============================================================================

if [ "$RUN_EMBEDDED_WORKER" = "true" ]; then
    # BUG FIX (Render free-tier cold start): this worker and uvicorn below
    # share ONE free instance's 0.1 CPU. Previously this ran at normal
    # priority and with Celery's default worker handshake, so on every
    # cold start (Redis, a separate free Key Value service, is often
    # ALSO still asleep -- see render.yaml's top-of-file comment) it
    # competed head-to-head with uvicorn's own boot for that sliver of
    # CPU, turning what used to be Render's normal ~1-minute wake into a
    # much longer one. Two changes:
    #   - `nice -n 19`: lowest scheduling priority, so the kernel favors
    #     uvicorn whenever both processes want the CPU at the same time.
    #   - `--without-gossip --without-mingle --without-heartbeat`: this
    #     is a solo, never-clustered worker (see docker-compose.yml's
    #     `beat` service comment on why clustering isn't done this way
    #     anyway) -- these three flags only exist to coordinate with
    #     OTHER workers and just add startup overhead here.
    # celery_app.py's broker_transport_options/broker_connection_max_retries
    # additionally bound how long/how hard it can retry against a
    # still-sleeping Redis before giving up.
    echo "render-start.sh: RUN_EMBEDDED_WORKER=true -- launching embedded Celery worker+beat in the background (low priority)"
    nice -n 19 celery -A celery_app worker -B --loglevel=info --concurrency=1 \
        --without-gossip --without-mingle --without-heartbeat &
else
    echo "render-start.sh: RUN_EMBEDDED_WORKER is not 'true' -- skipping the embedded Celery worker"
fi

echo "render-start.sh: starting uvicorn on port ${PORT:-8000}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
