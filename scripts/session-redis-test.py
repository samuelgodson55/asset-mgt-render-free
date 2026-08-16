#!/usr/bin/env python3
"""Redis + session smoke test (P1 production validation, item #10).

There is no direct "is Redis up" endpoint in this app (neither /healthz
nor /readyz touch Redis -- see backend/main.py) so this script validates
the two things Redis actually backs, end to end:

  1. SESSION LIFECYCLE (JWT-in-HttpOnly-cookie, not Redis-backed):
     login -> authenticated request works -> logout -> authenticated
     request is rejected. Note: logout only clears the browser's cookie
     (see auth_api.py's logout(), which just re-sets access_token with
     max_age=0) -- it does NOT revoke the JWT server-side. This script's
     "authenticated request rejected after logout" step is only true
     because the CookieJar client stops SENDING the cookie once the
     server expires it; a raw copy of the old token would still decode
     successfully until its normal expiry. That's expected JWT behavior
     for this app, not a bug -- flagged here so it isn't mistaken for one.

  2. REDIS-BACKED LOGIN RATE LIMITER (middleware/rate_limit.py):
     the ONLY thing in this codebase that actually talks to Redis on the
     request path (besides Celery, which isn't exercised by an HTTP
     smoke test). It fails OPEN if Redis is unreachable -- i.e. if Redis
     were down, every attempt below would return 401 and NONE would ever
     return 429. So seeing a 429 on attempt N+1 is positive evidence
     Redis is up, reachable from the backend, and the limiter's Redis
     INCR/EXPIRE path is working -- not just "the app is running".

     Uses a GUARANTEED-NONEXISTENT identifier for every attempt, since
     ACCOUNT_LOCKOUT_MAX_ATTEMPTS (services/auth_service.py) is ALSO 5 by
     default -- reusing the real test account's email here would risk
     locking it out for ACCOUNT_LOCKOUT_DURATION_MINUTES. A nonexistent
     identifier fails the "no matching account" branch, which never
     touches per-user lockout state, so only the IP-based Redis counter
     is exercised.

SIDE EFFECT: the rate-limit check consumes this machine's IP's login
rate-limit budget for the current LOGIN_RATE_LIMIT_WINDOW_SECONDS window
(60s by default). If you're chaining this before/after another script
that also logs in (crud-smoke-test.py, csv-export-test.py, load-test.py),
either run this one last or leave ~60s between runs so you don't trip the
limiter on a legitimate test login.

Usage:
  python scripts/session-redis-test.py \
      --base-url https://stack.multione.online \
      --login-email r.adeyemi@corp.io \
      --login-password 'Admin123!'

Standard library only, same Client/auth pattern as the other scripts here.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, build_opener, HTTPCookieProcessor

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

results: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    marker = {"PASS": "\u2713", "FAIL": "\u2717", "WARN": "!"}[status]
    print(f"[{marker}] {step}: {detail}")


class Client:
    def __init__(self, base_url: str, use_cookies: bool = True) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(jar)) if use_cookies else build_opener()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, "Message", dict | str]:
        url = urljoin(self.base_url, path.lstrip("/"))
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = Request(url, data=data, method=method, headers=headers)
        try:
            resp = self.opener.open(req, timeout=20)
            raw = resp.read().decode(errors="replace")
            status, hdrs = resp.status, resp.headers
        except HTTPError as e:
            raw = e.read().decode(errors="replace")
            status, hdrs = e.code, e.headers
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = raw
        return status, hdrs, parsed


def expect(step: str, condition: bool, ok_detail: str, fail_detail: str) -> bool:
    record(step, PASS if condition else FAIL, ok_detail if condition else fail_detail)
    return condition


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--login-email", required=True)
    ap.add_argument("--login-password", required=True)
    ap.add_argument("--rate-limit-max", type=int, default=5, help="Must match backend LOGIN_RATE_LIMIT_MAX (default 5) for the 429 check to land on the right attempt.")
    args = ap.parse_args()

    # --- 1. Session lifecycle ------------------------------------------------
    session = Client(args.base_url, use_cookies=True)

    status, _, body = session.request("POST", "/api/auth/login", {"identifier": args.login_email, "password": args.login_password})
    if not expect("login", status == 200, "HTTP 200", f"HTTP {status}: {body}"):
        print_summary()
        return 1

    status, _, body = session.request("GET", "/api/auth/me")
    expect("authenticated request before logout", status == 200, f"HTTP 200: {body.get('email') if isinstance(body, dict) else body}", f"HTTP {status}: {body}")

    status, headers, body = session.request("POST", "/api/auth/logout")
    if expect("logout", status == 200, "HTTP 200", f"HTTP {status}: {body}"):
        set_cookie = headers.get("Set-Cookie", "")
        cookie_cleared = "access_token=" in set_cookie and ("Max-Age=0" in set_cookie or "max-age=0" in set_cookie.lower())
        expect(
            "logout clears session cookie",
            cookie_cleared,
            f"Set-Cookie expires access_token: {set_cookie}",
            f"unexpected Set-Cookie on logout: {set_cookie!r}",
        )

    status, _, body = session.request("GET", "/api/auth/me")
    expect(
        "authenticated request rejected after logout",
        status == 401,
        "HTTP 401 as expected (client no longer sends a cookie)",
        f"expected 401, got HTTP {status}: {body}",
    )

    # --- 2. Redis-backed login rate limiter ----------------------------------
    anon = Client(args.base_url, use_cookies=False)
    bogus_identifier = f"nonexistent-{uuid.uuid4().hex[:12]}@ratelimit-probe.invalid"

    limited_at = None
    statuses: list[int] = []
    for attempt in range(1, args.rate_limit_max + 2):
        status, headers, body = anon.request("POST", "/api/auth/login", {"identifier": bogus_identifier, "password": "not-a-real-password"})
        statuses.append(status)
        if status == 429:
            limited_at = attempt
            retry_after = headers.get("Retry-After")
            expect(
                "rate limiter: Retry-After header present on 429",
                bool(retry_after and retry_after.isdigit()),
                f"Retry-After: {retry_after}",
                f"missing/invalid Retry-After header: {retry_after!r}",
            )
            break

    pre_limit_ok = statuses[: args.rate_limit_max].count(401) == min(len(statuses), args.rate_limit_max) if limited_at else None
    expect(
        "rate limiter: Redis is enforcing the login rate limit",
        limited_at is not None,
        f"blocked with HTTP 429 on attempt {limited_at} (statuses so far: {statuses}) -- Redis is reachable and the limiter's INCR path is live, not fail-open",
        f"never got a 429 after {len(statuses)} attempts (statuses: {statuses}) -- either the limit is configured higher than --rate-limit-max={args.rate_limit_max}, or Redis is unreachable and the limiter is fail-open right now",
    )
    if limited_at is not None and statuses[:limited_at - 1] and any(s != 401 for s in statuses[:limited_at - 1]):
        record("rate limiter: pre-block attempts behaved as expected (401)", WARN, f"statuses before the block: {statuses[:limited_at - 1]}")

    print(f"\nNote: this machine's IP is now rate-limited for login for up to LOGIN_RATE_LIMIT_WINDOW_SECONDS ({60}s default) -- wait before running another script that logs in.")

    print_summary()
    return 1 if any(s == FAIL for _, s, _ in results) else 0


def print_summary() -> None:
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    warned = sum(1 for _, s, _ in results if s == WARN)
    print(f"\n{passed} passed, {failed} failed, {warned} informational")
    if failed:
        print("\nFailures:")
        for step, status, detail in results:
            if status == FAIL:
                print(f"  - {step}: {detail}")


if __name__ == "__main__":
    sys.exit(main())
