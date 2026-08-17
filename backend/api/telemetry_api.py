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

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from config import settings
from integrations.fastapi_errorbeacon import report_client_event
from utils.client_ip import resolve_client_ip
from utils.rate_limiter import RedisFixedWindowLimiter


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
