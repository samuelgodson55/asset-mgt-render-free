"""
app/main/auth.py
--------------------
API-key auth dependencies (separate scoped keys for ingest vs admin
endpoints) plus the in-memory rate limiters for alert ingestion, admin
auth failures, and the /v1/test endpoint.
"""

from __future__ import annotations

import hmac
import time

from fastapi import Header, HTTPException, Request

from .config import (
    ADMIN_API_KEY,
    ADMIN_AUTH_FAILURES_PER_MINUTE,
    INGEST_API_KEY,
    MAX_ALERTS,
    TEST_ALERTS_PER_MINUTE,
    TRUST_PROXY_HEADERS,
)
from .state import _admin_fail_lock, _admin_failures, _rate, _rate_lock, _test_rate, _test_rate_lock


def _client_ip_from_request(request: Request) -> str:
    # Proxy headers are attacker-controlled unless this service is known to
    # sit behind a trusted ingress/reverse proxy that overwrites them. Render
    # Free exposes ErrorBeacon publicly, so the safe default is to use the
    # immediate peer address. Controlled ACA deployments may opt in explicitly
    # with ERRORBEACON_TRUST_PROXY_HEADERS=true.
    if TRUST_PROXY_HEADERS:
        xff = request.headers.get('x-forwarded-for')
        if xff:
            return xff.split(',', 1)[0].strip()[:128] or 'unknown'
        forwarded = request.headers.get('x-real-ip')
        if forwarded:
            return forwarded.strip()[:128] or 'unknown'
    return request.client.host if request.client else 'unknown'

def _check_api_key(x_api_key: str | None, configured: str, label: str) -> bool:
    if not configured:
        raise HTTPException(503, f'ErrorBeacon is not configured with {label}')
    if not x_api_key or not hmac.compare_digest(x_api_key, configured):
        raise HTTPException(401, 'Invalid API key')
    return True

def ingest_auth(x_api_key: str | None = Header(default=None)):
    return _check_api_key(x_api_key, INGEST_API_KEY, 'ERRORBEACON_INGEST_API_KEY')

def admin_auth(request: Request, x_api_key: str | None = Header(default=None)):
    ip = _client_ip_from_request(request)
    now = time.time()
    with _admin_fail_lock:
        bucket = _admin_failures.setdefault(ip, [])
        bucket[:] = [t for t in bucket if t > now - 60]
        if len(bucket) >= ADMIN_AUTH_FAILURES_PER_MINUTE:
            raise HTTPException(429, 'Too many invalid admin authentication attempts; try again later')
    try:
        return _check_api_key(x_api_key, ADMIN_API_KEY, 'ERRORBEACON_ADMIN_API_KEY')
    except HTTPException as exc:
        if exc.status_code == 401:
            with _admin_fail_lock:
                bucket = _admin_failures.setdefault(ip, [])
                bucket.append(now)
                if len(_admin_failures) > 5000:
                    oldest = sorted(_admin_failures.items(), key=lambda item: max(item[1]) if item[1] else 0)[:500]
                    for key, _ in oldest:
                        _admin_failures.pop(key, None)
        raise

def auth(request: Request, x_api_key: str | None = Header(default=None)):
    return admin_auth(request, x_api_key)

def limited():
    now=time.time()
    with _rate_lock:
        _rate[:]=[x for x in _rate if x>now-60]
        if len(_rate)>=MAX_ALERTS:
            return False
        _rate.append(now)
        return True

def limited_test():
    now=time.time()
    with _test_rate_lock:
        _test_rate[:]=[x for x in _test_rate if x>now-60]
        if len(_test_rate)>=TEST_ALERTS_PER_MINUTE:
            return False
        _test_rate.append(now)
        return True
