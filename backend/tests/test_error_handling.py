"""
tests/test_error_handling.py
------------------------------
Covers main.py's global `@app.exception_handler(Exception)` -- the
last-resort safety net for genuinely unhandled exceptions (see that
handler's own docstring/comment block in main.py for the full "why").

These tests deliberately force a real, unanticipated exception (via
monkeypatch, not a code path that already has its own try/except) to
prove three things end-to-end, not just "the code doesn't raise a
NameError when it runs":
  1. The caller gets back a 500 with the SAME `{"detail": ...}` JSON
     shape every other error in this API uses -- not a bare plain-text
     body.
  2. That JSON body carries a `request_id`, and it's the exact same
     value as the `X-Request-ID` response header set by
     RequestContextMiddleware -- so a user/support agent can actually
     correlate "the error I saw" with "the matching log line(s)".
  3. CORS headers are still present on the error response -- proving the
     handler fires from inside CORSMiddleware's layer, not past it (see
     main.py's comment for why that ordering matters: without a
     registered handler, a genuinely unhandled exception previously blew
     straight past CORSMiddleware to Starlette's default
     ServerErrorMiddleware, which never adds CORS headers at all).
"""

import services.asset_service as asset_service


def test_unhandled_exception_returns_structured_500_with_request_id(client, monkeypatch):
    """Force a route to raise a plain, unanticipated exception (nothing in
    this API's own code catches a bare RuntimeError from
    list_asset_categories) and confirm the global handler -- not
    Starlette's default -- is what answers."""

    def _boom(db):
        raise RuntimeError("simulated unexpected failure -- category lookup exploded")

    monkeypatch.setattr(asset_service, "list_asset_categories", _boom)

    from tests.conftest import auth_headers
    headers = auth_headers(client, "r.adeyemi@corp.io", "Admin123!")

    response = client.get("/api/assets/categories", headers=headers)

    assert response.status_code == 500
    body = response.json()

    # Same {"detail": ...} shape as every other error in this API --
    # never the raw exception message (that would leak internals).
    assert "detail" in body
    assert "simulated unexpected failure" not in body["detail"]
    assert "RuntimeError" not in body["detail"]

    # The response body's request_id must match the X-Request-ID header
    # RequestContextMiddleware stamped on this exact response -- that's
    # what makes the error traceable back to a specific log line.
    assert body["request_id"]
    assert body["request_id"] == response.headers["x-request-id"]

    # CORS headers must still be present -- proves the handler runs
    # INSIDE CORSMiddleware's layer rather than bypassing it (see
    # main.py's comment for the full "why this matters" explanation).
    cors_response = client.get(
        "/api/assets/categories",
        headers={**headers, "Origin": "http://localhost:8080"},
    )
    assert cors_response.status_code == 500
    assert cors_response.headers.get("access-control-allow-origin") == "http://localhost:8080"


def test_unhandled_exception_is_logged_with_traceback(client, monkeypatch, caplog):
    """The full traceback must actually reach the logging system (not get
    swallowed) -- that's what makes the failure debuggable after the
    fact, not just "handled" from the caller's point of view."""
    import logging

    def _boom(db):
        raise RuntimeError("simulated unexpected failure for log assertion")

    monkeypatch.setattr(asset_service, "list_asset_categories", _boom)

    from tests.conftest import auth_headers
    headers = auth_headers(client, "r.adeyemi@corp.io", "Admin123!")

    with caplog.at_level(logging.ERROR, logger="main"):
        response = client.get("/api/assets/categories", headers=headers)

    assert response.status_code == 500
    assert any(
        record.exc_info and "simulated unexpected failure for log assertion" in str(record.exc_info[1])
        for record in caplog.records
    )


def test_normal_http_exceptions_are_unaffected(client):
    """Sanity check: ordinary `raise HTTPException(...)` error paths
    (e.g. 401 for no auth) must keep working exactly as before -- the new
    global handler only registered for the bare `Exception` type, and
    FastAPI dispatches the more specific HTTPException handler first, so
    routine 4xx responses shouldn't gain a request_id field or otherwise
    change shape."""
    response = client.get("/api/assets/categories")
    assert response.status_code == 401
    assert "detail" in response.json()
