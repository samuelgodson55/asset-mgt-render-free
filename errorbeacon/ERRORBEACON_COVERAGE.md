# ErrorBeacon Coverage and Error-Handling Map

This document records the system-wide scan of the Asset Inventory Quotes application and explains where ErrorBeacon is integrated.

## Table of contents

1. [What was scanned](#what-was-scanned)
2. [Coverage already present](#coverage-already-present)
3. [New ErrorBeacon capture points](#new-errorbeacon-capture-points)
4. [Expected errors intentionally not paged](#expected-errors-intentionally-not-paged)
5. [Background and infrastructure failures](#background-and-infrastructure-failures)
6. [Frontend failures](#frontend-failures)
7. [What Telegram receives](#what-telegram-receives)
8. [Why this is fast](#why-this-is-fast)

## What was scanned

The scan covered:

- FastAPI middleware and application startup/shutdown
- API routers
- Services
- Database/session lifecycle
- Celery worker and scheduled tasks
- Authentication and MFA paths
- Backup/restore paths
- Notifications and third-party integrations
- Redis rate limiting
- Legacy JavaScript frontend
- React frontend
- Docker Compose local and VM deployment files
- ACA Bicep and deployment workflows

The repository contains many intentional `HTTPException(4xx)` paths. Those are normal business/validation outcomes and should not generate production incident pages.

## Coverage already present

The application already had several good protections before ErrorBeacon:

- A global pure-ASGI unhandled exception middleware
- Request IDs/correlation IDs
- Structured logging
- Database readiness checks
- Explicit rollback around transactional operations
- Backup/restore error handling
- Celery task isolation
- Redis rate limiting with fail-open behavior
- Frontend API error parsing
- OpenTelemetry instrumentation

ErrorBeacon builds on those controls instead of replacing them.

## New ErrorBeacon capture points

### 1. Global HTTP exceptions

`backend/middleware/error_handling.py` reports every unhandled HTTP exception with:

- request ID
- method
- path
- exception type
- message
- traceback
- release
- host

The original exception is still re-raised where required, and the existing safe JSON 500 response remains unchanged.

### 2. Explicit unexpected service failures

High-value broad catches in asset checkout/import and quotation fulfillment now report their exceptions before returning the existing safe 500 response.

### 3. Backup and restore API failures

Backup/restore API failures are reported while preserving the existing HTTP error messages and status codes.

### 4. Startup and readiness failures

Database startup failures and readiness failures are reported. Readiness polling remains deliberately non-paging when it repeats every few seconds, but the monitor groups/deduplicates the incident.

### 5. Celery failures

A Celery `task_failure` signal captures unhandled failures from scheduled/background tasks without requiring every task to be manually wrapped.

This covers:

- audit exports
- notification jobs
- SLA escalation jobs
- audit partition jobs
- future Celery tasks added to the application

### 6. Redis/rate-limit degradation

Redis failures in the rate limiter are still fail-open, but ErrorBeacon records them as warning-level infrastructure degradation rather than allowing them to disappear into logs.

### 7. Browser/runtime errors

Both frontend implementations now report:

- `window.error`
- unhandled promise rejections
- HTTP 5xx responses from the central API wrapper
- API network failures

The browser never receives the ErrorBeacon API key. It posts to the application's own `/api/telemetry/client-error` endpoint, which forwards the event server-to-server.

A small per-IP/browser-event limit prevents this endpoint from becoming an alert flood vector.

## Expected errors intentionally not paged

These remain normal application behavior and are not automatically treated as production incidents:

- 400 validation failures
- 401 authentication failures
- 403 permission failures
- 404 missing resources
- 409 expected concurrency/state conflicts
- expected CSV row validation errors
- invalid MFA/password attempts
- normal user-facing business-rule rejections

This distinction is important. ErrorBeacon should report **unexpected failures**, not every rejected request.

## Background and infrastructure failures

The monitor can now distinguish application errors from operational failures using `component`, `operation`, and `category` fields.

Examples:

```text
component=celery
operation=tasks.send_email_task
category=background_task
```

or:

```text
component=database
operation=readiness_check
```

or:

```text
component=frontend
category=client_error
```

## Frontend failures

There are two frontend implementations in the repository:

- the existing legacy JavaScript frontend
- the React `frontend-app`

Both have the same ErrorBeacon browser bridge.

The frontend integration deliberately does not send stack traces containing authorization headers, cookies, or application secrets. Browser-side telemetry is treated as diagnostic data, not as a privileged monitoring credential.

## What Telegram receives

A new incident can include:

- severity
- application
- environment
- exception type
- message
- endpoint
- component
- operation
- request ID
- release/image tag
- occurrence count
- traceback

It can also be marked with:

```text
🔥 ERROR SPIKE DETECTED
```

or:

```text
⚠️ POSSIBLE DEPLOYMENT REGRESSION
```

AI analysis is deliberately sent as a **second message**. The first alert is deterministic and immediate, so Gemini latency never delays the initial notification.

## Why this is fast

The monitored application uses a fire-and-forget reporter:

```text
Exception
   │
   ├── normal application error handling continues
   │
   └── tiny background HTTP report
            │
            ▼
       ErrorBeacon
            │
            ├── persist/group
            ├── Telegram immediately
            └── Gemini asynchronously
```

The application does not wait for Telegram, Gemini, or ErrorBeacon database work.

ErrorBeacon itself also does not wait for Telegram or Gemini before acknowledging the event.

## Result

The application now has layered observability:

```text
                 Production Application
                           │
          ┌────────────────┼─────────────────┐
          │                │                 │
       Logs/OTel      ErrorBeacon        Azure Monitor
          │                │                 │
       detailed        incidents          platform
       telemetry       + Telegram         telemetry
                           │
                     fast human alert
```

Azure Monitor/Application Insights remains useful for broad infrastructure and telemetry analysis. ErrorBeacon is optimized for the much shorter path from **unexpected application failure → actionable Telegram incident**.


## Request ID coverage

HTTP exceptions from the FastAPI application preserve the generated request correlation ID. `RequestContextMiddleware` stores the ID in `request_id_var`; ErrorBeacon uses that context value instead of relying only on the incoming `X-Request-ID` header. Synthetic ErrorBeacon test alerts generate their own test request ID so the Telegram path can be visually verified. Background jobs that have no originating HTTP request intentionally have no request ID unless the job explicitly carries one.
