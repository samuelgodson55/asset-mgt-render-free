#!/usr/bin/env python3
"""CSV export smoke test for the Asset Inventory export (P1 production validation).

Exercises GET /api/assets/export end to end against a live deployment:

  login -> unauthenticated request rejected -> invalid format rejected
        -> category list -> "all categories" export -> row/column checks
        -> single-category export -> row/column checks -> no-leakage checks

Standard library only -- no dependencies to install on a jump box/CI runner.
Auth matches scripts/load-test.py and scripts/crud-smoke-test.py: POST
/api/auth/login sets an HttpOnly session cookie (there is no bearer token in
the response body to capture), so this script drives a CookieJar-backed
opener for every authenticated call, and a separate cookie-less opener for
the unauthenticated check.

/api/assets/export only requires get_current_user (any authenticated role),
unlike the CRUD smoke test's Super Admin/Admin-gated routes -- so this
script works against any test account. It does adapt its column-count
expectation to the account's role: Manager/Admin/Super Admin (or any
account with CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER on) get 8 columns
(...+ Available/Total/Status); Staff/Customer get the first 5.

Usage:
  python scripts/csv-export-test.py --base-url https://stack.multione.online --login-email r.adeyemi@corp.io --login-password 'Admin123!' --large-threshold 200

Exit code 0 = every step passed. Non-zero = see the FAIL line for which step
and why.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, build_opener, HTTPCookieProcessor

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"  # informational -- doesn't fail the run (e.g. dataset too small to be "large")

results: list[tuple[str, str, str]] = []  # (step, status, detail)


def record(step: str, status: str, detail: str) -> None:
    results.append((step, status, detail))
    marker = {"PASS": "\u2713", "FAIL": "\u2717", "WARN": "!"}[status]
    print(f"[{marker}] {step}: {detail}")


class Client:
    """Cookie-jar-backed client, mirroring scripts/crud-smoke-test.py's Client."""

    def __init__(self, base_url: str, use_cookies: bool = True) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(jar)) if use_cookies else build_opener()

    def _open(self, method: str, path: str, body: dict | None = None):
        url = urljoin(self.base_url, path.lstrip("/"))
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = Request(url, data=data, method=method, headers=headers)
        try:
            resp = self.opener.open(req, timeout=20)
            # resp.headers / e.headers are email.message.Message objects,
            # which are case-insensitive on both .get() and `in` -- do NOT
            # wrap them in dict(...), since HTTP header names are
            # case-insensitive but a plain dict's keys are not (this server
            # sends lowercase "content-type"/"content-disposition", which a
            # dict-wrapped lookup for "Content-Type" would silently miss).
            return resp.status, resp.headers, resp.read()
        except HTTPError as e:
            return e.code, e.headers, e.read()

    def request_json(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict, dict | str]:
        status, headers, raw = self._open(method, path, body)
        text = raw.decode(errors="replace")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = text
        return status, headers, parsed

    def request_raw(self, method: str, path: str) -> tuple[int, dict, bytes]:
        return self._open(method, path)


def expect(step: str, condition: bool, ok_detail: str, fail_detail: str) -> bool:
    record(step, PASS if condition else FAIL, ok_detail if condition else fail_detail)
    return condition


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--login-email", required=True)
    ap.add_argument("--login-password", required=True)
    ap.add_argument("--large-threshold", type=int, default=200, help="Row count above which the 'large-ish dataset' check is considered exercised rather than just informational.")
    args = ap.parse_args()

    auth = Client(args.base_url, use_cookies=True)
    anon = Client(args.base_url, use_cookies=False)

    # 1. Login
    status, headers, body = auth.request_json("POST", "/api/auth/login", {"identifier": args.login_email, "password": args.login_password})
    if not expect("login", status == 200, f"HTTP {status}", f"HTTP {status}: {body}"):
        print_summary()
        return 1
    role = None
    show_stock_expected = None
    me_status, _, me_body = auth.request_json("GET", "/api/auth/me")
    if me_status == 200 and isinstance(me_body, dict):
        role = me_body.get("role")

    # 2. Unauthenticated request -> 401 (no auth leakage)
    status, _, raw = anon.request_raw("GET", "/api/assets/export?format=csv")
    expect(
        "unauthenticated export rejected",
        status == 401,
        f"HTTP {status} as expected",
        f"expected 401, got HTTP {status}; body starts: {raw[:200]!r}",
    )

    # 3. Invalid format -> 400
    status, _, body = auth.request_json("GET", "/api/assets/export?format=xml")
    expect(
        "invalid format rejected",
        status == 400,
        f"HTTP {status} as expected",
        f"expected 400, got HTTP {status}: {body}",
    )

    # 4. Determine expected total row count + stock visibility via the list endpoint
    list_status, _, list_body = auth.request_json("GET", "/api/assets?limit=1&category=all")
    expected_total = None
    show_stock_expected = None
    if expect("baseline asset list", list_status == 200 and isinstance(list_body, dict), f"HTTP {list_status}, total={list_body.get('total') if isinstance(list_body, dict) else '?'}", f"HTTP {list_status}: {list_body}"):
        expected_total = list_body.get("total")
        show_stock_expected = list_body.get("show_stock")

    # 5. "All categories" CSV export
    status, headers, raw = auth.request_raw("GET", "/api/assets/export?format=csv&category=all")
    if not expect("export all-categories: HTTP 200", status == 200, "HTTP 200", f"HTTP {status}: {raw[:300]!r}"):
        print_summary()
        return 1

    content_type = headers.get("Content-Type", "")
    expect(
        "export all-categories: Content-Type",
        content_type.split(";")[0].strip() == "text/csv",
        f"Content-Type: {content_type}",
        f"unexpected Content-Type: {content_type!r}",
    )

    disposition = headers.get("Content-Disposition", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    expect(
        "export all-categories: Content-Disposition",
        disposition.startswith("attachment;") and "asset_inventory_all_" in disposition and disposition.strip().endswith(".csv"),
        f"Content-Disposition: {disposition}",
        f"unexpected Content-Disposition: {disposition!r} (expected attachment; filename=asset_inventory_all_{today}.csv)",
    )

    # 6. Valid CSV structure + expected headers
    try:
        text = raw.decode("utf-8")
        reader = csv.reader(io.StringIO(text))
        csv_rows = list(reader)
        parse_ok = True
        parse_detail = f"{len(csv_rows)} row(s) including header, decoded as UTF-8"
    except (UnicodeDecodeError, csv.Error) as e:
        parse_ok = False
        csv_rows = []
        parse_detail = f"failed to parse: {e}"
    expect("export all-categories: valid CSV", parse_ok, parse_detail, parse_detail)

    if parse_ok and csv_rows:
        header_row = csv_rows[0]
        data_rows = csv_rows[1:]
        full_headers = ["Pool ID", "Asset Name", "Category", "Department", "Price", "Available", "Total", "Status"]
        expected_headers = full_headers if show_stock_expected else full_headers[:5]
        expect(
            "export all-categories: header row",
            header_row == expected_headers,
            f"headers match ({'with' if show_stock_expected else 'without'} stock columns, role={role}): {header_row}",
            f"headers {header_row} != expected {expected_headers} (show_stock={show_stock_expected}, role={role})",
        )

        if expected_total is not None:
            expect(
                "export all-categories: row count matches inventory total",
                len(data_rows) == expected_total,
                f"{len(data_rows)} data row(s), matches /api/assets total={expected_total}",
                f"{len(data_rows)} data row(s) != /api/assets total={expected_total}",
            )

        if len(data_rows) >= args.large_threshold:
            record("export all-categories: large-ish dataset", PASS, f"{len(data_rows)} rows served without error (>= {args.large_threshold})")
        else:
            record("export all-categories: large-ish dataset", WARN, f"only {len(data_rows)} rows in this environment (< {args.large_threshold}); export itself succeeded but a truly large dataset wasn't exercised")

        # 7. No auth/secret leakage in the export body or headers
        secrets_to_check = [args.login_password]
        leaked = [s for s in secrets_to_check if any(s in cell for row in csv_rows for cell in row)]
        expect(
            "export: no credential leakage in CSV body",
            not leaked,
            "login password/token not found in any cell",
            f"found sensitive value(s) echoed in CSV cells: {leaked}",
        )
        expect(
            "export: no Set-Cookie on a plain GET",
            "Set-Cookie" not in headers,
            "no Set-Cookie header on the export response",
            f"unexpected Set-Cookie on export response: {headers.get('Set-Cookie')!r}",
        )

    # 8. Single-category export, if any category exists
    cats_status, _, cats_body = auth.request_json("GET", "/api/assets/categories")
    category_list = cats_body.get("categories") if cats_status == 200 and isinstance(cats_body, dict) else None
    if not category_list and isinstance(cats_body, list):
        category_list = cats_body
    if cats_status == 200 and category_list:
        target_category = category_list[0]
        cat_list_status, _, cat_list_body = auth.request_json("GET", f"/api/assets?limit=1&category={target_category}")
        expected_cat_total = cat_list_body.get("total") if cat_list_status == 200 and isinstance(cat_list_body, dict) else None

        status, headers, raw = auth.request_raw("GET", f"/api/assets/export?format=csv&category={target_category}")
        if expect(f"export category={target_category!r}: HTTP 200", status == 200, "HTTP 200", f"HTTP {status}: {raw[:300]!r}"):
            try:
                cat_rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
                cat_data_rows = cat_rows[1:]
                all_match = all(row[2].strip().lower() == target_category.strip().lower() for row in cat_data_rows if len(row) > 2)
                expect(
                    f"export category={target_category!r}: rows scoped correctly",
                    all_match,
                    f"all {len(cat_data_rows)} row(s) have Category == {target_category!r}",
                    "found row(s) outside the requested category -- category filter is leaking other pools",
                )
                if expected_cat_total is not None:
                    expect(
                        f"export category={target_category!r}: row count matches filtered total",
                        len(cat_data_rows) == expected_cat_total,
                        f"{len(cat_data_rows)} data row(s), matches filtered total={expected_cat_total}",
                        f"{len(cat_data_rows)} data row(s) != filtered total={expected_cat_total}",
                    )
            except (UnicodeDecodeError, csv.Error) as e:
                record(f"export category={target_category!r}: valid CSV", FAIL, f"failed to parse: {e}")
    else:
        record("export by category", WARN, "no categories present in this environment -- category-scoped export not exercised")

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
