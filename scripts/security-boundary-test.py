#!/usr/bin/env python3
"""Security boundary test (P2 item #16). External-only -- everything here
is checkable from outside the ACA network, against the public backend/
frontend URL.

Covers:
  - /api/assets without auth -> 401
  - /api/assets with an invalid/garbage session token -> 401
  - API docs (/docs, /redoc, /openapi.json) not publicly exposed in prod
    (backend/main.py gates these behind settings.ENABLE_API_DOCS, which
    defaults to False when is_production -- see config.py)
  - No secrets/internals leaking into error response bodies (DB connection
    strings, Python tracebacks, filesystem paths, API keys/JWT secrets)
  - No secrets leaking into response headers (Server/X-Powered-By version
    strings, stray Set-Cookie on non-auth endpoints)
  - CORS isn't configured as wildcard-origin + credentials (a real hole:
    allow_origins=["*"] with allow_credentials=True lets ANY site read an
    authenticated response using the victim's cookies)
  - The ErrorBeacon service itself is not reachable on any guessable
    public path off the same base URL (its ingress should be internal-only
    -- see infra/main.bicep)

WHAT THIS DOES **NOT** COVER (needs infra/log access, not an HTTP client):
  - "no secrets in logs / Bicep outputs / GitHub Actions logs" -- that's a
    manual review of `az containerapp logs show`, the Bicep template's
    `output` blocks, and the Actions run history, not something an
    external HTTP probe can see.
  - "backend internal endpoint -> inaccessible publicly" -- there's no
    separate internal-only backend route to probe from outside; ACA's own
    ingress config (internal vs external) is what enforces this, and
    that's a Bicep/portal check, not an HTTP one.

Usage:
  python scripts/security-boundary-test.py --base-url https://stack.multione.online
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

results: list[tuple[str, str, str]] = []

# Patterns that should never appear in a response body -- deliberately
# broad; a false positive here just means eyeballing the body once, which
# is cheap insurance against something like a stray traceback or DB URL.
LEAK_PATTERNS = {
    "DB connection string": re.compile(r"(?i)(postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://\S+"),
    "Python traceback": re.compile(r"Traceback \(most recent call last\)"),
    "filesystem path": re.compile(r"/(?:home|usr|app|site-packages)/[^\s\"']{3,}"),
    "JWT-looking token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    "AWS-style access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    marker = {"PASS": "\u2713", "FAIL": "\u2717", "WARN": "!"}[status]
    print(f"[{marker}] {step}: {detail}")


def expect(step: str, condition: bool, ok_detail: str, fail_detail: str) -> bool:
    record(step, PASS if condition else FAIL, ok_detail if condition else fail_detail)
    return condition


def fetch(base_url: str, path: str, headers: dict | None = None, method: str = "GET"):
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    req = Request(url, headers=headers or {}, method=method)
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.status, resp.headers, resp.read().decode(errors="replace")
    except HTTPError as e:
        return e.code, e.headers, e.read().decode(errors="replace")
    except URLError as e:
        return None, None, str(e.reason)


def check_leaks(step_label: str, body: str) -> None:
    found = [name for name, pat in LEAK_PATTERNS.items() if pat.search(body)]
    expect(
        f"{step_label}: no secrets/internals leaked in body",
        not found,
        "clean",
        f"found suspicious pattern(s): {found} -- inspect the response body manually",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="Public backend/frontend base URL, e.g. https://stack.multione.online")
    args = ap.parse_args()
    base = args.base_url

    # 1. No auth -> 401
    status, headers, body = fetch(base, "/api/assets?limit=1")
    expect("GET /api/assets without auth", status == 401, "HTTP 401", f"HTTP {status}: {body[:300]}")
    check_leaks("unauthenticated /api/assets", body or "")

    # 2. Garbage/invalid session cookie -> 401 (not 500 -- an unhandled
    #    JWT decode error should be caught, not crash the request)
    status, headers, body = fetch(base, "/api/assets?limit=1", headers={"Cookie": "access_token=not.a.real.jwt.token"})
    expect("GET /api/assets with invalid token", status == 401, "HTTP 401", f"HTTP {status}: {body[:300]}")
    check_leaks("invalid-token /api/assets", body or "")

    # 3. API docs not publicly exposed
    for doc_path in ("/docs", "/redoc", "/openapi.json"):
        status, headers, body = fetch(base, doc_path)
        if status in (404, 401, 403):
            record(f"{doc_path} not exposed", PASS, f"HTTP {status}")
        elif status == 200:
            record(f"{doc_path} not exposed", WARN, "HTTP 200 -- API docs are publicly reachable; confirm ENABLE_API_DOCS is intentionally on for this environment")
        else:
            record(f"{doc_path} not exposed", WARN, f"HTTP {status} (unreachable or unexpected) -- {body[:150]}")

    # 4. Response headers don't leak stack/version info
    status, headers, body = fetch(base, "/healthz")
    if headers is not None:
        server_header = headers.get("Server", "")
        powered_by = headers.get("X-Powered-By")
        expect(
            "no verbose version info in Server header",
            not re.search(r"\d+\.\d+", server_header or ""),
            f"Server: {server_header!r}",
            f"Server header reveals a version number: {server_header!r}",
        )
        expect("no X-Powered-By header", powered_by is None, "absent", f"present: {powered_by!r}")

    # 5. CORS: wildcard origin + credentials is the dangerous combination
    status, headers, body = fetch(base, "/api/assets?limit=1", headers={"Origin": "https://evil.example.com"})
    if headers is not None:
        allow_origin = headers.get("Access-Control-Allow-Origin")
        allow_creds = headers.get("Access-Control-Allow-Credentials")
        dangerous = allow_origin == "*" and str(allow_creds).lower() == "true"
        expect(
            "CORS is not wildcard-origin + credentials",
            not dangerous,
            f"Allow-Origin={allow_origin!r} Allow-Credentials={allow_creds!r}",
            f"DANGEROUS: Allow-Origin=* with Allow-Credentials=true lets any website read authenticated responses using a visitor's cookies",
        )
        if allow_origin and allow_origin != "*" and "evil.example.com" in allow_origin:
            record("CORS origin allowlist", FAIL, f"an untrusted Origin (https://evil.example.com) was reflected back: {allow_origin!r} -- the allowlist may not be checking origins at all")
        else:
            record("CORS origin allowlist", PASS, f"untrusted Origin was not reflected/allowed (Allow-Origin={allow_origin!r})")

    # 6. 404 body doesn't leak internals either
    status, headers, body = fetch(base, "/this-route-does-not-exist-12345")
    check_leaks("404 response", body or "")

    # 7. ErrorBeacon not reachable on any obvious public path off this base URL
    for guess in ("/errorbeacon", "/eb", ":8000/healthz"):
        probe_url = base.rstrip("/") + guess if not guess.startswith(":") else base.rstrip("/") + guess
        status, headers, body = fetch(base, guess)
        if status in (None, 404):
            record(f"ErrorBeacon not exposed at {guess}", PASS, f"HTTP {status}")
        else:
            record(f"ErrorBeacon not exposed at {guess}", WARN, f"HTTP {status} -- got a response at a guessed path; not conclusive (could just be your frontend's own 200/redirect), but worth a manual look")

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
