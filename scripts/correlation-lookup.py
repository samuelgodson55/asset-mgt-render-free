#!/usr/bin/env python3
"""Correlation ID propagation test -- INTERNAL leg (P2 item #15), pairs with
scripts/correlation-id-test.py.

RUN THIS INSIDE THE BACKEND CONTAINER (same reasoning as
errorbeacon-pipeline-test.py -- ErrorBeacon's ingress is internal-only):

  az containerapp exec --name backend --resource-group rg-snipeit-lite-prod

Then:

  python3 scripts/correlation-lookup.py --request-id corr-test-xxxxxxxx

Looks up GET /v1/incidents on ErrorBeacon and finds the incident whose
request_id matches the one scripts/correlation-id-test.py just sent from
outside, proving the SAME id survived frontend(simulated) -> backend ->
ErrorBeacon -> (eventually) Telegram, not just that each hop worked in
isolation.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

PASS = "PASS"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    marker = {"PASS": "\u2713", "FAIL": "\u2717"}[status]
    print(f"[{marker}] {step}: {detail}")


def call(method: str, url: str, api_key: str, timeout: float = 10.0):
    req = urllib.request.Request(url, method=method, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        status = e.code
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--request-id", required=True, help="The request_id printed by scripts/correlation-id-test.py")
    ap.add_argument("--wait-seconds", type=int, default=30, help="How long to poll for the incident to show up / settle.")
    args = ap.parse_args()

    base_url = os.environ.get("ERRORBEACON_URL", "").rstrip("/")
    api_key = os.environ.get("ERRORBEACON_API_KEY", "")
    if not base_url or not api_key:
        record("ERRORBEACON_URL / ERRORBEACON_API_KEY set", FAIL, f"URL={'set' if base_url else 'MISSING'} KEY={'set' if api_key else 'MISSING'}")
        print_summary()
        return 1

    deadline = time.time() + args.wait_seconds
    match = None
    while time.time() < deadline:
        status, body = call("GET", f"{base_url}/v1/incidents?limit=100", api_key)
        if status == 200 and isinstance(body, list):
            match = next((row for row in body if row.get("request_id") == args.request_id), None)
            if match:
                break
        time.sleep(2)

    if not expect("incident found with matching request_id", match is not None, f"found incident {match.get('id') if match else None}", f"no incident with request_id={args.request_id!r} found within {args.wait_seconds}s -- check that correlation-id-test.py ran successfully and ERRORBEACON_API_KEY on the backend matches this container's"):
        print_summary()
        return 1

    expect("component recorded as 'frontend'", match.get("component") == "frontend", f"component={match.get('component')}", f"component={match.get('component')!r}, expected 'frontend' (see report_client_event())")
    telegram_sent = match.get("telegram_sent")
    expect("telegram_sent is set (true or false, not null)", telegram_sent is not None, f"telegram_sent={telegram_sent}", "telegram_sent is null -- delivery may still be in flight or the worker never picked it up")

    print(f"\nFull matched incident:\n{json.dumps(match, indent=2, default=str)}")
    print("\nIf telegram_configured (see errorbeacon-pipeline-test.py's /healthz output) and telegram_sent is true,")
    print("go check your Telegram channel now for a message containing this exact request_id -- that's the final,")
    print("human-visible confirmation that correlation ID survives the full chain.")

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
