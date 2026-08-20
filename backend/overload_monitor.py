"""
overload_monitor.py
--------------------
Structured observability for database-overload 503s.

WHY THIS EXISTS
------------------------------------------------------------------
Two independent code paths turn "the database is under more concurrent
load than this process is willing to push at it" into an HTTP 503 for
the caller:

  - middleware/db_concurrency.py SHEDS requests before they ever try to
    check out a DB connection ("shedding weight before it reaches the
    DB" -- the admission-control queue is full).
  - middleware/error_handling.py catches sqlalchemy.exc.TimeoutError
    when a request WAS admitted but the SQLAlchemy pool itself had no
    free/overflow connection within DB_POOL_TIMEOUT_SECONDS.

Both are expected, self-recovering conditions under a burst, not bugs --
so neither should page anyone or spam ErrorBeacon on every single
occurrence (a burst that clears in under a second is exactly what these
two mechanisms are FOR). But an operator still needs two things this
module provides, and both call sites funnel through the single
`record_503()` entry point below so the behavior stays identical
regardless of which layer shed the request:

  1. A per-route, per-reason COUNT of how often each is actually
     happening. Cheap enough to leave on unconditionally: a structured
     WARNING log line every time (queryable as a rate in any log-based
     metrics system, e.g. Azure Log Analytics/KQL, without further
     setup) PLUS a real OpenTelemetry counter -- see telemetry.py's
     `_setup_metrics()` -- exported wherever traces already go
     (console/OTLP/Application Insights) when OTEL_ENABLED=true. When
     OTEL_ENABLED is false, the OTel API's own no-op MeterProvider
     serves the `.add()` call as a harmless no-op; the log line and the
     in-memory `snapshot()` below remain the metrics.

  2. A single ErrorBeacon "database is degraded" signal when rejections
     are SUSTAINED (>= OVERLOAD_ALERT_THRESHOLD_COUNT within the last
     OVERLOAD_ALERT_WINDOW_SECONDS), not merely present -- with a
     cooldown so one sustained episode produces ONE alert, not one per
     rejected request. A rejection rate that stays elevated means the
     database (or PgBouncer in front of it) is the thing that actually
     needs attention, which is a materially different signal from "the
     admission gate correctly did its job for one burst".

RESPONSE SHAPE
------------------------------------------------------------------
The caller-facing message stays generic (never leaks pool sizes, queue
depth, or internal reasoning) -- see user_message() below. The
machine-readable `reason` code travels in the JSON body (useful for a
retrying client/monitoring probe to distinguish failure modes without
parsing prose) and in full in the structured logs and ErrorBeacon
context, where operators actually need it.

Counters here are per-process (module-level state, not shared across
ACA replicas via Redis) -- deliberately so: this module exists to
answer "is THIS process shedding load right now", the same
per-process framing db_concurrency.py's own semaphore already uses.
Each replica evaluating its own sustained-overload threshold and
cooldown independently is fine; under real sustained overload every
replica is likely seeing elevated rejections at once anyway, and the
cooldown keeps any single replica from flooding ErrorBeacon.
"""

import logging
import threading
import time
from collections import defaultdict, deque

from starlette.types import Scope

from config import settings
from integrations.fastapi_errorbeacon import report_exception

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_route_reason_counts: dict[tuple[str, str], int] = defaultdict(int)
_recent_rejections: dict[str, deque] = defaultdict(deque)  # reason -> timestamps
_last_alert_at: dict[str, float] = {}

_rejection_counter = None
_counter_unavailable = False


class OverloadReason:
    """Stable, greppable reason codes -- these strings are a small public
    contract (log queries, dashboards, and any external monitoring that
    keys off the `reason` field in a 503 body should be written against
    them), so treat renaming one as a breaking change."""

    ADMISSION_QUEUE_FULL = "db_admission_queue_full"
    POOL_TIMEOUT = "db_pool_exhausted"


_USER_MESSAGES = {
    OverloadReason.ADMISSION_QUEUE_FULL: (
        "The service is busy handling requests right now. Please retry shortly."
    ),
    OverloadReason.POOL_TIMEOUT: (
        "The server is momentarily busy handling other requests. Please retry shortly."
    ),
}
_DEFAULT_USER_MESSAGE = "The service is temporarily unavailable. Please retry shortly."


def user_message(reason: str) -> str:
    """The safe, generic message shown to the caller. The REAL cause lives
    in `reason` (also in the body, for tooling) and in the structured
    logs/ErrorBeacon event -- never in this string."""
    return _USER_MESSAGES.get(reason, _DEFAULT_USER_MESSAGE)


def _get_counter():
    """Lazily create the OTel counter the first time it's needed. Reuses
    whichever MeterProvider telemetry.py's _setup_metrics() installed (or
    the OTel API's built-in no-op provider if metrics were never
    configured) -- either way this call is safe and cheap. Returns None
    only if the opentelemetry-api package itself isn't importable."""
    global _rejection_counter, _counter_unavailable
    if _rejection_counter is not None or _counter_unavailable:
        return _rejection_counter
    try:
        from opentelemetry import metrics
    except ImportError:
        _counter_unavailable = True
        return None
    meter = metrics.get_meter("asset-mgt-render-free/http")
    _rejection_counter = meter.create_counter(
        name="http.server.rejections_503",
        unit="1",
        description=(
            "Count of HTTP 503 responses returned because a request was "
            "shed by admission control or timed out waiting for a database "
            "connection, broken out by route and reason."
        ),
    )
    return _rejection_counter


def record_503(
    *,
    route: str,
    reason: str,
    scope: Scope | None = None,
    request_id: str | None = None,
) -> None:
    """Record one 503 rejection and evaluate sustained-overload alerting.

    Called from exactly two places (see module docstring): the admission
    gate in middleware/db_concurrency.py and the pool-timeout handler in
    middleware/error_handling.py. Never raises -- a failure recording
    observability data must not turn an already-degraded request into a
    worse one.
    """
    now = time.monotonic()
    window = max(float(settings.OVERLOAD_ALERT_WINDOW_SECONDS), 1.0)
    threshold = max(int(settings.OVERLOAD_ALERT_THRESHOLD_COUNT), 1)
    cooldown = max(float(settings.OVERLOAD_ALERT_COOLDOWN_SECONDS), 0.0)

    with _lock:
        _route_reason_counts[(route, reason)] += 1
        bucket = _recent_rejections[reason]
        bucket.append(now)
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        recent_count = len(bucket)

        should_alert = False
        if recent_count >= threshold:
            # BUG FIX: this used to default a reason's never-alerted-before
            # state to 0.0 and compare `now - last_alert >= cooldown`.
            # `now` is time.monotonic(), which counts up from an arbitrary
            # reference point (commonly process/system start), NOT from the
            # Unix epoch -- so 0.0 doesn't mean "infinitely long ago", it
            # means "at that arbitrary reference point". The practical
            # effect: for the first `cooldown` seconds of a process's own
            # monotonic-clock lifetime (300s by default), `now - 0.0` is
            # smaller than `cooldown`, so the very first sustained-overload
            # episode after a fresh boot silently failed to alert -- exactly
            # the scenario a Render free-tier cold start (spin-down/spin-up,
            # see render.yaml) or any container restart produces on a
            # regular basis. Using -inf as the "never alerted" sentinel
            # makes `now - last_alert` unconditionally exceed any cooldown
            # the first time a reason crosses the threshold, regardless of
            # how recently this process itself started.
            last_alert = _last_alert_at.get(reason, float("-inf"))
            if now - last_alert >= cooldown:
                _last_alert_at[reason] = now
                should_alert = True

    logger.warning(
        "503 rejection: reason=%s route=%s recent_count_in_window=%d window_seconds=%.0f",
        reason,
        route,
        recent_count,
        window,
        extra={
            "overload_reason": reason,
            "http_path": route,
            "recent_rejections": recent_count,
        },
    )

    try:
        counter = _get_counter()
        if counter is not None:
            counter.add(1, {"route": route, "reason": reason})
    except Exception:
        logger.debug("Failed to record OTel 503 rejection counter", exc_info=True)

    if not should_alert:
        return

    logger.error(
        "Sustained database overload detected: reason=%s recent_count=%d "
        "over %.0fs window (threshold=%d) -- reporting to ErrorBeacon as a "
        "degraded-dependency signal.",
        reason,
        recent_count,
        window,
        threshold,
        extra={"overload_reason": reason, "recent_rejections": recent_count},
    )
    try:
        report_exception(
            RuntimeError(f"Sustained database overload: {reason}"),
            scope,
            status_code=503,
            component="database",
            operation=(
                "admission_control"
                if reason == OverloadReason.ADMISSION_QUEUE_FULL
                else "pool_checkout"
            ),
            severity="warning",
            category="dependency_degraded",
            context={
                "request_id": request_id,
                "failure_mode": reason,
                "recent_rejections": recent_count,
                "window_seconds": window,
                "threshold": threshold,
            },
        )
    except Exception:
        logger.debug("Failed to report sustained overload to ErrorBeacon", exc_info=True)


def snapshot() -> dict[str, int]:
    """Plain-dict snapshot of per-route/per-reason 503 counts accumulated
    by THIS process since it started. Cheap, in-memory, no external
    dependency -- a fallback view of the same data the OTel counter and
    log lines above carry, useful when OTEL_ENABLED is off or as a quick
    `/api/health`-adjacent sanity check without a metrics backend."""
    with _lock:
        return {
            f"{route} [{reason}]": count
            for (route, reason), count in sorted(_route_reason_counts.items())
        }


def reset_for_tests() -> None:
    """Test-only: clear all accumulated state between test cases so one
    test's rejections can't push another test over the alert threshold."""
    with _lock:
        _route_reason_counts.clear()
        _recent_rejections.clear()
        _last_alert_at.clear()
