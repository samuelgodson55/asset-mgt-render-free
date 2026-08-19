# OpenTelemetry deployment and access guide

OpenTelemetry is an opt-in observability feature. **Everything is controlled
by `OTEL_ENABLED`.**

- `OTEL_ENABLED=false` (default): backend, worker, beat, and browser tracing
  are disabled.
- `OTEL_ENABLED=true`: the backend tracing stack is enabled and the React
  browser can participate in the same distributed traces.
- Browser tracing never receives `OTEL_EXPORTER_OTLP_HEADERS`; secrets remain
  server-side.
- Telemetry failures are isolated and must not make normal application
  requests fail.

## Contents

- [What a trace contains](#what-a-trace-contains)
  - [Browser telemetry security boundary](#browser-telemetry-security-boundary)
  - [Sampling contract](#sampling-contract)
  - [Finding application errors quickly](#finding-application-errors-quickly)
  - [Correlating with ErrorBeacon via request ID](#correlating-with-errorbeacon-via-request-id)
  - [Jaeger UX shortcuts](#jaeger-ux-shortcuts)
- [Business operation naming](#business-operation-naming)
- [Deployment matrix](#deployment-matrix)
  - [1. Local Docker Compose + Jaeger](#1-local-docker-compose--jaeger)
  - [2. Terraform-managed Azure VM + Jaeger](#2-terraform-managed-azure-vm--jaeger)
  - [3. Azure Container Apps](#3-azure-container-apps)
  - [4. External OTLP collector](#4-external-otlp-collector)
- [Telemetry helper safety contract](#telemetry-helper-safety-contract)
- [Troubleshooting](#troubleshooting)
  - [Jaeger UI is empty on the VM](#jaeger-ui-is-empty-on-the-vm)
  - [Browser spans are missing but backend spans exist](#browser-spans-are-missing-but-backend-spans-exist)
- [Security rule](#security-rule)

### One command in both local and VM environments

`./scripts/telemetry.sh` now detects its deployment target automatically. Run the
same command from the repository root locally or from `/opt/snipeit` on the
Terraform VM; no `local`/`vm` suffix is required. The VM deployment workflow
copies this script into `/opt/snipeit/scripts/` and preserves it across deploys.

The script only starts/stops the Jaeger service. It never runs a stack-wide
`docker compose down`, never recreates the database/Redis/application services,
and never changes the active blue/green slot.

## What a trace contains

Important user workflows use three semantic layers so the Jaeger waterfall
answers both **what the user intended** and **what the application actually
did**:

```text
Browser: ui.click.checkout
  └── Frontend: checkout.complete
        └── Browser HTTP: POST /api/assets/:id/checkout_advanced
              └── Backend: checkout.complete
                    ├── PostgreSQL
                    ├── Redis
                    └── Celery producer
                          └── Worker: Celery task
                                ├── PostgreSQL
                                └── Redis
```

The browser uses W3C `traceparent` propagation. The API layer creates stable
business-operation spans for important mutations, while infrastructure
instrumentation remains automatic. SQLAlchemy, Redis, FastAPI, and Celery
spans are not manually duplicated around individual queries/commands.

The browser bridge is intentionally dependency-free and only instruments
same-origin API calls made through `frontend-app/src/lib/api.ts`. Important
buttons opt into UI spans with `data-otel-action`; arbitrary buttons and
visible DOM text are not automatically traced.

### Browser telemetry security boundary

`POST /api/telemetry/traces` is a same-origin server-side proxy. It does **not**
blindly forward arbitrary OTLP supplied by the browser. Before forwarding, the
backend:

- fixes the resource identity to `snipeit-lite-frontend`
- allows only the supported frontend span kinds and valid trace/span IDs
- allows only approved operation/UI/HTTP/status attributes
- removes OTLP events and links
- removes arbitrary resource/scope data
- rejects unsafe names and malformed payloads
- bounds the body and span count
- never accepts exporter authentication headers from the client

The backend then adds `OTEL_EXPORTER_OTLP_HEADERS` itself when forwarding to
the collector. This means a user inspecting browser traffic can see ordinary
trace context such as `traceparent`, but cannot see the collector's server-side
authentication credentials because those credentials are never sent to the
browser.

The browser never records:

- cookies or session credentials
- `Authorization` headers
- request/response bodies
- passwords
- MFA codes/recovery codes
- password-reset tokens
- query strings
- arbitrary form or DOM text
- exception messages/stacks
- OTLP exporter credentials

Browser URL paths are normalized before export (`:id` / `:file`) so the trace
does not unnecessarily copy dynamic identifiers or backup filenames.

### Sampling contract

The browser makes one sampling decision for an interaction/business trace.
If the UI interaction is sampled, its business span and child HTTP span use
the same trace. If a business operation starts without a sampled UI
interaction, it makes one root sampling decision and passes that decision to
its HTTP child. This prevents the old "click sampled, fetch not sampled"
partial-trace problem.

The backend uses a `ParentBased(TraceIdRatioBased(...))` sampler, so an incoming
sampled browser trace remains sampled through the FastAPI request and its
children.

### Finding application errors quickly

Jaeger v2's Search page uses the **Tags** field for span attributes; there is
not a separate `status = ERROR` search box. This project therefore adds a
safe, boolean `error=true` tag to failed application/business/HTTP spans.

The fastest error workflow is:

```text
Jaeger
  → Quick Investigations
  → Errors — Last 1 Hour
```

That shortcut opens the normal Search page with:

```text
error=true
```

as the tag filter. You can also enter the equivalent filter manually in the
**Tags** field:

```text
error=true
```

The filter is intentionally a tag, not a fake `errors` operation. This keeps
the operation list meaningful while still allowing one search to find failed
requests across the frontend, backend, and application business spans.

The trace itself should then be read from the failed span upward/downward:

```text
ui.click.checkout
  └── checkout.complete
        └── POST /api/assets/:id/checkout_advanced
              └── backend checkout.complete   error=true
                    ├── PostgreSQL
                    ├── Redis
                    └── Celery
```

A 4xx/5xx HTTP request is also tagged `error=true`. This means the error
shortcut intentionally includes authentication failures, validation failures,
not-found responses, and server errors; inspect the HTTP status and the
business span to distinguish an expected client-side rejection from an
application defect.

The browser only exports the boolean error marker and a safe exception type.
It never exports exception messages, stacks, request bodies, credentials,
cookies, query strings, or authorization headers.

### Correlating with ErrorBeacon via request ID

Every request gets an `X-Request-ID`, and `RequestContextMiddleware` stamps it
onto the active span as the tag `app.request_id`. ErrorBeacon alerts (backend
exceptions, Celery/startup errors, and browser-reported client errors) all
carry this same `request_id`.

Given a `request_id` from an alert, search Jaeger with:

```text
app.request_id=<id>
```

in the **Tags** field. This jumps directly to the trace for that request
without needing an approximate time range.

### Jaeger UX shortcuts

The local Jaeger instance is configured with a small **Quick Investigations**
menu:

- **Errors — Last 1 Hour** — searches `error=true` across all services.
- **Checkout Traces — Last 1 Hour** — opens `snipeit-lite-frontend` +
  `checkout.complete`.
- **Slow Traces — Over 1 Second** — finds traces above a useful latency
  threshold.
- **Recent Traces — Last 15 Minutes** — returns the recent working set.

The UI also shows shorter trace IDs, keeps critical-path visualization enabled,
and prioritizes `error`, `app.`, `http.`, `db.`, `rpc.`, and `exception.` tags in
span details. These are UI-only improvements; they do not change application
behavior or expose telemetry credentials.

## Business operation naming

Search Jaeger using stable operation names rather than generic DOM events.

Full set of allowed business operation names (also the server-side allow-list
for browser-submitted spans, `SAFE_BROWSER_OPERATION_NAMES`):

```text
asset.category.update, asset.create, asset.delete, asset.department.update,
asset.exception.flag, asset.exception.recall, asset.import, asset.name.update,
asset.price.update, asset.purge, asset.quantity.update, asset.restore,
audit.export.start,
auth.login, auth.logout, auth.mfa.recovery_codes.regenerate, auth.mfa.setup,
auth.mfa.verify, auth.password.forgot, auth.password.reset, auth.password.update,
backup.create, backup.delete, backup.restore, backup.restore.upload,
checkin.complete, checkout.complete, checkout.extend, checkout.extend.bulk,
checkout.extension.decide, checkout.extension.request,
maintenance.update,
outsider.convert_to_user, outsider.delete, outsider.update,
profile.update,
quote.approve, quote.assign, quote.checkout, quote.create, quote.delete,
quote.discount.update, quote.item.add, quote.item.remove, quote.item.update,
quote.my_item.add, quote.my_item.remove, quote.my_item.update,
quote.notifications.read, quote.outsourced_item.add, quote.outsourced_item.remove,
quote.paid, quote.submit, quote.update,
settings.digest.update, settings.vat.update,
user.convert_to_outsider, user.create, user.delete, user.password.reset,
user.purge, user.restore, user.update
```

UI spans are intentionally separate:

```text
ui.click.checkout
ui.click.checkin
ui.click.quote.approve
```

The UI span means "the user initiated this action"; the business span means
"the application attempted this operation". This distinction is useful when
a click occurs but the API request never starts, or when the backend operation
fails after the click succeeds.

## Deployment matrix

Telemetry has two separate concerns:

1. **Application instrumentation** — controlled by `OTEL_ENABLED` when the
   backend/worker/beat processes start.
2. **Telemetry infrastructure** — the optional local/VM Jaeger container.

The helper `scripts/telemetry.sh` manages **only Jaeger**. It never runs
`docker compose down`, never recreates the application stack, and never
changes the active blue/green slot on the Terraform VM.

This separation is intentional: stopping Jaeger must never stop the
application.

### 1. Local Docker Compose + Jaeger

For a local trace session, first make sure the application's `.env` contains:

```text
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318
```

Start the application normally:

```bash
docker compose up -d
```

Then start **only** Jaeger:

```bash
./scripts/telemetry.sh on
```

This is the safe replacement for manually remembering the Compose `tracing`
profile. The underlying `jaeger` service remains profile-gated; the helper
selects that profile explicitly and starts only that service.

Jaeger's UI is:

```text
http://localhost:16686
```

The local Jaeger v2 service mounts `jaeger/ui-config.json` read-only. The
configuration only customizes the Jaeger UI; it does not alter the collector,
OTLP authentication, trace storage, or application services.

When you are finished tracing:

```bash
./scripts/telemetry.sh off
```

That stops and removes **only** the Jaeger container. Backend, frontend,
PostgreSQL, Redis, worker, and beat are left running.

Check the telemetry state at any time:

```bash
./scripts/telemetry.sh status
```

View Jaeger logs:

```bash
./scripts/telemetry.sh logs
```

#### Important `OTEL_ENABLED` behavior

`OTEL_ENABLED` is a process-start gate in the current application
implementation. If the backend was started with `OTEL_ENABLED=false`, merely
starting Jaeger cannot make that already-running backend begin tracing.

The helper therefore refuses to start Jaeger when `.env` says
`OTEL_ENABLED=false`. This prevents a misleading "Jaeger is running but the
application is not tracing" state.

Likewise, `telemetry.sh off` deliberately does **not** restart the application.
If you want to fully disable instrumentation after a trace session:

1. Set `OTEL_ENABLED=false` in the deployment environment.
2. Use the deployment's normal, controlled application restart/redeploy path.
3. Then run `./scripts/telemetry.sh off` if Jaeger is still running.

For local development, a normal `docker compose up -d` after changing `.env`
is sufficient. The helper itself never performs that restart.

### 2. Terraform-managed Azure VM + Jaeger

The VM path follows the same separation, but Jaeger remains **SSH-only**.

Before the application is started/redeployed with tracing, configure the VM
environment:

```text
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318
```

Use the normal VM deployment/start path for the application. Once the active
application processes have tracing enabled, start Jaeger without touching the
active application slot:

```bash
./scripts/telemetry.sh on
```

The helper uses `docker-compose.vm.yml` and the existing `tracing` profile,
but explicitly targets only the `jaeger` service.

Check it:

```bash
./scripts/telemetry.sh status
```

When finished:

```bash
./scripts/telemetry.sh off
```

This does **not** run `docker compose down`, does **not** stop Caddy, and does
not stop either application slot. It only stops/removes Jaeger.

#### Open the Jaeger UI safely

From your workstation, create an SSH local port forward:

```bash
ssh -L 16686:127.0.0.1:16686 <ssh-user>@<vm-host>
```

Keep the SSH session open and open:

```text
http://localhost:16686
```

Or print the exact access instructions:

```bash
./scripts/telemetry.sh ui
```

The VM Compose file does not publish:

- `16686` — Jaeger UI
- `4317` — OTLP/gRPC
- `4318` — OTLP/HTTP

to the VM host/public interface. SSH is the supported UI access path.

### 3. Azure Container Apps

ACA does not run the local Jaeger container. Its production telemetry
destination is Azure Monitor/Application Insights when that path is enabled.

Use:

```text
OTEL_ENABLED=true
OTEL_AZURE_MONITOR_ENABLED=true
```

with the normal ACA deployment workflow.

When disabled:

```text
OTEL_ENABLED=false
```

No Jaeger container is involved.

### 4. External OTLP collector

For a deployment using an external collector:

```text
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=https://<collector>/v1/traces
OTEL_EXPORTER_OTLP_HEADERS=<server-side-authentication>
```

Do not put authentication headers into frontend build-time configuration.
The browser continues to use the application's same-origin telemetry proxy,
which adds server-side authentication.

## Telemetry helper safety contract

`scripts/telemetry.sh` is deliberately conservative:

```text
telemetry.sh on
    │
    ├── requires OTEL_ENABLED=true
    └── starts ONLY jaeger

telemetry.sh off
    │
    ├── stops ONLY jaeger
    └── removes ONLY jaeger

Application services
    ├── backend     untouched
    ├── frontend    untouched
    ├── postgres    untouched
    ├── redis       untouched
    ├── worker      untouched
    └── beat        untouched
```

The script never owns application deployment. That is especially important on
the Terraform VM, where application rollouts are controlled by the existing
blue/green deployment workflow.

## Troubleshooting

### Jaeger UI is empty on the VM

Check that the tracing profile is running:

```bash
./scripts/telemetry.sh status
```

Check backend logs:

```bash
docker compose -f docker-compose.vm.yml logs --tail 200 backend
```

Check that Jaeger is healthy:

```bash
./scripts/telemetry.sh logs
```

Confirm:

```text
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318
```

Then make sure the SSH tunnel is still open:

```bash
ssh -L 16686:127.0.0.1:16686 <ssh-user>@<vm-host>
```

and browse locally to:

```text
http://localhost:16686
```

### Browser spans are missing but backend spans exist

Check that:

1. `OTEL_ENABLED=true`.
2. The public configuration endpoint reports OTEL as enabled.
3. An OTLP/HTTP destination is configured.
4. The browser can reach the same-origin `/api/telemetry/traces` endpoint.
5. The backend can reach the configured OTLP/HTTP collector.

The browser must never need to resolve the Docker-only hostname
`jaeger`.

## Security rule

For the Terraform VM, **SSH is the only supported Jaeger UI access path**.
This is intentional. Jaeger is an observability component, not an
application authentication boundary.
