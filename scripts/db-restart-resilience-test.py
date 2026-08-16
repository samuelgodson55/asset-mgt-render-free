#!/usr/bin/env python3
"""DB persistence + backend restart resilience test (P1 items #11 and #12).

This is a THREE-PHASE script because a real Azure restart happens between
phases 1 and 2/3, and this script can't trigger that itself (it has no
Azure credentials, deliberately -- it's an HTTP-only smoke test, same as
the others here):

  Phase "before"  Create/update a marker asset pool, print its ID, exit.
  [ -- you restart the backend revision here, via Azure CLI -- ]
  Phase "after"   Poll /healthz -> /readyz -> an authenticated request
                  until the new replica is fully back, timing the outage,
                  then verify the marker pool from phase "before" still
                  has the exact value you set -- proving it round-tripped
                  through a real restart via the database, not an
                  in-process cache that would've reset to empty.
  Phase "cleanup" Remove the marker pool.

USAGE
-----
  # 1. Before restarting anything:
  python scripts/db-restart-resilience-test.py before \
      --base-url https://stack.multione.online \
      --login-email r.adeyemi@corp.io --login-password 'Admin123!'
  # -> prints a Pool ID and a marker quantity. Note the Pool ID.

  # 2. Now restart the backend revision (separate terminal / Azure CLI):
  az containerapp revision list --name backend --resource-group rg-snipeit-lite-prod -o table
  az containerapp revision restart --name backend --resource-group rg-snipeit-lite-prod --revision <revision-name>

  # 3. Immediately after issuing the restart:
  python scripts/db-restart-resilience-test.py after \
      --base-url https://stack.multione.online \
      --login-email r.adeyemi@corp.io --login-password 'Admin123!' \
      --pool-id <the Pool ID from step 1>

  # 4. Clean up the marker pool:
  python scripts/db-restart-resilience-test.py cleanup \
      --base-url https://stack.multione.online \
      --login-email r.adeyemi@corp.io --login-password 'Admin123!' \
      --pool-id <the Pool ID from step 1>

Standard library only, same Client/auth pattern as the other scripts here.
Requires Super Admin/Admin (create/update/delete are all admin-gated).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, build_opener, HTTPCookieProcessor

PASS = "PASS"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    marker = {"PASS": "\u2713", "FAIL": "\u2717"}[status]
    print(f"[{marker}] {step}: {detail}")


def expect(step: str, condition: bool, ok_detail: str, fail_detail: str) -> bool:
    record(step, PASS if condition else FAIL, ok_detail if condition else fail_detail)
    return condition


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(jar))

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0):
        url = urljoin(self.base_url, path.lstrip("/"))
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode(errors="replace")
                status = resp.status
        except HTTPError as e:
            raw = e.read().decode(errors="replace")
            status = e.code
        except URLError:
            return None, None  # unreachable -- expected during the actual restart window
        try:
            return status, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return status, raw


def login(client: Client, email: str, password: str) -> bool:
    status, body = client.request("POST", "/api/auth/login", {"identifier": email, "password": password})
    return expect("login", status == 200, "HTTP 200", f"HTTP {status}: {body}")


MARKER_PREFIX = "zzz-restart-persistence-marker-"


def phase_before(client: Client, args) -> int:
    marker_qty = int(time.time()) % 100000  # a value we can't have picked by coincidence
    name = f"{MARKER_PREFIX}{uuid.uuid4().hex[:8]}"
    status, body = client.request("POST", "/api/assets", {"name": name, "total_quantity": marker_qty, "category": "zzz-restart-test"})
    if not expect("create marker pool", status == 200, f"HTTP 200: {body}", f"HTTP {status}: {body}"):
        print_summary()
        return 1
    pool_id = body.get("id")
    print(f"\nPool ID:   {pool_id}")
    print(f"Name:      {name}")
    print(f"Marker qty:{marker_qty}")
    print("\nSave the Pool ID above -- now restart the backend revision, then run the 'after' phase with --pool-id.")
    print_summary()
    return 0


def phase_after(client: Client, args) -> int:
    if not args.pool_id:
        print("ERROR: --pool-id is required for the 'after' phase (use the ID printed by the 'before' phase).")
        return 1

    # 1. Time the outage: poll /healthz until it's reachable and 200 again.
    print("Polling /healthz ...")
    outage_start = time.time()
    healthy_at = None
    deadline = outage_start + args.max_wait_seconds
    while time.time() < deadline:
        status, _ = client.request("GET", "/healthz", timeout=5)
        if status == 200:
            healthy_at = time.time()
            break
        time.sleep(1)
    expect(
        "backend became reachable (/healthz) within timeout",
        healthy_at is not None,
        f"reachable again after {healthy_at - outage_start:.1f}s",
        f"never became reachable within {args.max_wait_seconds}s -- restart may still be in progress, or it did not recover",
    )
    if healthy_at is None:
        print_summary()
        return 1

    # 2. /readyz -- schema-aware readiness, should also recover on its own.
    print("Polling /readyz ...")
    ready_at = None
    deadline = time.time() + args.max_wait_seconds
    while time.time() < deadline:
        status, body = client.request("GET", "/readyz", timeout=5)
        if status == 200:
            ready_at = time.time()
            break
        time.sleep(1)
    expect(
        "backend became ready (/readyz) within timeout",
        ready_at is not None,
        f"ready after {ready_at - outage_start:.1f}s total" if ready_at else "n/a",
        f"never reported ready within {args.max_wait_seconds}s of /healthz recovering -- check schema/migration status",
    )

    # 3. Authenticated request works again -- fresh login (cookie from a
    #    pre-restart session should still work too, since JWTs aren't tied
    #    to any in-memory server state, but re-login is the more realistic
    #    "app recovers without manual intervention" check).
    if not login(client, args.login_email, args.login_password):
        print_summary()
        return 1
    status, body = client.request("GET", "/api/assets?limit=1")
    expect("authenticated API request after restart", status == 200, f"HTTP 200, total={body.get('total') if isinstance(body, dict) else '?'}", f"HTTP {status}: {body}")

    # 4. THE ACTUAL PERSISTENCE CHECK: the marker pool created before the
    #    restart must still exist with its exact marker quantity. If the
    #    new replica came up with a different (or missing) value, that
    #    points at ephemeral storage, a stale connection pool serving
    #    cached/wrong data, or a migration that silently altered data.
    status, body = client.request("GET", f"/api/assets/{args.pool_id}/details")
    if expect("marker pool still exists after restart", status == 200, f"HTTP 200", f"HTTP {status}: {body}"):
        total_qty = body.get("total_quantity")
        print(f"    marker pool total_quantity after restart: {total_qty}")
        # We don't know the exact value here without it being passed in --
        # the 'before' phase printed it, so this is a manual visual
        # confirmation. If you want a hard assertion, pass --expect-quantity.
        if args.expect_quantity is not None:
            expect(
                "marker quantity matches what was set before the restart",
                total_qty == args.expect_quantity,
                f"{total_qty} == {args.expect_quantity}",
                f"{total_qty} != expected {args.expect_quantity} -- data did not persist correctly across the restart",
            )

    print_summary()
    return 1 if any(s == FAIL for _, s, _ in results) else 0


def phase_cleanup(client: Client, args) -> int:
    if not args.pool_id:
        print("ERROR: --pool-id is required for cleanup.")
        return 1
    if not login(client, args.login_email, args.login_password):
        print_summary()
        return 1
    status, body = client.request("DELETE", f"/api/assets/{args.pool_id}")
    expect("marker pool deleted", status == 200, f"HTTP 200: {body}", f"HTTP {status}: {body}")
    if any(s == PASS for _, s, _ in results):
        status, body = client.request("POST", f"/api/assets/{args.pool_id}/purge")
        expect("marker pool purged", status == 200, f"HTTP 200: {body}", f"HTTP {status}: {body}")
    print_summary()
    return 1 if any(s == FAIL for _, s, _ in results) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["before", "after", "cleanup"])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--login-email", required=True)
    ap.add_argument("--login-password", required=True)
    ap.add_argument("--pool-id", type=int, default=None, help="Required for 'after' and 'cleanup'.")
    ap.add_argument("--expect-quantity", type=int, default=None, help="Optional: the marker quantity printed by the 'before' phase, for a hard pass/fail check in 'after'.")
    ap.add_argument("--max-wait-seconds", type=int, default=180, help="How long to wait for the backend to recover in the 'after' phase.")
    args = ap.parse_args()

    client = Client(args.base_url)

    if args.phase == "before":
        if not login(client, args.login_email, args.login_password):
            print_summary()
            return 1
        return phase_before(client, args)
    elif args.phase == "after":
        return phase_after(client, args)
    else:
        return phase_cleanup(client, args)


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
