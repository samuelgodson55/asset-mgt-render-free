#!/usr/bin/env python3
"""Seed/cleanup throwaway asset pools so csv-export-test.py's "large-ish
dataset" check can be exercised for real, instead of just lowering
--large-threshold to match whatever happens to be in production today.

Two modes:

  seed     Creates --count new asset pools (default 250), all tagged with
           --category (default "zzz-csv-export-loadtest") and named
           "zzz-csv-export-loadtest-0001" .. "...-0250", via repeated
           POST /api/assets (Super Admin/Admin only, same as the Asset
           Inventory "+ New Pool" button). total_quantity=1 each, no price/
           department, so they don't skew stock-visibility dashboards
           beyond adding count.

  cleanup  Finds every pool under --category and removes it: soft-delete
           (DELETE /api/assets/{id}) then purge (POST /api/assets/{id}/purge)
           so nothing lingers in the "Restore Deleted Assets" panel either.
           Safe to re-run -- if seed partially failed, cleanup only acts on
           whatever actually exists under that category.

Usage:
  python scripts/seed-large-dataset.py seed    --base-url https://stack.multione.online --login-email r.adeyemi@corp.io --login-password 'Admin123!' --count 250
  python scripts/csv-export-test.py            --base-url https://stack.multione.online --login-email r.adeyemi@corp.io --login-password 'Admin123!' --large-threshold 200
  python scripts/seed-large-dataset.py cleanup --base-url https://stack.multione.online --login-email r.adeyemi@corp.io --login-password 'Admin123!'

Standard library only, same Client/auth pattern as crud-smoke-test.py and
csv-export-test.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, build_opener, HTTPCookieProcessor

DEFAULT_CATEGORY = "zzz-csv-export-loadtest"
NAME_PREFIX = "zzz-csv-export-loadtest-"


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
            with self.opener.open(req, timeout=20) as resp:
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


def login(client: Client, email: str, password: str) -> None:
    status, body = client.request("POST", "/api/auth/login", {"identifier": email, "password": password})
    if status != 200:
        print(f"[FAIL] login: HTTP {status}: {body}")
        sys.exit(1)
    print("[PASS] login: HTTP 200")


def seed(client: Client, count: int, category: str) -> int:
    """Sequential, since each create shares one authenticated CookieJar
    session and the server-side unique-name check makes this cheap enough
    (a few hundred single-row inserts) not to need concurrency."""
    created = 0
    failed = 0
    for i in range(1, count + 1):
        name = f"{NAME_PREFIX}{i:04d}"
        status, body = client.request("POST", "/api/assets", {
            "name": name,
            "total_quantity": 1,
            "category": category,
        })
        if status == 200:
            created += 1
        else:
            failed += 1
            print(f"  ! create {name} failed: HTTP {status}: {body}")
    print(f"[{'PASS' if failed == 0 else 'WARN'}] seed: {created} created, {failed} failed (category={category!r})")
    return created


def _list_all_in_category(client: Client, category: str) -> list[dict]:
    items: list[dict] = []
    offset = 0
    limit = 1000
    while True:
        status, body = client.request("GET", f"/api/assets?limit={limit}&offset={offset}&category={category}")
        if status != 200 or not isinstance(body, dict):
            print(f"[FAIL] listing category={category!r} at offset={offset}: HTTP {status}: {body}")
            break
        page = body.get("items", [])
        items.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return items


def cleanup(client: Client, category: str) -> None:
    items = _list_all_in_category(client, category)
    if not items:
        print(f"[PASS] cleanup: nothing found under category={category!r} (already clean)")
        return

    deleted = 0
    purged = 0
    failed = 0
    for item in items:
        asset_id = item["id"]
        status, body = client.request("DELETE", f"/api/assets/{asset_id}")
        if status != 200:
            failed += 1
            print(f"  ! delete pool {asset_id} failed: HTTP {status}: {body}")
            continue
        deleted += 1
        status, body = client.request("POST", f"/api/assets/{asset_id}/purge")
        if status == 200:
            purged += 1
        else:
            print(f"  ! purge pool {asset_id} failed (soft-deleted OK, but not purged): HTTP {status}: {body}")

    print(f"[{'PASS' if failed == 0 else 'WARN'}] cleanup: {deleted}/{len(items)} soft-deleted, {purged}/{deleted} purged, {failed} failed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["seed", "cleanup"])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--login-email", required=True)
    ap.add_argument("--login-password", required=True)
    ap.add_argument("--count", type=int, default=250, help="Pools to create (seed mode only).")
    ap.add_argument("--category", default=DEFAULT_CATEGORY)
    args = ap.parse_args()

    client = Client(args.base_url)
    login(client, args.login_email, args.login_password)

    if args.mode == "seed":
        seed(client, args.count, args.category)
    else:
        cleanup(client, args.category)
    return 0


if __name__ == "__main__":
    sys.exit(main())
