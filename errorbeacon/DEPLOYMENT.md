# ErrorBeacon Lite Deployment Guide

## Table of Contents

1. [Common configuration](#1-common-configuration)
2. [Generate the API key](#2-generate-the-api-key)
3. [Local Docker](#3-local-docker)
4. [Render Free](#4-render-free)
5. [Render persistent/paid](#5-render-persistentpaid)
6. [Azure Container Apps](#6-azure-container-apps)
7. [Azure VM](#7-azure-vm)
8. [Kubernetes and other Docker hosts](#8-kubernetes-and-other-docker-hosts)
9. [Production checklist](#9-production-checklist)

# 1. Common configuration

ErrorBeacon:

```env
ERRORBEACON_INGEST_API_KEY=...   # required for monitored application ingest
ERRORBEACON_ADMIN_API_KEY=...   # required for management endpoints
ERRORBEACON_TRUST_PROXY_HEADERS=false   # safe default; enable only behind a trusted proxy
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
TELEGRAM_THREAD_ID=
AI_ENABLED=true
GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free
OPENROUTER_SITE_URL=https://errorbeacon.local
TELEGRAM_POLLING=true
# ErrorBeacon email fallback reuses the application's existing email settings:
# NOTIFICATIONS_ENABLED, EMAIL_PROVIDER, SMTP_*, BREVO_API_KEY, RESEND_API_KEY,
# SMTP_FROM_EMAIL and ADMIN_NOTIFICATION_EMAILS. Do not configure another provider.
ERRORBEACON_EMAIL_FALLBACK_ENABLED=true
ERRORBEACON_EMAIL_FALLBACK_AFTER_ATTEMPTS=3
ERRORBEACON_EMAIL_FALLBACK_AFTER_SECONDS=300
ERRORBEACON_RETENTION_DAYS=90
ERRORBEACON_AUTO_STALE_DAYS=30
ERRORBEACON_DB_WARN_MB=4096
ERRORBEACON_CLEAN_MAX_DEPTH=10
ERRORBEACON_CLEAN_MAX_ITEMS=100
ERRORBEACON_DIGEST_ENABLED=true
ERRORBEACON_DIGEST_INTERVAL_HOURS=24
ERRORBEACON_STARTUP_SELF_TEST=false
ADMIN_NOTIFICATION_EMAILS=admin@example.com
# Reverse direction of the monitored application's ERRORBEACON_URL below -- lets
# the Telegram /apphealth command poll the monitored app's own /healthz, /readyz
# and /health/dependencies. Optional; each deployment target below sets its own
# correct value/default -- see that target's section.
BACKEND_URL=http://backend:8000
BACKEND_HEALTH_TIMEOUT_SECONDS=5
```

Monitored application:

```env
ERRORBEACON_URL=http://errorbeacon:8000
ERRORBEACON_INGEST_API_KEY=the-same-key
ERRORBEACON_APP=asset-inventory-quotes
APP_RELEASE=your-image-tag-or-commit-sha
```

# 2. Generate the API key

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

This generates 32 random bytes (256 bits) encoded as a URL-safe string.

Put the exact generated value in both systems. Never put it in source code, a Dockerfile, GitHub workflow YAML, frontend JavaScript or a committed `.env` file.

# 3. Local Docker

From `errorbeacon/`:

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8787/healthz
```

Test:

```bash
curl -X POST http://127.0.0.1:8787/v1/test -H "X-API-Key: YOUR_KEY"
curl -X POST http://127.0.0.1:8787/v1/test-telegram -H "X-API-Key: YOUR_KEY"
curl -X POST http://127.0.0.1:8787/v1/test-email -H "X-API-Key: YOUR_KEY"
```

When both applications run in one Compose project, use `http://errorbeacon:8000` from the backend. Do not use `localhost` from one container to reach another. The reverse direction (`BACKEND_URL`, used by ErrorBeacon's `/apphealth` Telegram command) defaults to `http://backend:8000` and works as-is in this same Compose project.

# 4. Render Free

Render Free is useful for development and low-volume monitoring, but it is not a hard always-on production service. Free Web Services can spin down after inactivity and their local filesystem is ephemeral.

Recommended arrangement:

```text
Production backend
      │ HTTPS
      ▼
ErrorBeacon on Render Free
      │
      ▼
Telegram
```

Create a Docker Web Service from `errorbeacon/` and set:

```text
Health check: /healthz
Plan: Free
```

Set the secret environment variables in Render. Do not commit `.env`. Keep `ERRORBEACON_TRUST_PROXY_HEADERS=false` on Render Free because ErrorBeacon is itself publicly reachable and clients can spoof proxy headers.

ErrorBeacon here is a fully standalone Render Web Service with no shared network to the monitored application, so `BACKEND_URL` (the `/apphealth` Telegram command's target) has no working default and is left unset. `/apphealth` reports itself as not configured until you set `BACKEND_URL` to the monitored app's public URL in this service's own Render environment variables.

To reduce cold starts, use a free external uptime monitor to request `/healthz` every five minutes. This is only a practical workaround; Render can still restart the service and the monitor's filesystem is not durable.

If incident history must survive restarts, use Render with a persistent database/storage option or move ErrorBeacon to ACA/VM.

# 5. Render persistent/paid

Use the same Docker deployment, but attach a persistent database/storage solution supported by your Render plan. Keep the ErrorBeacon service separate from the application whose errors it monitors.

# 6. Azure Container Apps

The integrated Asset Inventory Quotes Bicep provisions ErrorBeacon separately from backend/frontend blue-green traffic.

The intended topology is:

```text
ACA Environment
├── frontend          public
├── backend           blue/green
├── redis
├── migrate
└── errorbeacon       internal, min=1, max=1
```

ErrorBeacon has:

- internal ingress only
- `ERRORBEACON_TRUST_PROXY_HEADERS=true` because ACA ingress is a controlled proxy boundary
- one warm replica
- independent revision lifecycle
- Azure Files mounted at `/data`
- bounded alert workers
- Telegram long polling for interactive controls
- separate API/Telegram/Gemini secrets

The integrated infrastructure creates an `errorbeacon-data` Azure Files share and mounts it at `/data`. This prevents incident history from disappearing when the ErrorBeacon revision is replaced.

Because Telegram callbacks use outbound long polling, no public ErrorBeacon ingress is required.

Deploy the normal infrastructure workflow first, then the ACA application deployment as you already do. ErrorBeacon is not part of backend/frontend traffic switching.

Unlike Compose/VM, the backend does not reach it at `http://errorbeacon:8000`. ACA apps with internal-only ingress are only reachable through the environment's ingress proxy (port 80/443, not the container's own port), so Bicep sets `ERRORBEACON_URL` to the internal FQDN instead, with no port suffix:

```text
http://errorbeacon.internal.<environment-default-domain>
```

The reverse direction (`BACKEND_URL`, used by the `/apphealth` Telegram command) works the same way: `infra-deploy.yml` resolves backend's own internal-ingress FQDN with `az containerapp show` before each deployment and Bicep sets `BACKEND_URL` to it automatically -- nothing to configure by hand. It's a plain resolved parameter rather than a Bicep-internal resource reference, because `backend` already depends on `errorbeacon` (for `ERRORBEACON_URL` above); a reference back the other way inside Bicep itself would be a circular dependency. On the very first deploy of a brand-new environment, `backend` doesn't exist yet, so this resolves to empty and `/apphealth` reports itself as not configured until the next deploy.

# 7. Azure VM

The integrated `docker-compose.vm.yml` includes ErrorBeacon as an isolated service:

```text
backend-blue / backend-green
frontend-blue / frontend-green
worker / beat
postgres / redis
caddy / cloudflared
errorbeacon
```

Incident history is persisted at:

```text
/mnt/docker-data/volumes/errorbeacon_data
```

The backend reaches it through:

```text
http://errorbeacon:8000
```

The reverse direction (`BACKEND_URL`, used by the `/apphealth` Telegram command) points at `http://backend-green:8000` by default -- the fixed, always-on production slot, not `backend-blue`, which only exists transiently during a rollout -- and reaches it directly rather than through Caddy/nginx, since nginx's reverse proxy only forwards `/healthz`/`/readyz` externally, not `/health/dependencies`.

Deploy/update normally with your existing VM workflow. No separate public ErrorBeacon endpoint is necessary.

# 8. Kubernetes and other Docker hosts

Run one ErrorBeacon replica when Telegram polling is enabled. Mount `/data` to persistent storage and expose port 8000 only to the monitored application/network.

The same environment variables and API key rules apply. Set `BACKEND_URL` to wherever the monitored application is reachable on your cluster/network if you want the `/apphealth` Telegram command to work; leave it unset otherwise.

# 9. Production checklist

- [ ] Generate a dedicated 256-bit ErrorBeacon API key.
- [ ] Store the key as a secret, never in source code.
- [ ] Configure Telegram bot token/chat ID.
- [ ] Test `/v1/test` and confirm the Telegram Request ID matches the API response.
- [ ] Keep ErrorBeacon outside application blue/green traffic switching.
- [ ] Keep one replica while Telegram long polling is enabled.
- [ ] Mount persistent `/data` on VM/ACA/persistent hosting.
- [ ] Treat Render Free as best effort only.
- [ ] Verify reset/access tokens are redacted from browser URLs and traces.
- [ ] Verify Resolve/View/Silence buttons work from the configured Telegram chat.
- [ ] Monitor ErrorBeacon's own `/healthz` endpoint.

### ACA SQLite note

The ACA Azure Files mount uses `SQLITE_JOURNAL_MODE=DELETE` instead of SQLite WAL because the incident database is on an SMB-backed Azure Files share. Local Docker and VM deployments keep the faster WAL default on local disk.

## Incident classification and production alert noise

ErrorBeacon stores health/dependency/test events separately from its Telegram notification decision. Health-check events are never paged. Redis rate-limit failures are recorded as `dependency_degraded` and remain fail-open, so the application's successful request is not falsely reported as HTTP 503. Controlled chaos/correlation events are labeled `CONTROLLED CHAOS TEST`; local development alerts them by default, while production deployments should set `CHAOS_TEST_ALERTS=false` so deliberate test events remain auditable without creating responder noise.

Client-side events use a null HTTP status unless an actual HTTP response status exists. Do not interpret shell exit status `0` as HTTP 0.
