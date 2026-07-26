"""
telemetry.py
------------
OpenTelemetry distributed tracing (Operations & Observability requirement).

WHY THIS EXISTS, AND HOW IT'S DIFFERENT FROM logging_config.py
------------------------------------------------------------------
logging_config.py + middleware/request_context.py already give every log
line a `request_id` -- great for "show me every log line from THIS one
request", as long as that request stayed inside a single process. What
that pair *can't* answer on its own is "this request was slow -- was it
this process's own code, the Postgres query, or the SMTP call to send a
notification email?", or "an audit-log CSV export queued via POST
/audit-logs/export took 40 seconds -- was that time spent waiting in
Redis, or actually running inside the Celery worker?". Answering those
requires seeing *inside* a single request/task as a tree of nested
operations with real durations, and following that tree as it crosses
from `backend` into `worker`/`beat` -- exactly what a "trace" (a directed
tree of "spans", each with a start time, an end time, and a parent) is
for, and exactly what request_id/correlation-ID logging does not attempt
to do.

WHAT GETS INSTRUMENTED, AND WHY EACH ONE
------------------------------------------------------------------
  - FastAPI (`instrument_fastapi_app`): one span per incoming HTTP
    request, automatically wrapping every route -- no per-endpoint code
    changes needed anywhere in `api/`.
  - SQLAlchemy (`instrument_sqlalchemy_engine`): one child span per SQL
    statement issued through `database.engine`/`SessionLocal` -- this is
    usually where "why was this request slow" answers actually live (an
    unindexed query, an N+1 query loop, etc.), and it nests automatically
    under whichever HTTP-request or Celery-task span was active when the
    query ran.
  - Celery (`instrument_celery`): one span per task execution
    (`tasks.generate_audit_export`, the overdue/due-soon notification
    digests, the audit-partition maintenance check) -- AND, critically,
    OpenTelemetry's Celery instrumentation propagates the *trace context*
    itself through the message Celery sends over Redis, so a trace that
    starts in an HTTP request (POST /audit-logs/export) and finishes
    inside the `worker` process later shows up as ONE continuous trace,
    not two disconnected ones -- exactly the "which side did the time go"
    question above.
  - Redis (`instrument_redis`): one span per Redis command -- covers both
    Celery's own broker/result-backend traffic and this app's other
    direct Redis uses (the login rate limiter, the scheduled-backup
    leader lock -- see middleware/rate_limit.py and
    services/backup_service.py).
  - Logging (`setup_tracing`'s own `LoggingInstrumentor` call): stamps
    `otelTraceID`/`otelSpanID` onto every `logging.LogRecord` created
    while a span is active. `logging_config.py`'s `JsonFormatter` already
    folds any non-standard LogRecord attribute into its JSON output (see
    that file's docstring), so this ties every structured log line to the
    exact trace/span that produced it with NO changes needed to
    logging_config.py itself.

WHY THIS IS ITS OWN MODULE, NOT INLINED INTO main.py/celery_app.py
------------------------------------------------------------------
Both `main.py` (the FastAPI/uvicorn process) and `celery_app.py` (the
Celery worker/beat process) need to set this up, in slightly different
ways -- see celery_app.py's own comment for why Celery specifically has
to do this from a `worker_process_init`/`beat_init` signal handler
instead of at plain module-import time (the short version: Celery's
prefork worker pool forks child processes AFTER importing celery_app.py
once in the parent, and a network-exporting background thread set up
before that fork does not reliably survive being forked). Centralizing
the actual setup logic here means both entrypoints call the exact same,
single-source-of-truth functions instead of two subtly different
copy-pasted versions.

EVERYTHING IN THIS FILE IS A NO-OP WHEN OTEL_ENABLED IS FALSE
------------------------------------------------------------------
Matching every other opt-in flag in config.py (NOTIFICATIONS_ENABLED,
ENABLE_AUTO_BACKUP, BACKUP_GDRIVE_ENABLED, ...): with settings.OTEL_ENABLED
left at its default (False), every function below returns immediately
without importing a single `opentelemetry.*` module, so there is zero
startup cost, zero memory overhead, and zero behavior change for anyone
who hasn't opted in. The opentelemetry-* packages themselves are still a
normal (if lazily-imported) dependency in requirements.txt -- see that
file's own comment for why they're always installed even though only
imported when actually needed.

USAGE
------------------------------------------------------------------
    from telemetry import setup_tracing, instrument_fastapi_app, instrument_sqlalchemy_engine

    setup_tracing(settings)                       # once, at process start
    instrument_fastapi_app(app, settings)         # FastAPI processes only
    instrument_sqlalchemy_engine(engine, settings)  # any process touching the DB directly
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level guard: `setup_tracing()` builds and installs the ONE global
# TracerProvider a process is allowed to have. Without this, a process that
# calls it more than once (e.g. `backend` importing celery_app.py as a
# `.delay(...)` producer, on top of its own main.py setup) would silently
# clobber whichever TracerProvider was installed first with a second,
# differently-configured one.
_tracing_configured = False


def _parse_otlp_headers(raw: str) -> dict:
    """
    Parses OTEL_EXPORTER_OTLP_HEADERS's "key1=value1,key2=value2" format
    into the dict the OTLP exporter's `headers=` argument expects. Same
    comma-separated-string convention config.py already uses for
    CORS_ORIGINS/ADMIN_NOTIFICATION_EMAILS/BACKUP_HOURS_UTC -- kept
    consistent rather than inventing a new format just for this one
    setting.
    """
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            headers[key] = value
    return headers


def setup_tracing(settings, service_name: Optional[str] = None) -> bool:
    """
    Builds and installs the process-wide OpenTelemetry TracerProvider.
    Call this exactly ONCE per process (see `_tracing_configured` guard
    above for what happens if you don't) -- main.py does this directly at
    import time; celery_app.py does it from `worker_process_init`/
    `beat_init` signal handlers instead (see that file's own comment for
    why Celery specifically can't just do this at plain import time).

    NOTE: this "exactly once" requirement isn't just this function's own
    preference -- opentelemetry-sdk's own `trace.set_tracer_provider()`
    enforces it independently, at a layer `_tracing_configured` above has
    no visibility into. The FIRST call to ever successfully reach
    `trace.set_tracer_provider()` in a real process wins, permanently;
    every later call from ANYWHERE (even a hypothetical caller that
    reset `_tracing_configured` back to False itself) just logs
    "Overriding of current TracerProvider is not allowed" and keeps the
    original provider in place. `_tracing_configured` exists as a cheap
    early-exit so this module's OWN legitimate double-import scenario
    (see the paragraph above) skips rebuilding a whole
    Resource/TracerProvider/exporter stack it's just going to throw away
    -- not as the thing actually preventing the override, which the SDK
    guarantees on its own regardless.

    `service_name` lets a caller override settings.OTEL_SERVICE_NAME --
    celery_app.py uses this to append "-worker"/"-beat" so a trace that
    crosses from an HTTP request into a queued background task is still
    easy to tell apart by service name in a trace waterfall view, without
    needing a second, separate settings field just for that.

    Returns True if a TracerProvider now exists (either because this call
    just configured one, or a previous call already did) -- False if
    tracing is disabled or the opentelemetry packages aren't installed,
    so callers can skip the rest of their own instrumentation setup
    without duplicating the OTEL_ENABLED check everywhere.
    """
    global _tracing_configured

    if not settings.OTEL_ENABLED:
        return False

    if _tracing_configured:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError:
        logger.warning(
            "OTEL_ENABLED is true but the opentelemetry-sdk/-api packages "
            "aren't installed (see backend/requirements.txt) -- tracing "
            "stays disabled. Run `pip install -r requirements.txt` and "
            "restart."
        )
        return False

    resource = Resource.create({
        "service.name": service_name or settings.OTEL_SERVICE_NAME,
        "service.version": settings.OTEL_SERVICE_VERSION,
        "deployment.environment": settings.ENVIRONMENT,
    })

    # ParentBased: a span whose PARENT was already sampled (e.g. this
    # request's trace context was propagated in from an upstream caller
    # that decided to sample it) is always sampled too, regardless of the
    # ratio below -- a trace is never split across sampled/unsampled
    # pieces. TraceIdRatioBased is only consulted for ROOT spans (no
    # parent), which is the normal case for this app (nothing calls it
    # with an incoming traceparent header today).
    sampler = ParentBased(TraceIdRatioBased(settings.OTEL_TRACES_SAMPLE_RATIO))
    provider = TracerProvider(resource=resource, sampler=sampler)

    exporter_configured = False

    if settings.OTEL_CONSOLE_EXPORTER:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        exporter_configured = True
        logger.info("OpenTelemetry: console span exporter enabled (OTEL_CONSOLE_EXPORTER=true).")

    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        headers = _parse_otlp_headers(settings.OTEL_EXPORTER_OTLP_HEADERS) or None
        using_http_exporter = False
        if settings.OTEL_EXPORTER_OTLP_PROTOCOL.lower() == "grpc":
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            except ImportError:
                logger.warning(
                    "OTEL_EXPORTER_OTLP_PROTOCOL is 'grpc' but "
                    "opentelemetry-exporter-otlp-proto-grpc isn't installed "
                    "(it's not in requirements.txt by default -- see that "
                    "file's OTel comment). Falling back to 'http/protobuf'."
                )
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                using_http_exporter = True
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            using_http_exporter = True

        otlp_endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
        if using_http_exporter:
            # IMPORTANT: OTLPSpanExporter (http/protobuf) only auto-appends
            # "/v1/traces" when it falls back to reading
            # OTEL_EXPORTER_OTLP_ENDPOINT from the environment itself (see
            # its __init__: `endpoint or environ.get(...,
            # _append_trace_path(environ.get(...)))`). Because we always
            # pass `endpoint=` explicitly here, that fallback branch -- and
            # the path-append it does -- never runs, so without this the
            # exporter POSTs straight to "http://jaeger:4318" (no path) and
            # Jaeger's OTLP/HTTP receiver 404s on every batch. This applies
            # any time we end up on the http exporter class, including the
            # "grpc requested but package missing" fallback above -- gRPC
            # itself has no such path suffix, so it's skipped when the real
            # gRPC exporter class is in use.
            otlp_endpoint = f"{otlp_endpoint.rstrip('/')}/v1/traces"

        otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, headers=headers)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        exporter_configured = True
        logger.info(
            "OpenTelemetry: OTLP span exporter configured (endpoint=%s, protocol=%s, sample_ratio=%s).",
            settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            settings.OTEL_EXPORTER_OTLP_PROTOCOL,
            settings.OTEL_TRACES_SAMPLE_RATIO,
        )

    if settings.APPLICATIONINSIGHTS_CONNECTION_STRING:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        except ImportError:
            logger.warning(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is set but "
                "azure-monitor-opentelemetry-exporter isn't installed -- "
                "see backend/requirements.txt. Application Insights "
                "export skipped."
            )
        else:
            azure_exporter = AzureMonitorTraceExporter.from_connection_string(
                settings.APPLICATIONINSIGHTS_CONNECTION_STRING
            )
            provider.add_span_processor(BatchSpanProcessor(azure_exporter))
            exporter_configured = True
            logger.info(
                "OpenTelemetry: Azure Monitor (Application Insights) span "
                "exporter configured. See README.md's 'Distributed "
                "Tracing' section for how to find traces in the Azure "
                "Portal."
            )

    if not exporter_configured:
        logger.warning(
            "OTEL_ENABLED is true but no exporter is configured -- set "
            "OTEL_EXPORTER_OTLP_ENDPOINT, APPLICATIONINSIGHTS_CONNECTION_STRING, "
            "or OTEL_CONSOLE_EXPORTER. Spans will be created (and cost a "
            "little CPU/memory) but never actually exported anywhere."
        )

    trace.set_tracer_provider(provider)

    # Correlate every structured log line with the trace/span active when
    # it was logged -- see this module's docstring for exactly what this
    # adds and why logging_config.py needs no changes to pick it up.
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    LoggingInstrumentor().instrument(set_logging_format=False)

    _tracing_configured = True
    return True


def shutdown_tracing() -> None:
    """
    Flushes any spans still buffered in the BatchSpanProcessor(s) set up by
    `setup_tracing()` above and stops their background export thread.

    WHY THIS NEEDS TO EXIST AT ALL: BatchSpanProcessor queues finished
    spans in memory and flushes them on a periodic timer (every 5 seconds
    by default) from a background thread -- NOT synchronously as each
    request finishes. Without calling this at process shutdown, up to
    that last ~5 seconds of spans before the process exits are silently
    lost, AND (worse, and what this was actually written to fix) that
    background thread can lose its race against interpreter teardown --
    e.g. it wakes up to flush right as pytest is closing stdout at the
    end of a test run, and `ConsoleSpanExporter`/`OTLPSpanExporter` raise
    a raw `ValueError: I/O operation on closed file`/connection error
    trying to write to something that's already gone.

    main.py calls this from a FastAPI `shutdown` event handler; there is
    no equivalent hook for celery_app.py's worker/beat processes to call
    this from -- Celery does not run an async event loop with graceful
    shutdown hooks the way FastAPI/uvicorn does, and a worker process is
    far more likely to be killed outright (SIGKILL after a task timeout,
    a Container Apps revision replacement) than to unwind cleanly. Losing
    the last few seconds of a worker's spans on a hard kill is an
    accepted trade here, the same way this app already accepts "state on
    Redis broker with no persistent volume is lost on restart" (see
    docker-compose.yml's own comment on that) -- not worth adding
    complexity for.

    Safe to call even when tracing was never configured (OTEL_ENABLED is
    false, or setup_tracing() was never called in this process) -- it's a
    no-op in that case, so callers don't need to gate this behind their
    own OTEL_ENABLED check.
    """
    if not _tracing_configured:
        return
    try:
        from opentelemetry import trace
    except ImportError:
        return
    provider = trace.get_tracer_provider()
    # `shutdown()` only exists on the real SDK TracerProvider -- guard
    # with getattr rather than an isinstance check so this stays correct
    # even if a caller's test suite swapped in some other
    # trace.get_tracer_provider() implementation (e.g. the SDK's own
    # NoOpTracerProvider, which predates `_tracing_configured` ever being
    # set True and so shouldn't reach here anyway, but cheap insurance).
    shutdown_fn = getattr(provider, "shutdown", None)
    if callable(shutdown_fn):
        shutdown_fn()


def instrument_fastapi_app(app, settings) -> None:
    """
    Wraps every route on `app` in its own span. Safe to call even when
    tracing ends up disabled/misconfigured (returns immediately) -- so
    main.py can call this unconditionally rather than gating it itself.

    `excluded_urls` skips /healthz and /readyz: both probes are polled
    every 10-30 seconds by Container Apps/Docker Compose/Kubernetes for
    the app's entire lifetime (see main.py's health_check()/
    readiness_check() docstrings) -- tracing them would be pure noise
    (hundreds of near-identical zero-information spans a day) crowding
    out the request traces you actually want to look at.
    """
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")


def instrument_sqlalchemy_engine(engine, settings) -> None:
    """Adds one child span per SQL statement issued through `engine`."""
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        return
    SQLAlchemyInstrumentor().instrument(engine=engine, service=f"{settings.OTEL_SERVICE_NAME}-db")


def instrument_redis(settings) -> None:
    """
    Adds one span per Redis command, across every redis-py client this
    process creates -- Celery's own broker/result-backend connections,
    plus middleware/rate_limit.py's and services/backup_service.py's
    direct `redis.Redis.from_url(settings.REDIS_URL)` calls. Patches the
    redis-py library globally (there's no per-instance "instrument just
    this client" option), so this only needs to run once per process,
    same as every other instrumentor here.
    """
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError:
        return
    RedisInstrumentor().instrument()


def instrument_celery(settings) -> None:
    """
    Adds one span per Celery task execution AND propagates trace context
    through the task message itself (see this module's docstring) -- call
    this from the `worker`/`beat` process only (celery_app.py's
    `worker_process_init`/`beat_init` signal handlers), never from a
    process that merely enqueues tasks via `.delay(...)`.
    """
    if not settings.OTEL_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
    except ImportError:
        return
    CeleryInstrumentor().instrument()
