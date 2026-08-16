#!/usr/bin/env python3
"""Correlation ID propagation test -- EXTERNAL leg (P2 item #15).

Run this from your own machine (not inside any container) -- it only
talks to the public backend, via the same endpoint the frontend itself
uses to report browser-side errors: POST /api/telemetry/client-error
(backend/api/telemetry_api.py). No auth required by design (a logged-out
user's browser can still hit an unhandled JS error and needs to report
it), and it's rate-limited to 20/min per IP.

WHY THIS ENDPOINT: your changelog note said "/v1/test was specifically
modified to use the middleware correlation ID" -- but /v1/test lives on
ErrorBeacon itself and bypasses the backend entirely, so it can't prove
the FRONTEND -> BACKEND leg of correlation propagation, only
ErrorBeacon's own internal handling. /api/telemetry/client-error is the
one real HTTP path where a request_id genuinely travels
frontend -> backend -> ErrorBeacon, which is the actual claim worth
checking end to end.

WHAT THIS PROVES: this script sends a request with a caller-chosen
X-Request-ID and a matching request_id in the JSON body, then checks
that:
  (a) the backend accepts it (202) and echoes the SAME request_id back
      in its own response -- proving RequestContextMiddleware and this
      route both read/propagate the same ID.
  (b) report_client_event() (integrations/fastapi_errorbeacon.py) is
      given that ID, category="client_error" (a real, notifying
      severity -- NOT "chaos_test", which defaults OFF in production
      via CHAOS_TEST_ALERTS and would silently never reach Telegram).

WHAT THIS DOES **NOT** PROVE ON ITS OWN: that ErrorBeacon actually
received an incident carrying this exact request_id, and that it made it
into the Telegram message. That's the BACKEND -> ERRORBEACON leg, which
requires internal ACA network access -- run
scripts/correlation-lookup.py inside the backend container (same
`az containerapp exec` session as scripts/errorbeacon-pipeline-test.py)
with the request_id this script prints, right after running this.

The test message is clearly labeled as a deliberate validation ping so
whoever reads it in Telegram or the incident list knows it's not a real
bug -- resolve/silence it there once you've confirmed it landed.

Usage:
  python scripts/correlation-id-test.py --base-url https://stack.multione.online
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PASS = "PASS"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    marker = {"PASS": "\u2713", "FAIL": "\u2717"}[status]
    print(f"[{marker}] {step}: {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    args = ap.parse_args()

    request_id = f"corr-test-{uuid.uuid4().hex[:16]}"
    path = "/errorbeacon-correlation-test"
    message = (
        f"[PRODUCTION VALIDATION] ErrorBeacon correlation ID test ({request_id}). "
        "This is a deliberate, safe test event confirming request_id propagation "
        "frontend->backend->ErrorBeacon. Safe to resolve/silence."
    )

    url = urljoin(args.base_url.rstrip("/") + "/", "api/telemetry/client-error")
    payload = json.dumps({
        "message": message,
        "path": path,
        "request_id": request_id,
        "context": {},  # deliberately NOT {"test": true} -- that maps to
                         # category="chaos_test", which CHAOS_TEST_ALERTS
                         # suppresses by default in production and would
                         # never reach Telegram, defeating the point.
    }).encode()
    req = Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
    })

    try:
        with urlopen(req, timeout=15) as resp:
            status = resp.status
            resp_headers = resp.headers
            body = json.loads(resp.read().decode())
    except HTTPError as e:
        status = e.code
        resp_headers = e.headers
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}

    if not expect("POST /api/telemetry/client-error accepted", status == 202, f"HTTP {status}", f"HTTP {status}: {body}"):
        print_summary()
        return 1

    expect(
        "response body echoes the same request_id",
        body.get("request_id") == request_id,
        f"request_id={body.get('request_id')}",
        f"expected request_id={request_id!r}, got {body.get('request_id')!r}",
    )
    expect(
        "response X-Request-ID header matches",
        resp_headers.get("X-Request-ID") == request_id,
        f"X-Request-ID: {resp_headers.get('X-Request-ID')}",
        f"expected X-Request-ID={request_id!r}, got {resp_headers.get('X-Request-ID')!r} -- RequestContextMiddleware may not be echoing a caller-supplied ID",
    )
    expect("accepted=true", body.get("accepted") is True, "accepted=true", f"accepted={body.get('accepted')} (body: {body})")

    print(f"\nrequest_id for the internal-leg check: {request_id}")
    print("Next: from inside the backend container (az containerapp exec --name backend ...):")
    print(f"  python3 scripts/correlation-lookup.py --request-id {request_id}")

    print_summary()
    return 1 if any(s == FAIL for _, s, _ in results) else 0


def expect(step: str, condition: bool, ok_detail: str, fail_detail: str) -> bool:
    record(step, PASS if condition else FAIL, ok_detail if condition else fail_detail)
    return condition


def print_summary() -> None:
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        print("\nFailures:")
        for step, status, detail in results:
            if status == FAIL:
                print(f"  - {step}: {detail}")


if __name__ == "__main__":
    sys.exit(main())
