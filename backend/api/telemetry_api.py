"""HTTP endpoint for receiving safe browser-side telemetry events.

This module keeps the browser error-reporting path small and isolated. It rate
limits by client IP, extracts the request ID for correlation, and forwards the
sanitized event to the backend ErrorBeacon integration.

BUG FIX -- rate limiting used to be two separate bugs stacked on top of each
other:

  1. Client IP was resolved with `request.headers.get('x-real-ip') or
     request.client.host` -- trusting X-Real-IP alone. That's exactly the
     header middleware/rate_limit.py's own history documents as UNSTABLE
     behind Azure Container Apps' ingress (see utils/client_ip.py's module
     docstring for the full production incident writeup): the value can
     vary request-to-request depending on which ACA ingress node handled a
     given hop, which splits one real caller across many different
     "client IPs" and defeats a per-IP limiter without ever raising an
     error. That fix was applied to the login limiter but never ported
     here.
  2. The limiter itself was a plain in-memory `deque` per IP, local to one
     process. With more than one backend replica, each replica keeps its
     own independent 20-per-minute counter for the same IP, so the
     effective limit silently multiplies by the replica count -- and it
     resets completely on every deploy/restart.

Both are now the same shared implementation middleware/rate_limit.py uses
for POST /auth/login (see utils/client_ip.py and utils/rate_limiter.py),
so this endpoint gets the same cross-replica, spoof-resistant throttle
instead of a second, weaker one.
"""

import json
import logging
import re
from urllib.error import URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from config import settings
from integrations.fastapi_errorbeacon import report_client_event
from utils.client_ip import resolve_client_ip
from utils.rate_limiter import RedisFixedWindowLimiter

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/telemetry", tags=["telemetry"])

# Same shape as middleware/rate_limit.py's login limiter (Redis-backed,
# fixed-window, fails open on Redis errors) -- see that module and
# utils/rate_limiter.py's docstrings. Deliberately more permissive than the
# login limiter (this endpoint accepts routine, unauthenticated browser
# telemetry, not credential guesses) but still bounded per-IP so a
# misbehaving/malicious client can't flood ErrorBeacon or this process's
# own request-handling capacity.
_limiter = RedisFixedWindowLimiter(
    settings.REDIS_URL,
    key_prefix="rl:client-error",
    max_requests=20,
    window_seconds=60,
)

# Browser OTLP is intentionally a server-side proxy. The browser never sees
# OTEL_EXPORTER_OTLP_HEADERS (which may contain an API key) and never talks
# directly to a collector that requires those credentials. The proxy is
# enabled only when the same OTEL_ENABLED master switch is on and the backend
# has an OTLP/HTTP endpoint configured.
_trace_limiter = RedisFixedWindowLimiter(
    settings.REDIS_URL,
    key_prefix="rl:otel-browser-traces",
    max_requests=120,
    window_seconds=60,
)
MAX_TRACE_BODY_BYTES = 256 * 1024
MAX_BROWSER_SPANS = 100
BROWSER_SERVICE_NAME = "snipeit-lite-frontend"
SAFE_BROWSER_ATTRIBUTE_KEYS = {
    "http.request.method",
    "http.response.status_code",
    "url.path",
    "browser.url.path",
    "ui.action",
    "app.operation",
    "error",
    "error.type",
}
SAFE_BROWSER_ACTION_RE = re.compile(r"^(?:ui\.click\.[a-z0-9][a-z0-9._-]{0,78}|[a-z][a-z0-9._-]{0,78})$")
SAFE_BROWSER_OPERATION_NAMES = {
    "asset.category.update",
    "asset.create",
    "asset.delete",
    "asset.department.update",
    "asset.exception.flag",
    "asset.exception.recall",
    "asset.import",
    "asset.name.update",
    "asset.price.update",
    "asset.purge",
    "asset.quantity.update",
    "asset.restore",
    "audit.export.start",
    "auth.login",
    "auth.logout",
    "auth.mfa.recovery_codes.regenerate",
    "auth.mfa.setup",
    "auth.mfa.verify",
    "auth.password.forgot",
    "auth.password.reset",
    "auth.password.update",
    "backup.create",
    "backup.delete",
    "backup.restore",
    "backup.restore.upload",
    "checkin.complete",
    "checkout.complete",
    "checkout.extend",
    "checkout.extend.bulk",
    "checkout.extension.decide",
    "checkout.extension.request",
    "maintenance.update",
    "outsider.convert_to_user",
    "outsider.delete",
    "outsider.update",
    "profile.update",
    "quote.approve",
    "quote.assign",
    "quote.checkout",
    "quote.create",
    "quote.delete",
    "quote.discount.update",
    "quote.item.add",
    "quote.item.remove",
    "quote.item.update",
    "quote.my_item.add",
    "quote.my_item.remove",
    "quote.my_item.update",
    "quote.notifications.read",
    "quote.outsourced_item.add",
    "quote.outsourced_item.remove",
    "quote.paid",
    "quote.submit",
    "quote.update",
    "settings.digest.update",
    "settings.vat.update",
    "user.convert_to_outsider",
    "user.create",
    "user.delete",
    "user.password.reset",
    "user.purge",
    "user.restore",
    "user.update",
}
SAFE_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


MAX_CONTEXT_BYTES = 32768
MAX_CONTEXT_DEPTH = 10
MAX_CONTEXT_ITEMS = 100

def _validate_context(value):
    def walk(node, depth=0):
        if depth > MAX_CONTEXT_DEPTH:
            raise ValueError(f'context nesting exceeds {MAX_CONTEXT_DEPTH} levels')
        if isinstance(node, dict):
            if len(node) > MAX_CONTEXT_ITEMS:
                raise ValueError(f'context object exceeds {MAX_CONTEXT_ITEMS} keys')
            return {str(k)[:200]: walk(v, depth + 1) for k, v in node.items()}
        if isinstance(node, list):
            if len(node) > MAX_CONTEXT_ITEMS:
                raise ValueError(f'context list exceeds {MAX_CONTEXT_ITEMS} items')
            return [walk(v, depth + 1) for v in node]
        if isinstance(node, str):
            return node[:5000]
        if isinstance(node, (int, float, bool)) or node is None:
            return node
        return str(node)[:5000]
    result = walk(value)
    if len(json.dumps(result, ensure_ascii=False, default=str)) > MAX_CONTEXT_BYTES:
        raise ValueError(f'context exceeds {MAX_CONTEXT_BYTES} bytes')
    return result

class ClientErrorPayload(BaseModel):
    message: str = Field(max_length=5000)
    stack: str | None = Field(default=None, max_length=12000)
    path: str | None = Field(default=None, max_length=2000)
    request_id: str | None = Field(default=None, max_length=200)
    context: dict = Field(default_factory=dict)

    @field_validator('context')
    @classmethod
    def validate_context(cls, value):
        return _validate_context(value)



def _safe_otlp_id(value: object, length: int) -> str | None:
    if not isinstance(value, str) or len(value) != length:
        return None
    lowered = value.lower()
    if not re.fullmatch(r"[0-9a-f]+", lowered):
        return None
    return lowered


def _safe_browser_attribute(key: object, value: object) -> dict | None:
    if not isinstance(key, str) or key not in SAFE_BROWSER_ATTRIBUTE_KEYS or not isinstance(value, dict):
        return None

    if "stringValue" in value:
        raw = value.get("stringValue")
        if not isinstance(raw, str):
            return None
        text = raw[:512]
        if key == "http.request.method":
            text = text.upper()
            if text not in SAFE_HTTP_METHODS:
                return None
        elif key in {"ui.action", "app.operation"}:
            if not SAFE_BROWSER_ACTION_RE.fullmatch(text):
                return None
        elif key == "url.path":
            safe_path = _safe_browser_path(text)
            if safe_path is None:
                return None
            text = safe_path
        elif key == "browser.url.path":
            if not text.startswith("/") or "?" in text or "#" in text:
                return None
            text = re.sub(r"/[0-9]+(?=/|$)", "/:id", text[:512])
            text = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,36}(?=/|$)", "/:id", text, flags=re.IGNORECASE)
            if not re.fullmatch(r"/[A-Za-z0-9_./:{}-]{0,240}", text):
                return None
        elif key == "error.type":
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", text):
                return None
        elif key == "error":
            # "error" is boolean-only (see the boolValue branch below). A
            # stringValue here is a forged/mismatched attribute shape, not a
            # legitimate encoding of the same fact, so it must be rejected
            # rather than silently accepted as free-form text.
            return None
        return {"key": key, "value": {"stringValue": text}}

    if "intValue" in value:
        # Only "http.response.status_code" is an integer-typed attribute.
        # Every other allow-listed key is string- or bool-typed (see the
        # stringValue/boolValue branches); accepting an intValue for one of
        # those would be the same forged-attribute-shape problem as the
        # "error" stringValue case above, just for a different key.
        if key != "http.response.status_code":
            return None
        raw = value.get("intValue")
        try:
            number = int(raw)
        except (TypeError, ValueError):
            return None
        if not 100 <= number <= 599:
            return None
        return {"key": key, "value": {"intValue": str(number)}}

    if "boolValue" in value:
        if key != "error" or not isinstance(value.get("boolValue"), bool):
            return None
        return {"key": key, "value": {"boolValue": value["boolValue"]}}

    return None


def _safe_browser_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or "?" in value or "#" in value:
        return None
    path = value[:512]
    path = re.sub(r"/[0-9]+(?=/|$)", "/:id", path)
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,36}(?=/|$)", "/:id", path, flags=re.IGNORECASE)
    path = re.sub(r"/(restore|download)/[^/]+(?=/|$)", r"/\1/:file", path)
    if not re.fullmatch(r"/api/[A-Za-z0-9_./:{}-]{1,240}", path):
        return None
    return path


def _sanitize_browser_otlp_payload(payload: object) -> dict | None:
    """Rebuild client OTLP data from a strict allow-list.

    The browser endpoint has server-side OTLP credentials, so it must never
    forward arbitrary client-controlled OTLP JSON. This sanitizer prevents a
    browser from smuggling credentials, cookies, request bodies, exception
    messages, events, links, or arbitrary exporter attributes into the
    collector under the application's authenticated exporter identity.
    """
    if not isinstance(payload, dict):
        return None

    resource_spans = payload.get("resourceSpans")
    if not isinstance(resource_spans, list) or not resource_spans:
        return None

    sanitized_resource_spans = []
    span_count = 0

    for resource_span in resource_spans[:4]:
        if not isinstance(resource_span, dict):
            continue

        resource_attrs = [
            {"key": "service.name", "value": {"stringValue": BROWSER_SERVICE_NAME}},
            {"key": "service.version", "value": {"stringValue": "0.1.0"}},
            {"key": "deployment.environment", "value": {"stringValue": "browser"}},
        ]

        scope_spans = resource_span.get("scopeSpans")
        if not isinstance(scope_spans, list):
            continue

        sanitized_scopes = []
        for scope_span in scope_spans[:4]:
            if not isinstance(scope_span, dict):
                continue
            raw_spans = scope_span.get("spans")
            if not isinstance(raw_spans, list):
                continue

            safe_spans = []
            for raw_span in raw_spans:
                if span_count >= MAX_BROWSER_SPANS:
                    break
                if not isinstance(raw_span, dict):
                    continue

                trace_id = _safe_otlp_id(raw_span.get("traceId"), 32)
                span_id = _safe_otlp_id(raw_span.get("spanId"), 16)
                parent_id = raw_span.get("parentSpanId")
                if parent_id:
                    parent_id = _safe_otlp_id(parent_id, 16)
                    if parent_id is None:
                        continue

                name = raw_span.get("name")
                if not isinstance(name, str):
                    continue
                name = name.strip()[:120]
                is_http_span = re.fullmatch(
                    r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (/[A-Za-z0-9_./:{}-]{1,240})",
                    name,
                )
                if SAFE_BROWSER_ACTION_RE.fullmatch(name):
                    pass
                elif name in SAFE_BROWSER_OPERATION_NAMES:
                    pass
                elif is_http_span:
                    safe_http_path = _safe_browser_path(is_http_span.group(1))
                    if safe_http_path is None:
                        continue
                    name = f"{name.split(' ', 1)[0]} {safe_http_path}"
                else:
                    continue

                kind = raw_span.get("kind")
                if kind not in (1, 3):
                    continue

                start = raw_span.get("startTimeUnixNano")
                end = raw_span.get("endTimeUnixNano")
                if not isinstance(start, str) or not start.isdigit() or len(start) > 24:
                    continue
                if not isinstance(end, str) or not end.isdigit() or len(end) > 24:
                    continue

                attrs = []
                raw_attrs = raw_span.get("attributes", [])
                if isinstance(raw_attrs, list):
                    for raw_attr in raw_attrs[:32]:
                        if not isinstance(raw_attr, dict):
                            continue
                        attr_key = raw_attr.get("key")
                        attr_value = raw_attr.get("value")
                        safe_attr = _safe_browser_attribute(attr_key, attr_value)
                        if safe_attr is not None:
                            attrs.append(safe_attr)
                        elif attr_key in SAFE_BROWSER_ATTRIBUTE_KEYS:
                            # A client-controlled value for an allow-listed key
                            # must use the exact safe OTLP type. Silently dropping
                            # a malformed value would make the security contract
                            # ambiguous and would allow a trace with a forged
                            # attribute shape to be accepted.
                            return None

                raw_status = raw_span.get("status")
                status_code = raw_status.get("code") if isinstance(raw_status, dict) else 0
                if status_code not in (0, 1, 2):
                    status_code = 0

                safe_span = {
                    "traceId": trace_id,
                    "spanId": span_id,
                    "name": name,
                    "kind": kind,
                    "startTimeUnixNano": start,
                    "endTimeUnixNano": end,
                    "attributes": attrs,
                    "status": {"code": status_code},
                }
                if parent_id:
                    safe_span["parentSpanId"] = parent_id

                safe_spans.append(safe_span)
                span_count += 1

            if safe_spans:
                sanitized_scopes.append({
                    "scope": {
                        "name": "asset-mgt-render-free/browser-telemetry",
                        "version": "1.1.0",
                    },
                    "spans": safe_spans,
                })

        if sanitized_scopes:
            sanitized_resource_spans.append({
                "resource": {"attributes": resource_attrs},
                "scopeSpans": sanitized_scopes,
            })

        if span_count >= MAX_BROWSER_SPANS:
            break

    if not sanitized_resource_spans:
        return None

    return {"resourceSpans": sanitized_resource_spans}


def _browser_otlp_endpoint() -> str | None:
    """Return the configured OTLP/HTTP trace URL, or None when unavailable.

    Browser telemetry deliberately does not support the backend's gRPC
    exporter. The standard OTLP/HTTP JSON encoding is used here so the
    browser can send standards-compliant traces without shipping a second
    binary protobuf runtime into the React bundle.
    """
    if not settings.OTEL_ENABLED:
        return None
    if settings.OTEL_EXPORTER_OTLP_PROTOCOL.lower() == "grpc":
        return None
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT.strip()
    if not endpoint:
        return None
    return endpoint if endpoint.rstrip("/").endswith("/v1/traces") else f"{endpoint.rstrip('/')}/v1/traces"


def _forward_browser_trace(body: bytes, endpoint: str) -> None:
    """Forward one already-validated OTLP/HTTP JSON batch using server secrets."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # OTEL_EXPORTER_OTLP_HEADERS stays server-side; it is never serialized
    # into the browser bundle or returned by /api/config/public.
    for pair in settings.OTEL_EXPORTER_OTLP_HEADERS.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            headers[key] = value

    request = UrlRequest(endpoint, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=5) as response:
        response.read(4096)


@router.post("/traces", status_code=202)
async def browser_traces(request: Request):
    """Receive browser OTLP/HTTP JSON and forward it without blocking the app.

    The endpoint intentionally returns 202 even if the upstream collector is
    unavailable. Telemetry must never turn a working asset-management request
    into an application error. The browser exporter is fire-and-forget and
    retries its own batch later.
    """
    endpoint = _browser_otlp_endpoint()
    if endpoint is None:
        return {"accepted": False, "reason": "browser_otel_unavailable"}

    ip = resolve_client_ip(
        request.headers.get,
        request.client.host if request.client else None,
    )
    blocked, _retry_after = _trace_limiter.check(ip)
    if blocked:
        return {"accepted": False, "reason": "rate_limited"}

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_TRACE_BODY_BYTES:
                return {"accepted": False, "reason": "payload_too_large"}
        except ValueError:
            return {"accepted": False, "reason": "invalid_content_length"}

    body = await request.body()
    if len(body) > MAX_TRACE_BODY_BYTES:
        return {"accepted": False, "reason": "payload_too_large"}

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {"accepted": False, "reason": "invalid_json"}

    sanitized_payload = _sanitize_browser_otlp_payload(payload)
    if sanitized_payload is None:
        return {"accepted": False, "reason": "invalid_otlp_payload"}

    safe_body = json.dumps(
        sanitized_payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        await run_in_threadpool(_forward_browser_trace, safe_body, endpoint)
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        # Exporter failures are deliberately isolated from application logic.
        logger.warning("browser OTLP export failed: %s", exc)
    return {"accepted": True}

@router.post("/client-error", status_code=202)
def client_error(payload: ClientErrorPayload, request: Request):
    ip = resolve_client_ip(
        request.headers.get,
        request.client.host if request.client else None,
    )
    blocked, _retry_after = _limiter.check(ip)
    if blocked:
        return {"accepted": False, "reason": "rate_limited"}

    rid = payload.request_id or request.headers.get("x-request-id")
    report_client_event(
        payload.message,
        stack=payload.stack,
        path=payload.path,
        request_id=rid,
        context=payload.context,
    )
    return {"accepted": True, "request_id": rid}
