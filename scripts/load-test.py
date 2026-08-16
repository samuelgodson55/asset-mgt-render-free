#!/usr/bin/env python3
"""Small dependency-free HTTP load/latency test for P2 validation.

Examples:
  python scripts/load-test.py --url http://localhost:8080/healthz --requests 500 --concurrency 25
  python scripts/load-test.py --base-url http://localhost:8080 --login-email user@example.com --login-password '...' --path /api/assets?limit=25

This intentionally uses only the Python standard library so it can run on a
VM, CI runner, or production jump box without installing a load-test stack.
It reports p50/p95/p99, throughput, error rate, and HTTP status distribution.
Keep-alive is enabled by default with one persistent connection per worker.
Responses are fully drained before connection reuse so large response bodies do
not poison the next request. Use --no-keep-alive only for diagnostic comparison.
Use k6/Locust later for sustained, large-scale benchmarking; this is the
repeatable smoke/load gate for this app.
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPBasicAuthHandler


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, max(0, round((p / 100) * (len(values) - 1))))
    return values[index]


def login(base_url: str, email: str, password: str) -> str:
    jar = CookieJar()
    opener = build_opener(__import__("urllib.request", fromlist=["HTTPCookieProcessor"]).HTTPCookieProcessor(jar))
    payload = json.dumps({"identifier": email, "password": password}).encode()
    req = Request(
        urljoin(base_url.rstrip("/") + "/", "api/auth/login"),
        data=payload,
        headers={"Content-Type": "application/json", "Connection": "keep-alive"},
    )
    with opener.open(req, timeout=15) as response:
        if response.status >= 300:
            raise RuntimeError(f"login returned HTTP {response.status}")
    return "; ".join(f"{c.name}={c.value}" for c in jar)


class PersistentHTTPClient:
    """Small per-thread HTTP/1.1 client so repeated requests reuse TCP connections.

    urllib.request creates a new HTTPConnection for each opener.open() call, which
    makes a high-concurrency localhost -> Docker test measure connection setup
    overhead rather than normal keep-alive request latency. Each worker thread gets
    one connection and reconnects only when the peer has closed a stale connection.
    """

    def __init__(self, target: str, timeout: float) -> None:
        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"unsupported URL: {target}")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None

    def _connect(self) -> None:
        self.close()
        if self.scheme == "https":
            self.connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout)
        else:
            self.connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def get(self, headers: dict[str, str]) -> int:
        if self.connection is None:
            self._connect()

        try:
            return self._request_once(headers)
        except (
            http.client.CannotSendRequest,
            http.client.ResponseNotReady,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
        ):
            # A keep-alive connection can become stale, or may still contain
            # unread response bytes from a previous request. Reconnect once and
            # retry the request. Only transport-level failures are retried;
            # HTTP 4xx/5xx responses are returned to the caller.
            self._connect()
            return self._request_once(headers)

    def _request_once(self, headers: dict[str, str]) -> int:
        if self.connection is None:
            self._connect()
        assert self.connection is not None
        self.connection.request("GET", self.path, headers={**headers, "Connection": "keep-alive"})
        response = self.connection.getresponse()
        status = response.status
        # Drain the COMPLETE response before reusing the HTTP/1.1 connection.
        # Reading only a fixed prefix leaves unread bytes in the socket and can
        # cause the next request on the same connection to fail with
        # CannotSendRequest/ResponseNotReady.
        response.read()
        return status

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None


_thread_local = threading.local()


def get_client(target: str, timeout: float) -> PersistentHTTPClient:
    client = getattr(_thread_local, "http_client", None)
    if client is None or getattr(client, "target", None) != target or client.timeout != timeout:
        client = PersistentHTTPClient(target, timeout)
        client.target = target
        _thread_local.http_client = client
    return client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Full URL to load-test; e.g. http://localhost:8080/healthz")
    parser.add_argument("--base-url", help="Base URL used with --path")
    parser.add_argument("--path", default="/healthz")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--login-email")
    parser.add_argument("--login-password")
    parser.add_argument(
        "--expected-status",
        type=int,
        default=200,
        help="HTTP status expected for every request (default: 200).",
    )
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--max-p95-ms", type=float, default=1000.0)
    parser.add_argument(
        "--no-keep-alive",
        action="store_true",
        help="Disable HTTP keep-alive and open a fresh connection for each request (diagnostic mode).",
    )
    args = parser.parse_args()

    if args.url:
        target = args.url
        base_url = target.rsplit("/api/", 1)[0] if "/api/" in target else target.rsplit("/", 1)[0]
    else:
        if not args.base_url:
            parser.error("provide --url or --base-url")
        target = urljoin(args.base_url.rstrip("/") + "/", args.path.lstrip("/"))
        base_url = args.base_url

    cookie = None
    if args.login_email or args.login_password:
        if not (args.login_email and args.login_password):
            parser.error("--login-email and --login-password must be supplied together")
        cookie = login(base_url, args.login_email, args.login_password)

    semaphore = threading.BoundedSemaphore(args.concurrency)
    latencies: list[float] = []
    failures: list[str] = []
    status_counts: dict[int, int] = {}
    lock = threading.Lock()

    def one(i: int) -> None:
        with semaphore:
            started = time.perf_counter()
            try:
                headers = {"Accept": "application/json"}
                if cookie:
                    headers["Cookie"] = cookie
                if args.no_keep_alive:
                    req = Request(target, headers={**headers, "Connection": "close"}, method="GET")
                    with build_opener().open(req, timeout=args.timeout) as response:
                        status = response.status
                        response.read()
                else:
                    status = get_client(target, args.timeout).get(headers)
                elapsed = (time.perf_counter() - started) * 1000
                with lock:
                    latencies.append(elapsed)
                    status_counts[status] = status_counts.get(status, 0) + 1
                    if status != args.expected_status:
                        failures.append(
                            f"#{i}: HTTP {status} (expected {args.expected_status})"
                        )
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                with lock:
                    failures.append(f"#{i}: {exc}")
            except Exception as exc:  # pragma: no cover - defensive load-runner guard
                with lock:
                    failures.append(f"#{i}: {type(exc).__name__}: {exc}")

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one, i) for i in range(args.requests)]
        for future in as_completed(futures):
            future.result()
    elapsed_s = time.perf_counter() - started

    total = args.requests
    errors = len(failures)
    error_rate = (errors / total) * 100 if total else 0
    throughput = total / elapsed_s if elapsed_s else 0
    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)

    print(json.dumps({
        "url": target,
        "requests": total,
        "concurrency": args.concurrency,
        "expected_status": args.expected_status,
        "status_counts": {str(code): count for code, count in sorted(status_counts.items())},
        "duration_s": round(elapsed_s, 3),
        "throughput_rps": round(throughput, 2),
        "errors": errors,
        "error_rate_pct": round(error_rate, 3),
        "latency_ms": {"p50": round(p50, 2), "p95": round(p95, 2), "p99": round(p99, 2)},
    }, indent=2))

    if failures:
        print("First failures:", file=sys.stderr)
        for item in failures[:10]:
            print(f"  {item}", file=sys.stderr)

    if error_rate > args.max_error_rate:
        return 2
    if p95 > args.max_p95_ms:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
