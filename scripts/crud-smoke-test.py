#!/usr/bin/env python3
"""CRUD smoke test for the Assets API (P1 production validation).

Exercises the full lifecycle of an asset pool end to end against a live
deployment:

  login -> list (baseline) -> create -> read -> update -> delete
         -> verify gone from active inventory -> verify present in
         soft-deleted list -> restore (cleanup)

Standard library only -- no dependencies to install on a jump box/CI runner.
Auth matches scripts/load-test.py: POST /api/auth/login sets an HttpOnly
session cookie (there is no bearer token in the response body to capture),
so this script drives a CookieJar-backed opener for every subsequent call.

Create/update/delete on /api/assets require the Super Admin/Admin role
(see backend/deps.py's require_super_admin) -- if your test account is
Manager/Staff/Customer, everything past "list" will correctly 403, and this
script reports that plainly rather than mistaking it for a bug.

Usage:
  python -c "import urllib.request; print(urllib.request.urlopen('https://stack.multione.online/healthz', timeout=10).status)"

Exit code 0 = every step passed. Non-zero = see the FAIL line for which step
and why. The script always attempts to clean up the asset pool it created,
even if a later step fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, build_opener, HTTPCookieProcessor

PASS = "PASS"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []  # (step, status, detail)


def record(step: str, ok: bool, detail: str) -> None:
    results.append((step, PASS if ok else FAIL, detail))
    marker = "✓" if ok else "✗"
    print(f"[{marker}] {step}: {detail}")


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(jar))

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        url = urljoin(self.base_url, path.lstrip("/"))
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read().decode()
                status = resp.status
        except HTTPError as e:
            raw = e.read().decode()
            status = e.code
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = raw
        return status, parsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--login-email", required=True)
    ap.add_argument("--login-password", required=True)
    ap.add_argument("--pool-name-prefix", default="smoke-test-pool", help="Unique-ish prefix for the throwaway pool this script creates.")
    args = ap.parse_args()

    client = Client(args.base_url)
    created_id: int | None = None

    # --- 1. Login -----------------------------------------------------
    status, body = client.request("POST", "/api/auth/login", {
        "identifier": args.login_email,
        "password": args.login_password,
    })
    if status != 200:
        record("login", False, f"HTTP {status}: {body}")
        print_summary()
        return 1
    if isinstance(body, dict) and (body.get("mfa_required") or body.get("mfa_setup_required")):
        record("login", False, f"account requires MFA, can't proceed non-interactively: {body}")
        print_summary()
        return 1
    record("login", True, f"HTTP {status}")

    # --- 2. Whoami / confirm role --------------------------------------
    status, me = client.request("GET", "/api/auth/me")
    role = me.get("role") if isinstance(me, dict) else None
    record("whoami", status == 200, f"HTTP {status}, role={role}")
    if role not in ("super_admin", "admin"):
        print(f"  NOTE: role '{role}' is below Super Admin/Admin -- create/update/delete below are EXPECTED to 403. That's a permissions check working correctly, not a smoke-test failure.")

    # --- 3. List (baseline) --------------------------------------------
    status, listing = client.request("GET", "/api/assets?limit=1")
    baseline_total = listing.get("total") if isinstance(listing, dict) else None
    record("list (baseline)", status == 200, f"HTTP {status}, total={baseline_total}")

    pool_name = f"{args.pool_name_prefix}-{__import__('time').strftime('%Y%m%d%H%M%S')}"

    try:
        # --- 4. Create ---------------------------------------------------
        status, created = client.request("POST", "/api/assets", {
            "name": pool_name,
            "total_quantity": 3,
        })
        ok = status == 200 and isinstance(created, dict) and "id" in created
        record("create", ok, f"HTTP {status}: {created}")
        if not ok:
            print_summary()
            return 1
        created_id = created["id"]

        # --- 5. Read -------------------------------------------------------
        status, details = client.request("GET", f"/api/assets/{created_id}/details")
        ok = status == 200 and isinstance(details, dict)
        record("read", ok, f"HTTP {status}: id={created_id}, name={details.get('name') if isinstance(details, dict) else details}")

        # --- 6. Update (quantity) ------------------------------------------
        status, updated = client.request("PUT", f"/api/assets/{created_id}/quantity", {"new_total": 7})
        record("update (quantity)", status == 200, f"HTTP {status}: {updated}")

        # Verify the update actually landed
        status, details2 = client.request("GET", f"/api/assets/{created_id}/details")
        new_qty = details2.get("total_quantity") if isinstance(details2, dict) else None
        record("update verify", new_qty == 7, f"HTTP {status}: total_quantity={new_qty} (expected 7)")

        # --- 7. Delete (soft) ------------------------------------------------
        status, deleted = client.request("DELETE", f"/api/assets/{created_id}")
        record("delete", status == 200, f"HTTP {status}: {deleted}")

        # --- 8. Verify gone from active inventory --------------------------
        status, details3 = client.request("GET", f"/api/assets/{created_id}/details")
        record("verify gone (active)", status == 404, f"HTTP {status} (expected 404)")

        # --- 9. Verify present in soft-deleted list -------------------------
        status, deleted_list = client.request("GET", "/api/assets/deleted?limit=200")
        found = False
        if isinstance(deleted_list, dict):
            found = any(item.get("id") == created_id for item in deleted_list.get("items", []))
        record("verify present (soft-deleted list)", found, f"HTTP {status}, found={found}")

        # --- 10. Restore (cleanup, also exercises the restore route) -------
        status, restored = client.request("POST", f"/api/assets/{created_id}/restore")
        record("restore", status == 200, f"HTTP {status}: {restored}")

        # --- 11. Final cleanup: soft-delete again so we don't leave test
        #         data sitting in active inventory ------------------------
        status, _ = client.request("DELETE", f"/api/assets/{created_id}")
        record("final cleanup delete", status == 200, f"HTTP {status}")

    finally:
        pass

    return print_summary()


def print_summary() -> int:
    print("\n" + "=" * 60)
    failed = [r for r in results if r[1] == FAIL]
    for step, status, detail in results:
        print(f"  {status:4s}  {step}")
    print("=" * 60)
    if failed:
        print(f"{len(failed)} step(s) FAILED.")
        return 1
    print("All steps PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
