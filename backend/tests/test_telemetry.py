"""
tests/test_telemetry.py
------------------------
Covers telemetry.py (OpenTelemetry distributed tracing -- Operations &
Observability requirement #4; see that module's docstring for the full
picture of what it instruments and why).

WHY THIS DOESN'T USE THE `client`/`db_engine` FIXTURES THE REST OF THIS
SUITE RELIES ON
------------------------------------------------------------------
conftest.py imports `main` (and therefore runs telemetry.setup_tracing())
at MODULE level, before any individual test runs -- see that file's own
docstring for why. The test environment's OTEL_ENABLED is never set (see
conftest.py's os.environ.setdefault() block), so it resolves to
config.py's default (False), and `main.app`/`database.engine` were
imported/instrumented exactly once, that one time, with tracing disabled.
There's no way for a later test to retroactively make that already-
completed import behave as if OTEL_ENABLED had been true.

So instead of trying to flip the real global `settings.OTEL_ENABLED` and
re-trigger main.py's already-finished module-level code, every test below
calls telemetry.py's functions directly with a small hand-built
stand-in object carrying just the OTEL_* attributes they read -- exactly
the same functions main.py/celery_app.py call, just invoked directly
rather than through an app/module import. This tests the actual unit of
new code far more precisely than an end-to-end HTTP test could anyway
(an HTTP test could only ever prove "a request didn't crash", not "a span
with the right attributes was created").

CLEANUP, AND WHY ONLY ONE TEST BELOW ACTUALLY ENABLES TRACING END-TO-END
------------------------------------------------------------------
telemetry.py's `setup_tracing()` sets a module-level
`_tracing_configured = True` guard the first time it successfully
configures a TracerProvider, and the `_reset_tracing` fixture below
resets that guard before/after any test using it. But OpenTelemetry's
OWN `trace.set_tracer_provider()` enforces a separate, stricter rule
that no amount of resetting telemetry.py's own guard can work around:
the FIRST call to ever reach it in a real process wins, permanently --
every later call, from anywhere, is a silent no-op (see
setup_tracing()'s own docstring for the full explanation). Practically,
that means only ONE test in this file
(`test_setup_tracing_enabled_configures_provider_and_is_idempotent`) can
meaningfully observe a freshly-installed provider's actual
configuration; every other "enabled" test below is written to only
assert things that stay true regardless of whether its own call actually
won that race (return values, log output, "did not raise").
"""

from types import SimpleNamespace

import pytest

import telemetry


def _fake_settings(**overrides):
    """
    A minimal stand-in for config.py's real `settings` object, carrying
    only the attributes telemetry.py's functions actually read. Defaults
    to fully disabled/empty -- exactly config.py's own OTEL_* defaults --
    so `_fake_settings()` alone is the "tracing is off" case, and
    `_fake_settings(OTEL_ENABLED=True, ...)` layers on just what a given
    test needs.
    """
    defaults = dict(
        OTEL_ENABLED=False,
        OTEL_SERVICE_NAME="snipeit-lite-backend",
        OTEL_SERVICE_VERSION="0.1.0",
        ENVIRONMENT="test",
        OTEL_EXPORTER_OTLP_ENDPOINT="",
        OTEL_EXPORTER_OTLP_HEADERS="",
        OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
        OTEL_TRACES_SAMPLE_RATIO=1.0,
        OTEL_CONSOLE_EXPORTER=False,
        APPLICATIONINSIGHTS_CONNECTION_STRING="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture()
def _reset_tracing():
    """
    Guarantees telemetry.py's `_tracing_configured` module guard (see
    this file's own docstring) is False both before AND after any test
    using this fixture -- "before" matters just as much as "after" since
    pytest doesn't guarantee this file's own tests run in any particular
    order relative to each other.
    """
    telemetry._tracing_configured = False
    yield
    telemetry._tracing_configured = False


# ---------------------------------------------------------------------------
# _parse_otlp_headers -- pure string-parsing logic, no OpenTelemetry
# machinery involved at all, so no fixture/cleanup needed for these.
# ---------------------------------------------------------------------------

def test_parse_otlp_headers_empty_string_gives_empty_dict():
    assert telemetry._parse_otlp_headers("") == {}


def test_parse_otlp_headers_single_pair():
    assert telemetry._parse_otlp_headers("x-api-key=abc123") == {"x-api-key": "abc123"}


def test_parse_otlp_headers_multiple_pairs_with_whitespace():
    # Matches config.py's OTEL_EXPORTER_OTLP_HEADERS docstring example
    # ("x-honeycomb-team=<api-key>") -- whitespace around commas/equals is
    # a realistic copy-paste artifact from a value pasted out of a
    # tracing backend's own setup docs, not a contrived edge case.
    raw = " x-honeycomb-team = abc123 , Authorization=Bearer xyz "
    assert telemetry._parse_otlp_headers(raw) == {
        "x-honeycomb-team": "abc123",
        "Authorization": "Bearer xyz",
    }


def test_parse_otlp_headers_ignores_malformed_pairs():
    # A bare key with no "=" at all (typo, or a trailing comma) is
    # dropped rather than raising -- this is parsing an operator-supplied
    # config value, not user input inside a request; failing the whole
    # export pipeline over one malformed header would be a worse outcome
    # than just skipping it.
    assert telemetry._parse_otlp_headers("valid=1,no-equals-sign,,another=2") == {
        "valid": "1",
        "another": "2",
    }


# ---------------------------------------------------------------------------
# setup_tracing() -- the OTEL_ENABLED=false no-op path, unconditionally
# safe for every other test in this suite to rely on (see module docstring).
# ---------------------------------------------------------------------------

def test_setup_tracing_returns_false_and_does_nothing_when_disabled(_reset_tracing):
    result = telemetry.setup_tracing(_fake_settings(OTEL_ENABLED=False))
    assert result is False
    assert telemetry._tracing_configured is False


def test_setup_tracing_enabled_configures_provider_and_is_idempotent(_reset_tracing):
    """
    Combines what would otherwise be two separate tests
    (service_name override, and idempotency on a second call) into one,
    for a real reason, not just brevity: opentelemetry-sdk's own
    `trace.set_tracer_provider()` only honors the FIRST call to ever
    reach it in a real process -- every later call, from anywhere, is a
    silent no-op (see setup_tracing()'s own docstring for the full
    explanation). Splitting this into two independent test functions
    would make whichever one pytest happens to run second observe stale
    state from the first and fail for a reason that has nothing to do
    with what it's actually trying to test. Since this file's `_fake_settings`-based
    tests are the only ones in the whole suite that ever call
    `setup_tracing(OTEL_ENABLED=True, ...)` (see this file's own
    docstring on why conftest.py's real `main`/`database` imports never
    do), THIS test is effectively the one and only chance in the entire
    pytest session to observe a real, freshly-installed provider --
    everything meaningful it can prove has to happen in one place.
    """
    from opentelemetry import trace

    enabled = _fake_settings(OTEL_ENABLED=True, OTEL_CONSOLE_EXPORTER=True)

    first = telemetry.setup_tracing(enabled, service_name="snipeit-lite-backend-worker")
    assert first is True
    assert telemetry._tracing_configured is True

    provider = trace.get_tracer_provider()
    resource = provider.resource
    assert resource.attributes["service.name"] == "snipeit-lite-backend-worker"
    assert resource.attributes["service.version"] == "0.1.0"
    assert resource.attributes["deployment.environment"] == "test"

    # Second call: must return True (a working provider genuinely does
    # exist) and must NOT raise attempting to rebuild/reinstall one --
    # see setup_tracing()'s docstring for why this reuses the SAME
    # provider from the first call rather than actually reconfiguring
    # anything.
    second = telemetry.setup_tracing(enabled, service_name="snipeit-lite-backend-worker")
    assert second is True


def test_setup_tracing_enables_trace_context_in_log_records(_reset_tracing, monkeypatch):
    """
    Regression test for the OpenTelemetry logging stack upgrade.

    Newer opentelemetry-instrumentation-logging releases make trace-context
    injection opt-in. The application deliberately keeps its existing logging
    format, so setup_tracing() must request injection explicitly instead of
    enabling set_logging_format=True (which would change the application's
    output format).
    """
    import opentelemetry.instrumentation.logging as otel_logging

    captured = {}

    class _FakeLoggingInstrumentor:
        def instrument(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        otel_logging,
        "LoggingInstrumentor",
        _FakeLoggingInstrumentor,
    )

    enabled = _fake_settings(
        OTEL_ENABLED=True,
        OTEL_CONSOLE_EXPORTER=True,
    )
    assert telemetry.setup_tracing(enabled) is True
    assert captured == {
        "set_logging_format": False,
        "inject_trace_context": True,
    }


def test_setup_tracing_http_otlp_endpoint_gets_v1_traces_path_appended(_reset_tracing, monkeypatch):
    """
    Regression test for a real bug: OTLPSpanExporter (http/protobuf) only
    auto-appends "/v1/traces" itself when it falls back to reading
    OTEL_EXPORTER_OTLP_ENDPOINT from the environment directly -- see that
    exporter's own __init__ (`endpoint or environ.get(...,
    _append_trace_path(environ.get(...)))`). Because setup_tracing()
    always passes `endpoint=` explicitly, that auto-append never ran, so
    the exporter POSTed straight to e.g. "http://jaeger:4318" (no path)
    and got a 404 from Jaeger's OTLP/HTTP receiver on every single batch.
    This asserts the exporter is actually constructed with the full
    "http://jaeger:4318/v1/traces" URL, not the bare base endpoint --
    regardless of trailing slash on the configured setting.
    """
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as http_trace_exporter

    captured = {}

    class _FakeOTLPSpanExporter:
        def __init__(self, endpoint=None, headers=None):
            captured["endpoint"] = endpoint

        def export(self, spans):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setattr(http_trace_exporter, "OTLPSpanExporter", _FakeOTLPSpanExporter)

    enabled = _fake_settings(
        OTEL_ENABLED=True,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://jaeger:4318",
        OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
    )
    assert telemetry.setup_tracing(enabled) is True
    assert captured["endpoint"] == "http://jaeger:4318/v1/traces"


def test_setup_tracing_http_otlp_endpoint_path_append_handles_trailing_slash(_reset_tracing, monkeypatch):
    """Same bug as above, just confirming a trailing slash on the
    configured endpoint doesn't produce a doubled/malformed path like
    "http://jaeger:4318//v1/traces"."""
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as http_trace_exporter

    captured = {}

    class _FakeOTLPSpanExporter:
        def __init__(self, endpoint=None, headers=None):
            captured["endpoint"] = endpoint

        def export(self, spans):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setattr(http_trace_exporter, "OTLPSpanExporter", _FakeOTLPSpanExporter)

    enabled = _fake_settings(
        OTEL_ENABLED=True,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://jaeger:4318/",
        OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
    )
    assert telemetry.setup_tracing(enabled) is True
    assert captured["endpoint"] == "http://jaeger:4318/v1/traces"


def test_setup_tracing_warns_with_no_exporter_configured(_reset_tracing, caplog):
    """
    OTEL_ENABLED=true with every exporter destination left empty (no
    OTLP endpoint, no Application Insights connection string, console
    exporter off) is a real misconfiguration a deployer could make by
    forgetting the last step -- see this scenario's warning in
    setup_tracing() itself. Spans still get created (harmlessly
    discarded) rather than the whole app failing to start, but it should
    be loud in the logs.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="telemetry"):
        result = telemetry.setup_tracing(_fake_settings(OTEL_ENABLED=True))
    assert result is True
    assert any("no exporter is configured" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# instrument_*() -- every one of these must be a true no-op (never even
# touch the object it was handed) when settings.OTEL_ENABLED is false,
# since main.py/celery_app.py call all of them unconditionally rather
# than gating each call site with its own `if settings.OTEL_ENABLED`.
# ---------------------------------------------------------------------------

class _ExplodesIfTouched:
    """Stands in for `app`/`engine` in the disabled-instrumentation tests
    below -- any attribute access at all is a test failure, proving the
    instrument_*() function returned before doing anything with it."""

    def __getattr__(self, name):
        raise AssertionError(
            f"instrument_*() touched .{name} on its target even though "
            f"settings.OTEL_ENABLED was False -- should have returned "
            f"immediately."
        )


def test_instrument_fastapi_app_noop_when_disabled():
    telemetry.instrument_fastapi_app(_ExplodesIfTouched(), _fake_settings(OTEL_ENABLED=False))


def test_instrument_sqlalchemy_engine_noop_when_disabled():
    telemetry.instrument_sqlalchemy_engine(_ExplodesIfTouched(), _fake_settings(OTEL_ENABLED=False))


def test_instrument_redis_noop_when_disabled():
    # Doesn't take a target object at all (patches the redis-py library
    # globally -- see this function's own docstring), so there's nothing
    # to hand an _ExplodesIfTouched() stand-in for; just confirm it
    # returns cleanly with no side effects/exceptions.
    telemetry.instrument_redis(_fake_settings(OTEL_ENABLED=False))


def test_instrument_celery_noop_when_disabled():
    telemetry.instrument_celery(_fake_settings(OTEL_ENABLED=False))


# ---------------------------------------------------------------------------
# shutdown_tracing() -- see that function's own docstring for the exact
# "background export thread races interpreter teardown" bug this fixes.
# ---------------------------------------------------------------------------

def test_shutdown_tracing_safe_when_never_configured(_reset_tracing):
    """Must never raise even if tracing was never turned on in this
    process at all -- main.py's shutdown handler calls this
    unconditionally on every app shutdown, tracing enabled or not."""
    telemetry.shutdown_tracing()


def test_shutdown_tracing_flushes_without_raising(_reset_tracing):
    enabled = _fake_settings(OTEL_ENABLED=True, OTEL_CONSOLE_EXPORTER=True)
    assert telemetry.setup_tracing(enabled) is True
    telemetry.shutdown_tracing()


def test_trace_operation_is_a_noop_when_tracing_is_disabled(_reset_tracing):
    calls = []

    @telemetry.trace_operation("asset.create")
    def operation():
        calls.append("called")
        return {"ok": True}

    assert operation() == {"ok": True}
    assert calls == ["called"]
