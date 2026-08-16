#!/usr/bin/env python3
"""Regression tests for scripts/load-test.py.

These tests use only the Python standard library and exercise the keep-alive
connection handling that previously failed when response bodies exceeded 4 KiB.
"""

from __future__ import annotations

import http.server
import importlib.util
import os
import threading
import unittest


SCRIPT = os.path.join(os.path.dirname(__file__), "load-test.py")


def load_module():
    spec = importlib.util.spec_from_file_location("load_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load load-test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


load_test = load_module()


class LargeBodyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    body = b"x" * 8192

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(self.body)
        self.wfile.flush()

    def log_message(self, *_args):
        pass


class StatusHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, *_args):
        pass


class LoadTestRunnerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.server = None
        self.thread = None

    def start_server(self, handler):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/"

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def test_keep_alive_drains_large_response_before_reuse(self):
        target = self.start_server(LargeBodyHandler)
        client = load_test.PersistentHTTPClient(target, timeout=5)

        try:
            self.assertEqual(client.get({"Accept": "application/json"}), 200)
            self.assertEqual(client.get({"Accept": "application/json"}), 200)
        finally:
            client.close()

    def test_http_error_status_is_returned_for_caller_to_classify(self):
        target = self.start_server(StatusHandler)
        client = load_test.PersistentHTTPClient(target, timeout=5)

        try:
            self.assertEqual(client.get({"Accept": "application/json"}), 401)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
