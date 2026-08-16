# ErrorBeacon FastAPI Integration

The Asset Inventory Quotes integration is intentionally small:

1. Keep the application's existing `UnhandledExceptionMiddleware`.
2. Import `report_exception` from `integrations.fastapi_errorbeacon`.
3. Call it from the existing exception path.
4. For background/service failures, call `report_background_exception()`.

Do **not** add a second global exception middleware if the application already has one. The existing middleware owns the safe 500 response, request ID and CORS/security behavior; ErrorBeacon is an observability side effect.

The reporter is fire-and-forget and uses a short connection/read timeout. If ErrorBeacon is down, the application continues normally.

The same integration module also exposes `report_client_event()` for browser errors. The browser never receives the ErrorBeacon API key; it reports through the application's `/api/telemetry/client-error` endpoint.
