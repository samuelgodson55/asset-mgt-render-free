# ErrorBeacon FastAPI Integration

The Asset Inventory Quotes integration is intentionally small:

1. Keep the application's existing `UnhandledExceptionMiddleware`.
2. Import `report_exception` from `integrations.fastapi_errorbeacon`.
3. Call it from the existing exception path.
4. For background/service failures, call `report_background_exception()`.

Do **not** add a second global exception middleware if the application already has one. The existing middleware owns the safe 500 response, request ID and CORS/security behavior; ErrorBeacon is an observability side effect.

The reporter is fire-and-forget and uses a short connection/read timeout. If ErrorBeacon is down, the application continues normally.

The same integration module also exposes `report_client_event()` for browser errors. The browser never receives the ErrorBeacon API key; it reports through the application's `/api/telemetry/client-error` endpoint.

## Keeping this file in sync

This file is a **reference copy**, not something imported at runtime by anything in this repo — Asset Inventory Quotes runs its own copy at `backend/integrations/fastapi_errorbeacon.py`. It exists so a *new* integrating application has a correct, current starting point to copy.

Env var names are deliberately namespaced (`ERRORBEACON_URL`, not `URL`) so a copy-pasted integration won't collide with an existing app's globals — that difference from `backend/integrations/fastapi_errorbeacon.py` is intentional and doesn't need to change.

Everything else — function signatures, parameters, and what gets forwarded to ErrorBeacon — should track `backend/integrations/fastapi_errorbeacon.py`. If you change that file's behavior (not just its env-var names), port the same change here, or a team that copies this reference later will reintroduce a bug that's already been fixed once. `backend/tests/test_chaos_contract.py::test_errorbeacon_reference_integration_stays_in_sync` asserts the two files' `report_client_event()` signatures match; keep it passing.
