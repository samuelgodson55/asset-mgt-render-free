# Snipe-IT Lite

Asset Inventory Quotes is a self-hosted asset lifecycle platform for
managing equipment inventory, custody, approvals, and fulfillment across
teams. It helps organizations track what they own, who is using it,
what is available, and how equipment requests are turned into approved,
auditable operational workflows.

From asset pools and checkouts to exception handling and quote-to-checkout
operations, the platform brings inventory visibility, service delivery,
and operational accountability into a single system.

This is designed for environments where equipment is a shared operational
resource: IT teams managing laptops and peripherals, service teams
allocating devices and tools, or support functions that need a clear chain
from request to approval to physical handoff.

Operational visibility is built in as part of the platform: backend and
browser telemetry are routed to ErrorBeacon for exception aggregation,
alerting, and incident triage, so teams can identify issues before they
become service disruptions.

The system is built around four core business objects:

- **Asset Pool** — the inventory group that defines a type of asset, its
  total stock, and its available quantity.
- **Checkout / Custody** — the active assignment of equipment to a user
  or ad-hoc individual, including due dates and returns.
- **Exception** — the operational state for equipment that is under
  repair, missing, lost, or otherwise unavailable.
- **Quote** — the approval-driven request workflow that captures demand,
  price, dates, and business intent before a device is fulfilled.

A quote is not a side feature here; it is a core part of the product's
workflow. Staff and customers can create requests, managers can review and
adjust them, and approved quotes are ultimately fulfilled as real asset
checkouts. The result is a structured, traceable path from need to
allocation to return.

The default user experience is a React + TypeScript interface built around
work queues, operational dashboards, and manager reporting: dashboard KPIs,
asset utilization trends, overdue and due-soon views, and a dedicated
Reports surface for operational analysis and export workflows.

**Backend:** FastAPI · PostgreSQL · SQLAlchemy · Celery
**Frontend:** React + TypeScript + Vite · Tailwind CSS (legacy static frontend remains supported)
**Infra:** Docker Compose locally · Azure Container Apps or a single VM in production

---

## Features

- **Asset pools** — create pools (for example, "MacBook Pro 14" M3"),
  track total versus available quantity, and flag units under repair or
  lost
- **Dashboard & reporting** — inventory health, checkout trends,
  overdue activity, utilization by asset type, spend/revenue summaries,
  and operational reporting for Manager/Admin decision-making
- **Quote-to-checkout workflow** — browse the catalog, build an order,
  submit a quote, approve it, and fulfill it into a real asset checkout
- **Checkout & returns** — dispatch to staff, linked customers, or ad-hoc
  individuals; partial and bulk returns; due-date extensions
- **Role-based access** — Super Admin, Admin, Manager, Staff, and
  Customer roles, each scoped to the actions and visibility they should
  have
- **Notifications** — overdue and due-soon alerts, extension requests,
  optional email digests, and in-app notices for operational activity
- **Operational visibility** — backend and browser exceptions are reported
  to ErrorBeacon for grouping, alerting, and faster incident triage
- **Audit trail** — every mutating action is logged in an append-only
  record for accountability and traceability
- **Exports** — CSV/PDF exports for inventory, audit logs, user
  checkouts, and reporting views
- **Blue-green deploys** — zero-downtime rollouts with automatic rollback
  on a failed health check, on both supported infra targets

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

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic |
| Database | PostgreSQL 16 |
| Auth | PyJWT sessions, Argon2id password hashing (`pwdlib`) |
| Background jobs | Celery + Redis (exports, notifications, scheduled digests) |
| Monitoring & alerting | ErrorBeacon — exception aggregation, Telegram/email alerts, AI-assisted triage |
| Email | Plain SMTP (`smtplib`) — works with any provider, no vendor SDK |
| Frontend | React + TypeScript + Vite, Tailwind CSS — default web app for the live product |
| Legacy frontend | Static HTML/JS build remains available for compatibility and operational fallback |
| Reverse proxy | nginx — serves static assets, proxies `/api/*` to the backend |
| Local infra | Docker Compose (6 services: db, redis, backend, worker, beat, frontend) |
| Cloud infra | Azure Container Apps (Bicep) or a single VM (Terraform) |
| CI/CD | GitHub Actions — lint, test, security scan, build, blue-green deploy |

## Architecture

```
┌──────────────┐      /api/*      ┌──────────────┐      ┌──────────────┐
│ frontend     │ ───────────────► │ backend      │ ───► │ PostgreSQL   │
│ (nginx)      │ ◄──── static ─── │ (FastAPI)    │      │ (primary DB) │
└──────┬───────┘                  └──────┬───────┘      └──────────────┘
       │                                  │
       │                                  │
       │                                  ▼
       │                            ┌──────────────┐      ┌──────────┐
       │                            │ worker       │ ───► │ Redis    │
       │                            │ (Celery)     │      │ (jobs)   │
       │                            └──────┬───────┘      └──────────┘
       │                                   │
       │                                   │ telemetry / error reporting
       │                                   ▼
       │                           ┌─────────────────────┐
       └──────────────────────────►│ ErrorBeacon         │
                                   │ exception monitor   │
                                   │ + Telegram/email    │
                                   └─────────────────────┘
```

nginx is the only publicly-exposed service. It serves the static
frontend directly and reverse-proxies `/api/*` to the backend — the
browser never talks to FastAPI directly, so nothing in the frontend
needs to know a backend hostname. This is what lets the same image run
unmodified in every environment.

The platform also includes ErrorBeacon as an operational reporting layer.
Backend exceptions and browser telemetry are sent to it for grouping,
alerting, and incident triage. This keeps the product experience simpler
while giving operators a clear operational signal when something breaks.

## Project Structure

```
├── backend/          FastAPI app, SQLAlchemy models, Alembic migrations
├── frontend-app/     React + TypeScript default frontend (dashboard,
│                    assets, quotes, reports)
├── frontend/         Legacy static HTML/JS frontend retained for
│                    compatibility/fallback
├── nginx/            Reverse proxy config (templated per environment)
├── infra/            Bicep — Azure Container Apps
├── infra-vm/         Terraform — single-VM deployment
├── scripts/          Blue-green deploy, health checks, backups
├── errorbeacon/      Monitoring and incident reporting service
└── .github/workflows/ CI, image builds, deploys, secret rotation
```

## Deployment

Two supported production targets, same application images:

- **Azure Container Apps** — `infra/main.bicep` + `deploy-azure-aca.yml`.
  Blue-green canary rollout (10→25→50→75→100%) with health-gated
  auto-rollback. See [`DEPLOYMENT.md`](docs/DEPLOYMENT.md).
- **Single VM** — `infra-vm/` (Terraform) + `deploy-azure-vm.yml`. Same
  blue-green model, local disk instead of cloud storage. See
  [`DEPLOYMENT_VM.md`](docs/DEPLOYMENT_VM.md).

Both are driven from GitHub Actions (`workflow_dispatch`, or a `git tag
vX.Y.Z` push via `release.yml`) — no manual `docker push` or SSH required.

## Testing

The `backend` image only installs `backend/requirements.txt` (production
dependencies) -- pytest itself, and starlette's TestClient dependency
(`httpx2`), live in `backend/requirements-dev.txt` instead, so they're
never shipped in the deployed image. Install those into the container
once per environment, then run the suite:

```bash
docker compose exec backend pip install -r backend/requirements-dev.txt
docker compose exec backend pytest backend/tests
```

Or, for a local (non-Docker) virtualenv against a Postgres/Redis you're
running yourself:

```bash
cd backend
pip install -r requirements-dev.txt
pytest tests
```

Runs against real Postgres/Redis service containers, not mocks. CI
(`ci.yml`) also runs `ruff`, `pip-audit`, a Gitleaks secret scan, and a
Trivy image scan on every push and PR.

## Security

- Argon2id password hashing, JWT session tokens
- Redis-backed login rate limiting
- Role-based authorization enforced server-side on every endpoint
- Security headers (CSP, X-Frame-Options, etc.) on every response

## Documentation

| Doc | Covers |
|---|---|
| [`README.md`](README.md) | This file |
| [`DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Azure Container Apps deploy guide |
| [`DEPLOYMENT_VM.md`](docs/DEPLOYMENT_VM.md) | Single-VM deploy guide |
| [`POST_DEPLOYMENT.md`](docs/POST_DEPLOYMENT.md) | Day-2 operations |
| [`blue-green.md`](blue-green.md) | How zero-downtime rollout works |
| [`SRE_STRATEGY.md`](docs/SRE_STRATEGY.md) | Reliability & on-call posture |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |
