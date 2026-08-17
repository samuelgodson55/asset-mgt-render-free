# ErrorBeacon security controls

- Production VM and ACA ErrorBeacon instances are internal-only in the deployment configuration.
- `ERRORBEACON_INGEST_API_KEY` is accepted only by `POST /v1/events`.
- `ERRORBEACON_ADMIN_API_KEY` protects incident reads, statistics, health diagnostics and management/test endpoints.
- Public `GET /healthz` is intentionally minimal. Detailed diagnostics are under admin-protected `GET /v1/health`.
- Swagger/ReDoc/OpenAPI are disabled in production by default with `ERRORBEACON_ENABLE_DOCS=false`.
- Failed admin-key attempts are rate limited per client IP with `ERRORBEACON_ADMIN_AUTH_FAILURES_PER_MINUTE` (default 10/minute).
- Request bodies are capped by `ERRORBEACON_MAX_REQUEST_BODY_BYTES` (default 131072 bytes).
- Browser telemetry is additionally capped at the nginx layer and validates context depth, item count and serialized size.
- Browser telemetry is forwarded server-to-server; no ErrorBeacon API key is sent to the browser.
