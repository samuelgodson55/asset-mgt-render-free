# Snipe-IT Lite

**A production-grade asset lifecycle platform** for tracking equipment
inventory, custody, approvals, and fulfillment — built to demonstrate
real operational engineering, not just CRUD.

From asset pools and checkouts to quote-to-checkout workflows and
audit-grade traceability, it gives organizations a single system of
record for what they own, who's using it, and how requests become
approved, accountable handoffs.

**Stack:** FastAPI · PostgreSQL · SQLAlchemy · Celery · React/TypeScript
**Infra:** Docker Compose · Azure Container Apps or single-VM Terraform, both with zero-downtime blue-green deploys

---

## At a Glance

| | |
|---|---|
| ⚡ **Zero-downtime releases** | Blue-green canary rollout (10→25→50→75→100%) with automatic health-gated rollback |
| ☁️ **Two production targets** | Azure Container Apps (Bicep) or a single VM (Terraform) — same images, one deploy story |
| 🔍 **Built-in observability** | OpenTelemetry tracing + ErrorBeacon, a purpose-built exception-aggregation service with Telegram/email alerting and AI-assisted triage |
| 🔒 **Security by default** | Argon2id hashing, JWT sessions, rate limiting, server-side RBAC, CSP/security headers on every response |
| 🧪 **CI discipline** | Lint, real-service test suite, secret scanning (Gitleaks), and image scanning (Trivy) on every push |
| 📜 **Full audit trail** | Every mutating action logged to an append-only record |
| ⚙️ **DB concurrency hardening** | Admission-controlled connection pooling and PgBouncer tuning so background jobs can't starve the API |

## Core Model

- **Asset Pool** — a type of asset, its total stock, and what's available
- **Checkout / Custody** — who has what, and when it's due back
- **Exception** — units under repair, missing, or otherwise unavailable
- **Quote** — the approval-driven request that gets fulfilled into a real checkout

A quote isn't a side feature — it's the core workflow: request → review
→ approve → fulfill, fully traceable end to end.

## Quick Start

Requires Docker only — no local Python or Node.js.

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into JWT_SECRET_KEY

docker compose up --build
```

| | |
|---|---|
| App | http://localhost:8080 |
| API docs | http://localhost:8080/docs |

Demo accounts (seeded automatically on a fresh database):

| Role | Username | Password |
|------|----------|----------|
| Admin | `r.adeyemi` | `Admin123!` |
| Manager | `s.chen` | `Manager123!` |
| Staff | `t.okafor` | `Staff123!` |
| Customer | `d.martins` | `Customer123!` |

## Architecture

```
┌──────────────┐      /api/*      ┌──────────────┐      ┌──────────────┐
│ frontend     │ ───────────────► │ backend      │ ───► │ PostgreSQL   │
│ (nginx)      │ ◄──── static ─── │ (FastAPI)    │      │ (primary DB) │
└──────┬───────┘                  └──────┬───────┘      └──────────────┘
       │                                  │
       │                                  ▼
       │                            ┌──────────────┐      ┌──────────┐
       │                            │ worker       │ ───► │ Redis    │
       │                            │ (Celery)     │      │ (jobs)   │
       │                            └──────┬───────┘      └──────────┘
       │                                   │ telemetry / error reporting
       │                                   ▼
       │                           ┌─────────────────────┐
       └──────────────────────────►│ ErrorBeacon         │
                                   │ exception monitor    │
                                   │ + Telegram/email     │
                                   └─────────────────────┘
```

nginx is the only publicly-exposed service — it serves the frontend and
reverse-proxies `/api/*` to the backend, so the same image runs
unmodified in every environment.

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic |
| Database | PostgreSQL 16 |
| Frontend | React + TypeScript + Vite, Tailwind CSS |
| Background jobs | Celery + Redis |
| Monitoring | ErrorBeacon (custom) + OpenTelemetry |
| Reverse proxy | nginx |
| Cloud infra | Azure Container Apps (Bicep) or single VM (Terraform) |
| CI/CD | GitHub Actions — lint, test, security scan, build, blue-green deploy |

## Project Structure

```
├── backend/          FastAPI app, SQLAlchemy models, Alembic migrations
├── frontend-app/     React + TypeScript frontend (dashboard, assets, quotes, reports)
├── nginx/            Reverse proxy config (templated per environment)
├── infra/            Bicep — Azure Container Apps
├── infra-vm/         Terraform — single-VM deployment
├── scripts/          Blue-green deploy, health checks, backups
├── errorbeacon/      Monitoring and incident reporting service
└── .github/workflows/ CI, image builds, deploys, secret rotation
```

## Deployment

Two production targets, one deploy story — both driven from GitHub
Actions (`workflow_dispatch`, or a `git tag vX.Y.Z` push), no manual
`docker push` or SSH required:

- **Azure Container Apps** — `infra/main.bicep`
- **Single VM** — `infra-vm/` (Terraform)

## Testing

```bash
docker compose exec backend pip install -r backend/requirements-dev.txt
docker compose exec backend pytest backend/tests
```

Runs against real Postgres/Redis service containers, not mocks. CI also
runs `ruff`, `pip-audit`, Gitleaks, and a Trivy image scan on every push.

---

## Documentation

This README is intentionally a quick overview. For a deep technical
dive — full feature tour, environment variables, database/migrations,
tracing setup, API reference, and a "making changes safely" guide —
see **[`docs/README.md`](docs/README.md)**, the project's full manual.

| Doc | Covers |
|---|---|
| [`docs/README.md`](docs/README.md) | **Full technical manual** — features, architecture, API reference, dev guide |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Azure Container Apps deploy guide |
| [`docs/DEPLOYMENT_VM.md`](docs/DEPLOYMENT_VM.md) | Single-VM deploy guide |
| [`docs/POST_DEPLOYMENT.md`](docs/POST_DEPLOYMENT.md) | Day-2 operations |
| [`docs/SRE_STRATEGY.md`](docs/SRE_STRATEGY.md) | Reliability & on-call posture |
| [`blue-green.md`](blue-green.md) | How zero-downtime rollout works |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |
