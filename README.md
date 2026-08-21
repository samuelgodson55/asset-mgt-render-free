# Snipe-IT Lite

**A production-grade asset lifecycle platform** — equipment inventory,
custody, approvals, and fulfillment — built to demonstrate real
operational engineering, not just CRUD.

From asset pools and checkouts to quote-to-checkout workflows and
audit-grade traceability, it gives an organization a single system of
record for what it owns, who's using it, and how requests become
approved, accountable handoffs.

**Stack:** FastAPI · PostgreSQL · SQLAlchemy · Celery · React/TypeScript
**Infra:** Docker Compose (local) · Azure Container Apps or single-VM
Terraform (production) · Render free tier (zero-cost demo), all with
zero-downtime blue-green deploys

**🔗 Live demo:** [snipeit-lite-web.onrender.com](https://snipeit-lite-web.onrender.com)
— runs on Render's free tier, so the first request after 15 minutes of
inactivity takes ~1 minute to spin back up. Demo login credentials are in
the [Quick Start](#quick-start) section below.

---

## Why this project

Most portfolio CRUD apps stop at "it works." This one is built the way a
small team would actually run it in production:

- **Deploys three different ways** from the same codebase — Azure
  Container Apps, a Terraform-managed VM, and a single-container Render
  free-tier build — with one CI/CD pipeline behind all three.
- **Zero-downtime releases**, not just "docker restart": a blue-green
  canary rollout (10→25→50→75→100%) with automatic health-gated rollback.
- **Real observability**: OpenTelemetry tracing plus ErrorBeacon, a
  purpose-built exception-aggregation service (its own repo-within-a-repo)
  with Telegram/email alerting and AI-assisted triage.
- **Schema discipline**: 18 additive-only Alembic migrations, tested with
  a real `upgrade`/`downgrade` chain in CI — not just `create_all()` and
  hope.
- **Security and audit as first-class**, not bolted on: Argon2id hashing,
  JWT sessions, per-IP and per-account rate limiting, server-side RBAC,
  and an append-only audit log for every mutating action.

## At a Glance

| | |
|---|---|
| ⚡ **Zero-downtime releases** | Blue-green canary rollout (10→25→50→75→100%) with automatic health-gated rollback |
| ☁️ **Three deploy targets** | Azure Container Apps (Bicep), a single VM (Terraform), or Render's free tier — same codebase, one CI/CD pipeline |
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
       │                           ┌───────────────────┐
       └──────────────────────────►│ ErrorBeacon       │
                                   │ exception monitor │
                                   │ + Telegram/email  │
                                   └───────────────────┘
```

nginx is the only publicly-exposed service — it serves the frontend and
reverse-proxies `/api/*` to the backend, so the same image runs
unmodified in every environment. (The Render free-tier build collapses
this into a single container instead, since Render's free plan has no
private-service or background-worker type — see `render.yaml`'s
top-of-file comment for the full reasoning.)

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic |
| Database | PostgreSQL 16 |
| Frontend | React + TypeScript + Vite, Tailwind CSS |
| Background jobs | Celery + Redis |
| Monitoring | ErrorBeacon (custom) + OpenTelemetry |
| Reverse proxy | nginx |
| Cloud infra | Azure Container Apps (Bicep), single VM (Terraform), or Render free tier |
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

Three production-shaped targets, one deploy story — all driven from
GitHub Actions (`workflow_dispatch`, or a `git tag vX.Y.Z` push), no
manual `docker push` or SSH required:

- **Azure Container Apps** — `infra/main.bicep`
- **Single VM** — `infra-vm/` (Terraform)
- **Render (free tier)** — `render.yaml` + `Dockerfile.render`, a single
  combined image built specifically to fit Render's free-plan
  constraints (no Background Worker or Private Service type available)

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
