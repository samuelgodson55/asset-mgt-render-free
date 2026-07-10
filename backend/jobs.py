"""
jobs.py
-------
A tiny in-process replacement for Celery + Redis, used for two things:

  1. Fire-and-forget email sends (services/extension_service.py) -- run in
     the background so an HTTP request never blocks on an SMTP round-trip.
  2. Polled export jobs (api/audit.py's audit-ledger CSV/PDF export) --
     submit a job, return a job_id immediately, let the frontend poll
     .../status and then fetch .../download once it's ready.

WHY THIS EXISTS (instead of Celery + Redis)
--------------------------------------------
This app previously ran a separate Celery `worker` container, using Redis
as both the message broker and the result backend (see git history /
README.md's older "Async Workers" section). That shape doesn't fit
Render's -- or most platforms' -- FREE tier: free instance types only
cover Web Services, Postgres, and Key Value (Redis-compatible) instances.
Background workers and private services always require a paid plan (see
https://render.com/docs/free -- "Other service types don't support Free
instances"). Running the whole thing as a single free Web Service means
there's no separate worker process to enqueue jobs to in the first place.

The trade-off: everything below lives in plain Python memory inside the
SAME process serving HTTP requests. That's the right trade-off for a
"lite", single-instance app (a free instance can't horizontally scale
anyway), but it does mean:
  - Queued/finished jobs are lost if the process restarts, redeploys, or
    (on Render's free tier) spins down after 15 minutes idle.
  - This does NOT scale correctly if you ever run more than one replica
    of this service -- each replica would have its own separate jobs
    dict, so a job submitted on replica A could never be polled/
    downloaded from replica B. Free instances can't scale beyond a single
    instance anyway (see https://render.com/docs/free), so this is a
    non-issue until you upgrade off the Free plan AND turn on scaling --
    if you do both, swap this back out for a real broker (Celery+Redis,
    an RQ+Redis queue, etc.) backed by Render's Key Value or a similar
    shared store.

A single shared `ThreadPoolExecutor` runs submitted work off the request-
handling event loop's thread; a plain dict (guarded by a lock) tracks each
job's state/result, mirroring just enough of Celery's AsyncResult API
(`state` of PENDING/STARTED/SUCCESS/FAILURE) that api/audit.py barely had
to change.
"""

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Small pool: this app's only two background workloads (audit exports,
# transactional emails) are low-volume and short-lived -- a handful of
# threads is plenty and keeps memory usage low on a free instance's
# limited RAM.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="snipeit-job")

_lock = threading.Lock()
# job_id -> {"state": str, "result": Any, "error": str | None, "finished_at": float | None}
_jobs: dict[str, dict[str, Any]] = {}


def submit(fn: Callable, /, *args, ttl_seconds: int = 3600, **kwargs) -> str:
    """
    Runs `fn(*args, **kwargs)` on the background thread pool and returns a
    job_id immediately. Poll `get_status(job_id)` / `get_result(job_id)`
    for progress and the eventual return value -- same two-step flow the
    frontend already used against Celery's AsyncResult.
    """
    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {"state": "PENDING", "result": None, "error": None, "finished_at": None}

    def _run():
        with _lock:
            _jobs[job_id]["state"] = "STARTED"
        try:
            result = fn(*args, **kwargs)
            with _lock:
                _jobs[job_id]["state"] = "SUCCESS"
                _jobs[job_id]["result"] = result
                _jobs[job_id]["finished_at"] = time.monotonic()
        except Exception as exc:  # noqa: BLE001 -- surface ANY failure to the poller
            logger.exception("background_job_failed", extra={"job_id": job_id})
            with _lock:
                _jobs[job_id]["state"] = "FAILURE"
                _jobs[job_id]["error"] = str(exc)
                _jobs[job_id]["finished_at"] = time.monotonic()

    _executor.submit(_run)
    _cleanup_expired(ttl_seconds)
    return job_id


def run_async(fn: Callable, /, *args, **kwargs) -> None:
    """
    Fire-and-forget: same background thread pool as `submit()`, but for
    callers (e.g. transactional emails) that don't need a job_id to poll
    -- they just want `fn` to run without blocking the current request.
    Failures are logged, never raised back to the caller.
    """
    def _run():
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("background_task_failed", extra={"function": getattr(fn, "__name__", str(fn))})

    _executor.submit(_run)


def get_status(job_id: str) -> dict:
    """Mirrors just enough of Celery AsyncResult.state for api/audit.py's polling endpoint."""
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        return {"state": "PENDING", "result": None, "error": None}
    return {"state": job["state"], "result": job["result"], "error": job["error"]}


def get_result(job_id: str) -> Any:
    """Returns a finished job's return value, or None if unknown/not finished/expired."""
    with _lock:
        job = _jobs.get(job_id)
    if job is None or job["state"] != "SUCCESS":
        return None
    return job["result"]


def _cleanup_expired(ttl_seconds: int) -> None:
    """
    Sweeps finished jobs older than `ttl_seconds` out of the in-memory
    dict, so a long-running process doesn't accumulate unbounded memory
    from exports nobody ever downloaded. Called opportunistically on
    every `submit()` rather than on a separate timer thread -- simplest
    thing that works for this app's low submission volume.
    """
    now = time.monotonic()
    with _lock:
        expired = [
            jid for jid, job in _jobs.items()
            if job["finished_at"] is not None and (now - job["finished_at"]) > ttl_seconds
        ]
        for jid in expired:
            del _jobs[jid]
