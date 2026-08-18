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

## What a trace contains

When browser tracing is enabled, a meaningful UI action can form one trace:

```text
Browser UI action
  └── Browser fetch /api/...
        └── FastAPI request
              ├── SQLAlchemy / PostgreSQL spans
              ├── Redis spans
              └── Celery task, when one is queued
```

The browser uses W3C `traceparent` propagation and sends its spans through
`POST /api/telemetry/traces`. The backend forwards them to the configured
OTLP/HTTP destination.

The browser intentionally does not record passwords, cookies, authorization
headers, request/response bodies, query strings, or arbitrary visible form
text.

## Deployment matrix

### 1. Local Docker Compose + Jaeger

Use the tracing profile:

```bash
OTEL_ENABLED=true docker compose --profile tracing up -d
```

The default OTLP/HTTP destination is:

```text
http://jaeger:4318
```

Jaeger's UI is bound for local development at:

```text
http://localhost:16686
```

No Azure resources are required.

### 2. Terraform-managed Azure VM + Jaeger

The VM path is intentionally **SSH-only for Jaeger UI access**.

`docker-compose.vm.yml` does not publish Jaeger ports to the VM host:

- `16686` — UI: not publicly published
- `4317` — OTLP/gRPC: internal Docker network only
- `4318` — OTLP/HTTP: internal Docker network only

Enable tracing in the VM environment:

```text
OTEL_ENABLED=true
```

Then start the opt-in tracing profile:

```bash
docker compose -f docker-compose.vm.yml --profile tracing up -d
```

Backend/worker/beat containers use the internal:

```text
http://jaeger:4318
```

#### Open the Jaeger UI safely

From your workstation, create an SSH local port forward:

```bash
ssh -L 16686:127.0.0.1:16686 <ssh-user>@<vm-host>
```

Keep the SSH session open and open:

```text
http://localhost:16686
```

The traffic path is:

```text
Your browser
    │
    │ localhost:16686
    ▼
SSH encrypted tunnel
    │
    ▼
VM 127.0.0.1:16686
    │
    ▼
Jaeger container :16686
```

**Do not expose port 16686 through the Azure NSG. Do not publish
`16686:16686` in the VM Compose file. Do not publish 4317/4318 to the public
VM interface.**

If the VM is accessed through an SSH-capable access mechanism, use its normal
SSH hostname in the command above. The remote endpoint should still be
`127.0.0.1:16686`.

Close the SSH session when finished.

### 3. Azure Container Apps + Application Insights

For the ACA path, use Azure Monitor/Application Insights rather than running a
public Jaeger UI.

Enable:

```text
OTEL_ENABLED=true
OTEL_AZURE_MONITOR_ENABLED=true
```

The deployment workflow provisions/wires the Application Insights connection
string according to the existing infrastructure configuration.

View traces in Azure Portal:

1. Open the Application Insights resource.
2. Use **Investigate → Transaction search**.
3. Use **Application Map** for service relationships.
4. Use **Performance** for slow operations.
5. Use the Log Analytics **Logs** blade for trace-oriented queries.

Browser tracing requires an OTLP/HTTP destination because the browser proxy
uses OTLP/HTTP JSON. If Application Insights is configured only through its
connection string and no OTLP/HTTP endpoint is available for the browser,
backend tracing can still work while browser spans remain disabled.

### 4. External OTLP/HTTP collector

You can point the backend at another OTLP/HTTP-compatible collector:

```text
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=https://<collector>/v1/traces
OTEL_EXPORTER_OTLP_HEADERS=<server-side-authentication>
```

Examples include a self-hosted OpenTelemetry Collector, Grafana Cloud, or
Honeycomb.

Keep exporter credentials in server-side environment/secrets. Never put them
in frontend build-time variables.

## Sampling

`OTEL_TRACES_SAMPLE_RATIO` controls the amount of tracing data collected.
Start with `1.0` while validating locally, then reduce it if production
volume warrants it.

Browser tracing follows the same setting exposed through the non-secret public
configuration.

## Troubleshooting

### Jaeger UI is empty on the VM

Check that the tracing profile is running:

```bash
docker compose -f docker-compose.vm.yml --profile tracing ps
```

Check backend logs:

```bash
docker compose -f docker-compose.vm.yml logs --tail 200 backend
```

Check that Jaeger is healthy:

```bash
docker compose -f docker-compose.vm.yml --profile tracing logs --tail 200 jaeger
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
