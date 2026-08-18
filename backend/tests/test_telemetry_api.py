"""
tests/test_telemetry_api.py
-----------------------------
Covers POST /api/telemetry/client-error (api/telemetry_api.py) -- the one
route in this API that accepts unauthenticated input straight from a
browser, and until now had zero dedicated test coverage even though
nearly every other surface in this codebase does.

WHY THIS DOESN'T HIT A REAL REDIS
------------------------------------
Like middleware/rate_limit.py's own tests (see test_resilience.py),
`telemetry_api._limiter` is a `RedisFixedWindowLimiter` that connects lazily
-- it only actually talks to Redis the first time `.check()` runs.
conftest.py points REDIS_URL at a deliberately unreachable host, so a real
`.check()` call here would fail open only after paying the
`socket_connect_timeout=1` delay on every single test. `monkeypatch.setattr`
swaps `_limiter.check` for a hand-written fake instead, exactly the way
test_resilience.py swaps `middleware._limiter._redis` -- these tests are
about THIS endpoint's request handling (rate-limit response shape, context
validation, request-ID fallback, which IP gets checked), not about
proving the shared limiter's own Redis behavior again.
"""

import api.telemetry_api as telemetry_api


class _FakeLimiter:
    """Stands in for RedisFixedWindowLimiter -- records every identity it
    was asked to check, and returns a scripted (blocked, retry_after)."""

    def __init__(self, blocked=False, retry_after=0):
        self.blocked = blocked
        self.retry_after = retry_after
        self.checked = []

    def check(self, identity):
        self.checked.append(identity)
        return self.blocked, self.retry_after


def _install_fake_limiter(monkeypatch, **kwargs):
    fake = _FakeLimiter(**kwargs)
    monkeypatch.setattr(telemetry_api, "_limiter", fake)
    return fake


def _capture_report_client_event(monkeypatch):
    captured = {}

    def _fake(message, *, stack=None, path=None, request_id=None, context=None):
        captured.update(message=message, stack=stack, path=path, request_id=request_id, context=context)

    monkeypatch.setattr(telemetry_api, "report_client_event", _fake)
    return captured


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_client_error_accepted_when_not_rate_limited(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    captured = _capture_report_client_event(monkeypatch)

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "TypeError: x is not a function"},
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert captured["message"] == "TypeError: x is not a function"


def test_client_error_rejected_when_rate_limited(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=True, retry_after=17)
    captured = _capture_report_client_event(monkeypatch)

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "rate limited please"},
    )

    # Rate-limited requests are still acknowledged (202), just marked
    # unaccepted -- the browser fire-and-forget beacon has nothing useful
    # to do with a 429, and this isn't a security boundary worth a
    # different status code for.
    assert response.status_code == 202
    assert response.json() == {"accepted": False, "reason": "rate_limited"}
    # A rate-limited event must never reach ErrorBeacon.
    assert captured == {}


def test_client_error_rate_limiter_is_checked_per_client_ip(client, monkeypatch):
    fake = _install_fake_limiter(monkeypatch, blocked=False)
    _capture_report_client_event(monkeypatch)

    client.post(
        "/api/telemetry/client-error",
        json={"message": "boom"},
        headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"},
    )

    # BUG FIX regression guard: the leftmost X-Forwarded-For entry (the
    # real external client) must be what gets checked/limited -- not an
    # internal hop, and not X-Real-IP when X-Forwarded-For is present.
    assert fake.checked == ["203.0.113.5"]


def test_client_error_prefers_x_forwarded_for_over_x_real_ip(client, monkeypatch):
    # Regression guard for the original bug: this endpoint used to trust
    # X-Real-IP alone, which utils/client_ip.py's docstring documents as
    # unstable in production. Both headers present -> X-Forwarded-For wins.
    fake = _install_fake_limiter(monkeypatch, blocked=False)
    _capture_report_client_event(monkeypatch)

    client.post(
        "/api/telemetry/client-error",
        json={"message": "boom"},
        headers={"X-Forwarded-For": "198.51.100.9", "X-Real-IP": "192.0.2.99"},
    )

    assert fake.checked == ["198.51.100.9"]


# ---------------------------------------------------------------------------
# Context validation: size, depth, item-count limits
# ---------------------------------------------------------------------------

def test_client_error_rejects_context_nesting_too_deep(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    _capture_report_client_event(monkeypatch)

    nested = {}
    node = nested
    for _ in range(telemetry_api.MAX_CONTEXT_DEPTH + 5):
        node["child"] = {}
        node = node["child"]

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "deep context", "context": nested},
    )

    assert response.status_code == 422
    assert "nesting exceeds" in response.text


def test_client_error_rejects_context_too_many_items(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    _capture_report_client_event(monkeypatch)

    huge = {f"key{i}": i for i in range(telemetry_api.MAX_CONTEXT_ITEMS + 5)}

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "wide context", "context": huge},
    )

    assert response.status_code == 422
    assert "exceeds" in response.text


def test_client_error_rejects_context_over_byte_limit(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    _capture_report_client_event(monkeypatch)

    # Each value is truncated to 5000 chars but there's no cap on how many
    # *keys* can each carry ~5000 chars until MAX_CONTEXT_ITEMS -- enough of
    # those blows the overall MAX_CONTEXT_BYTES budget.
    big_context = {f"k{i}": "x" * 5000 for i in range(10)}

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "oversized context", "context": big_context},
    )

    assert response.status_code == 422
    assert "bytes" in response.text


def test_client_error_accepts_context_within_limits(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    captured = _capture_report_client_event(monkeypatch)

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "ok", "context": {"userAgent": "pytest", "nested": {"a": 1}}},
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert captured["context"] == {"userAgent": "pytest", "nested": {"a": 1}}


# ---------------------------------------------------------------------------
# request_id correlation
# ---------------------------------------------------------------------------

def test_client_error_uses_body_request_id_over_header(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    captured = _capture_report_client_event(monkeypatch)

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "boom", "request_id": "body-rid-1"},
        headers={"X-Request-ID": "header-rid-1"},
    )

    assert response.json()["request_id"] == "body-rid-1"
    assert captured["request_id"] == "body-rid-1"


def test_client_error_falls_back_to_header_request_id(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    captured = _capture_report_client_event(monkeypatch)

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "boom"},
        headers={"X-Request-ID": "header-rid-2"},
    )

    assert response.json()["request_id"] == "header-rid-2"
    assert captured["request_id"] == "header-rid-2"


def test_client_error_missing_request_id_is_none(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    captured = _capture_report_client_event(monkeypatch)

    response = client.post(
        "/api/telemetry/client-error",
        json={"message": "boom"},
    )

    assert response.json()["request_id"] is None
    assert captured["request_id"] is None


def test_client_error_requires_message(client, monkeypatch):
    _install_fake_limiter(monkeypatch, blocked=False)
    _capture_report_client_event(monkeypatch)

    response = client.post("/api/telemetry/client-error", json={})

    assert response.status_code == 422


def test_public_config_reports_browser_otel_only_when_http_export_is_configured(monkeypatch):
    """The public flag never exposes exporter secrets and follows OTEL_ENABLED."""
    from config import settings
    from services.quotation_service import get_public_config

    class FakeDB:
        pass

    monkeypatch.setattr(settings, "OTEL_ENABLED", False)
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setattr(settings, "OTEL_TRACES_SAMPLE_RATIO", 0.25)

    # The service's maintenance lookup is isolated here so the assertion
    # covers only the telemetry fields added to the public configuration.
    import services.maintenance_service as maintenance_service
    monkeypatch.setattr(
        maintenance_service,
        "get_status",
        lambda _db: {"enabled": False, "message": ""},
    )

    config = get_public_config(FakeDB())
    assert config["otel_enabled"] is False
    assert config["otel_trace_sample_ratio"] == 0.25

    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    config = get_public_config(FakeDB())
    assert config["otel_enabled"] is True

    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    config = get_public_config(FakeDB())
    assert config["otel_enabled"] is False

