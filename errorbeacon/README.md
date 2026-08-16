# ErrorBeacon Lite

ErrorBeacon is an isolated application incident monitor for FastAPI/web applications. It is optimized for one job: **exception → grouping → immediate Telegram alert → optional AI diagnosis** without making the monitored application wait.

## Table of Contents

1. [Architecture](#architecture)
2. [What ErrorBeacon captures](#what-errorbeacon-captures)
3. [Performance and reliability](#performance-and-reliability)
4. [Generate the API key](#generate-the-api-key)
5. [Configure Telegram](#configure-telegram)
6. [Run locally](#run-locally)
7. [Connect the Asset Inventory Quotes application](#connect-the-asset-inventory-quotes-application)
8. [API](#api)
9. [Telegram controls](#telegram-controls)
10. [Security and redaction](#security-and-redaction)
11. [Deployment](#deployment)
12. [Files](#files)

## Architecture

```text
Application
   │ bounded non-blocking queue
   ▼
ErrorBeacon API
   │
   ├── SQLite incident history
   ├── fingerprint + deduplication
   ├── persistent spike detection
   ├── deployment regression detection
   ├── bounded alert workers
   ├── Telegram immediately
   └── AI enrichment afterwards
   └── ambiguous Telegram sends are persisted as unknown and are not replayed automatically
       ├── Groq
       ├── Gemini primary
       ├── Gemini fallback model
       └── OpenRouter
```

ErrorBeacon is intentionally a separate service. Keep it outside your backend/frontend blue-green revision lifecycle.

## What ErrorBeacon captures

The integrated Asset Inventory Quotes application reports:

- Unhandled FastAPI exceptions
- Important explicit 500/error catches in asset, quotation, backup, extension and notification services
- Database startup/readiness failures
- Celery `task_failure` events
- Redis rate-limit degradation
- Browser `window.onerror`
- Browser unhandled promise rejections
- Frontend API 5xx failures
- Frontend network failures
- Request IDs, release/image tags, component and operation metadata where available

Normal business 4xx responses are not treated as production incidents unless the application explicitly reports them.

## Performance and reliability

Two bounded queues protect application availability:

1. The monitored application's ErrorBeacon client uses a small bounded queue and 1–2 worker threads. It no longer creates one thread per exception.
2. ErrorBeacon itself uses a bounded alert queue and a fixed worker pool. Telegram/Gemini work cannot create unbounded threads.

If the monitor is unavailable, the application continues. If an extreme error storm fills a local queue, monitoring events are dropped rather than taking the application down.

The first Telegram alert does not wait for AI. AI analysis is a follow-up enrichment message. Providers are attempted in this order: Groq, Gemini primary, Gemini fallback model, then OpenRouter; only providers with configured API keys participate.

Spike detection is persisted in `incident_events`, so a monitor restart does not erase the recent error-rate history.

## Generate the API key

Generate a dedicated cryptographically secure 256-bit key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use the generated value in **both** ErrorBeacon and the monitored application:

```env
ERRORBEACON_API_KEY=YOUR_GENERATED_KEY
```

The application sends it as:

```text
X-API-Key: YOUR_GENERATED_KEY
```

Do not reuse a JWT secret, database password, Telegram token, Cloudflare token or another credential.

For ACA, store it as an ACA secret. For the VM, store it only in `.env`. For Render, create it as a secret environment variable.

## Configure Telegram

Set:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
```

For a forum topic:

```env
TELEGRAM_THREAD_ID=123
```

ErrorBeacon uses Telegram long polling for the interactive Resolve/View/Silence buttons. This requires outbound HTTPS only and works with an internal-only ACA Container App.

Run only one ErrorBeacon replica when Telegram polling is enabled.

## AI provider fallback chain

AI enrichment is optional and never blocks the immediate Telegram alert.

Configure:

```env
AI_ENABLED=true
GROQ_API_KEY=
GROQ_MODEL=llama-3.1-8b-instant
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free
OPENROUTER_SITE_URL=https://errorbeacon.local
```

The configured provider chain is:

```text
Groq -> Gemini primary -> Gemini fallback -> OpenRouter
```

A missing provider key removes that provider from the chain. Provider errors and
empty/invalid analyses cause failover. Durable SQLite retry state protects pending
AI work across process restarts.

## Run locally

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8787/healthz
```

The `/v1/test` endpoint is a real end-to-end Telegram test: it creates a fresh test incident, sends the immediate alert, then queues optional AI enrichment. It is intentionally not suppressed by normal incident deduplication.

Controlled Telegram test:

```bash
curl -X POST http://127.0.0.1:8787/v1/test -H "X-API-Key: YOUR_KEY"
```

The response contains the same `request_id` shown in Telegram.

## Connect the Asset Inventory Quotes application

Docker Compose:

```env
ERRORBEACON_URL=http://errorbeacon:8000
ERRORBEACON_API_KEY=YOUR_GENERATED_KEY
```

ACA uses the internal service name:

```text
http://errorbeacon:8000
```

VM Compose uses the same private Docker DNS name.

The browser never receives `ERRORBEACON_API_KEY`. Browser errors are forwarded through the backend telemetry endpoint.

The frontend reporter stores the latest `X-Request-ID` returned by any same-origin API response and attaches it to later browser exceptions. URLs are sanitized before reporting, including password-reset tokens and other sensitive query parameters.

## API

### Ingest an event

```text
POST /v1/events
X-API-Key: YOUR_KEY
Content-Type: application/json
```

### Controlled test

```text
POST /v1/test
X-API-Key: YOUR_KEY
```

### List incidents

```text
GET /v1/incidents?limit=50
X-API-Key: YOUR_KEY
```

### Resolve

```text
POST /v1/incidents/{incident_id}/resolve
X-API-Key: YOUR_KEY
```

### Silence

```text
POST /v1/incidents/{incident_id}/silence?seconds=3600
X-API-Key: YOUR_KEY
```

## Telegram controls

Every alert includes:

- **🔎 View**: sends the incident summary
- **✅ Resolve**: marks the incident resolved
- **🔕 Silence 1h**: suppresses alerts for that incident for one hour

Callback actions are accepted only from the configured Telegram chat ID.

## Security and redaction

Before persistence, Telegram delivery or Gemini analysis, ErrorBeacon recursively redacts sensitive fields and common credential formats, including:

- Authorization/Bearer tokens
- cookies/session IDs
- API keys
- passwords/secrets
- JWTs
- database connection strings
- reset/access/refresh tokens
- sensitive context dictionary keys

Frontend URLs are sanitized so `reset_token`, `access_token`, `code`, `password`, `api_key`, `session_id` and similar values cannot be forwarded in clear text.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the complete installation paths:

- Local Docker
- Render Free
- Render persistent/paid
- Azure Container Apps
- Azure VM
- Kubernetes/other Docker hosts

For ACA, the integrated Bicep now provisions an Azure Files share for `/data`, so the SQLite incident database survives ErrorBeacon revision replacement. The ErrorBeacon Container App remains at one warm replica and is independent of backend/frontend blue-green traffic.

Render Free remains a best-effort/free option. Its filesystem is ephemeral and Free services can spin down/restart, so use an external health monitor if you want to reduce cold starts. It is not an uptime guarantee.

## Files

```text
errorbeacon/
├── app/main.py
├── Dockerfile
├── requirements.txt
├── docker-compose.yml
├── render.yaml
├── DEPLOYMENT.md
└── tests/test_core.py
```

### Storage note

Local Docker and VM use SQLite WAL by default. ACA's Azure Files-backed `/data` uses `SQLITE_JOURNAL_MODE=DELETE` to avoid relying on WAL semantics over the SMB-backed share.

## Incident classification and alert policy

ErrorBeacon separates an incident being **stored** from an incident being **paged to Telegram**.

- `healthcheck`: stored for diagnostics but never sent as an incident alert.
- `dependency_degraded`: used for recoverable dependency degradation such as Redis rate-limit fail-open. It has no fake HTTP 503 because the monitored request was not rejected by the rate limiter.
- `chaos_test`: controlled resilience/correlation tests. These are labeled `CONTROLLED CHAOS TEST`. They alert by default in development/local Docker. Set `CHAOS_TEST_ALERTS=false` in production to persist the test event without sending it to Telegram.
- `client_error`: genuine browser/client-side failures.

The frontend client telemetry path does not invent an HTTP status code. Browser/test exit codes such as `0` are kept in context when supplied, while `status_code` remains null unless a real HTTP response status is known.


### Telegram delivery reliability (v3.9)

Telegram Bot API `sendMessage` does not provide a client idempotency key. A timeout or connection error therefore has an ambiguous outcome: Telegram may have accepted the message even when ErrorBeacon did not receive the response. ErrorBeacon now records such sends as `unknown` and suppresses automatic replay. Explicit Telegram API rejections remain retryable. This prevents a network timeout from turning one incident into duplicate Telegram alerts.

The same rule applies to AI enrichment notifications: a successful AI analysis is persisted before its Telegram delivery attempt, and an ambiguous delivery is stored as `telegram_unknown` rather than replayed.

## AI analysis reliability (v3.9)
AI analysis runs on a dedicated serialized queue with retry/backoff. Telegram delivery is not blocked by Gemini analysis, and the ErrorBeacon health endpoint exposes both alert and AI queue depth so local chaos tests can wait for analysis completion.
