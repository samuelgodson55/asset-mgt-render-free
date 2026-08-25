# Snipe-IT Lite

**A production-style asset lifecycle platform** — equipment inventory,
custody, approvals, and fulfillment — built to demonstrate real
operational engineering, not just CRUD.

From asset pools and checkouts to quote-to-checkout workflows and
audit-grade traceability, it gives an organization a single system of
record for what it owns, who's using it, and how requests become
approved, accountable handoffs.

**Stack:** FastAPI · PostgreSQL · SQLAlchemy · Celery · React/TypeScript
**Infra:** Docker Compose (local) · Azure Container Apps or single-VM
Terraform (production) · Render free tier (zero-cost demo) — all with
zero-downtime blue-green deploys

**🔗 Live demo:** [snipeit-lite-web.onrender.com](https://snipeit-lite-web.onrender.com)
— runs on Render's free tier, so the first request after 15 minutes of
inactivity takes ~1 minute to spin back up. Demo credentials are in
[Quick Start](#quick-start).

---

## Why This Project

Most portfolio CRUD apps stop at "it works." This one is built the way a
small team would actually run it in production, and was written as a
practical demonstration of engineering across the full lifecycle of a
real system — not just the day it ships:

- **Deploys three different ways** from the same codebase — Azure
  Container Apps, a Terraform-managed VM, and a single-container Render
  free-tier build — behind one CI/CD pipeline.
- **Zero-downtime releases**, not just `docker restart`: a blue-green
  canary rollout (10→25→50→75→100%) with automatic health-gated rollback.
- **Real observability**: OpenTelemetry tracing plus **ErrorBeacon**, a
  purpose-built exception-aggregation service (its own repo-within-a-repo)
  with fingerprinting, spike/regression detection, Telegram/email
  alerting, and AI-assisted triage.
- **Schema discipline**: 18 additive-only Alembic migrations, tested
  with a real `upgrade`/`downgrade` chain in CI — not just
  `create_all()` and hope.
- **Security and audit as first-class**, not bolted on: Argon2id
  hashing, JWT sessions + MFA, per-IP/per-account rate limiting,
  server-side RBAC, and an append-only audit log for every mutating
  action.
- **DB concurrency hardening**: admission-controlled connection pooling
  and PgBouncer tuning so a burst of background jobs can never starve
  the live API of connections.

This repo is the flagship project in my portfolio, spanning **full-stack
development, cloud engineering, DevOps/CI/CD, infrastructure as code,
application security, database engineering, reliability engineering, and
observability.**

## At a Glance

| | |
|---|---|
| ⚡ **Zero-downtime releases** | Blue-green canary rollout (10→25→50→75→100%) with automatic health-gated rollback |
| ☁️ **Three deploy targets** | Azure Container Apps (Bicep), a single VM (Terraform), or Render's free tier — same codebase, one CI/CD pipeline |
| 🔍 **Built-in observability** | OpenTelemetry tracing + ErrorBeacon, a custom exception-aggregation service with dedup, spike detection, and AI-assisted triage |
| 🔒 **Security by default** | Argon2id hashing, JWT + MFA, rate limiting, server-side RBAC, CSP/security headers on every response |
| 🧪 **CI discipline** | Lint, real-service test suite, dependency audit (pip-audit), secret scanning (Gitleaks), image scanning (Trivy) |
| 📜 **Full audit trail** | Every mutating action logged to an append-only record |
| ⚙️ **DB concurrency hardening** | Admission-controlled pooling and PgBouncer tuning so background jobs can't starve the API |
| 💥 **Resilience-tested** | Load, chaos, and dependency-failure scripts — not just happy-path tests |

## The Problem It Solves

Teams managing physical equipment need answers to simple but
operationally important questions: what do we own, how much is
available, who has it, when is it due back, what's broken or missing,
and — critically — **can any of that be traced back to a decision and a
person?** Snipe-IT Lite is one workflow for all of it:

```
Request → Quote Created → Review → Approval → Fulfillment
        → Checkout / Custody → Return or Exception → Full Audit Trail
```

The quote-to-checkout pipeline isn't a bolted-on feature — it's the
core object model, so every asset handoff is traceable end to end:
what was requested, what was approved, and what was actually issued.

## Core Model

- **Asset Pool** — a type of asset, its total stock, and what's available
- **Checkout / Custody** — who has what, and when it's due back
- **Exception** — units under repair, missing, or otherwise unavailable
- **Quote** — the approval-driven request that gets fulfilled into a real checkout

```
Available = Total − Outbound(active checkouts) − Isolated(exceptions)
```

recalculated after every mutating action so the numbers can never
silently drift.

## Key Features

- **Asset inventory** — pools, categories, capacity, bulk CSV import,
  maintenance/exception tracking
- **Checkout & custody** — assign to staff, linked customers, or ad-hoc
  outsiders; due dates; returns; damaged/missing handling
- **Quote & approval workflow** — request → review → approve → fulfill,
  fully traceable
- **RBAC** — five roles (`super_admin`, `admin`, `manager`, `staff`,
  `customer`), enforced server-side on every request, not just hidden in the UI
- **Auditability** — an append-only log answers "who changed what, and
  when" for every mutating action
- **Administration** — user/role management, backup & restore, system
  maintenance controls, audit visibility

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
unmodified in every environment. (Render's free-tier build collapses
this into a single container, since Render's free plan has no
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
| Reverse proxy | nginx / Caddy, depending on deploy target |
| Cloud infra | Azure Container Apps (Bicep), single VM (Terraform), or Render free tier |
| CI/CD | GitHub Actions — lint, test, dependency/secret/image scanning, blue-green deploy |
| Security | Argon2id, JWT + MFA, rate limiting, CSP/security headers, Gitleaks |
| Testing | Pytest against real Postgres/Redis, frontend tests, load/chaos/resilience scripts |

## Engineering Highlights

**Database & migration discipline** — Alembic migrations, not
`create_all()`; CI exercises the full `upgrade`/`downgrade` chain, plus
a dedicated migration-safety check for locking schema changes.
Connection-pool budgeting, PgBouncer tuning, and admission control keep
background jobs from starving live API traffic.

**Secure by design** — Argon2id password hashing, JWT sessions with
MFA support, server-side RBAC (never just a hidden UI button), rate
limiting, account lockout, CSP/security headers, and an append-only
audit log, all backed by secrets that never touch the repo (see
`.gitleaksignore` / Gitleaks in CI).

**Production-oriented observability** — structured JSON logs, request
correlation IDs threaded across the API and background workers, and
opt-in OpenTelemetry distributed tracing. **ErrorBeacon**, a
self-built sibling service, aggregates exceptions with fingerprinting,
dedup, and spike/regression detection, then alerts over
Telegram with an email fallback and AI-assisted triage — designed
around one rule: *failure of the monitoring system should never cause
failure of the system being monitored.*

**Deployment & reliability** — the same codebase ships to Azure
Container Apps (Bicep), a Terraform-managed VM, or Render's free tier,
all through one CI/CD pipeline. Every path gets a real blue-green
canary rollout (10→25→50→75→100% traffic shift), gated by health
checks at each stage with automatic rollback — plus dedicated load
testing, chaos testing (killing Postgres/Redis mid-run), and
backup/restore validation, so reliability claims are actually exercised,
not just described.

## Quick Start

Requires Docker only — no local Python or Node.js.

```bash
git clone <repository-url>
cd asset-mgt-render-free

cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into JWT_SECRET_KEY

docker compose up --build
```

| | |
|---|---|
| App | http://localhost:8080 |
| API docs | http://localhost:8080/docs |

Demo accounts (seeded automatically on a fresh database — local/demo use only):

| Role | Username | Password |
|------|----------|----------|
| Admin | `r.adeyemi` | `Admin123!` |
| Manager | `s.chen` | `Manager123!` |
| Staff | `t.okafor` | `Staff123!` |
| Customer | `d.martins` | `Customer123!` |

## Project Structure

```
├── backend/          FastAPI app, SQLAlchemy models, Alembic migrations
├── frontend-app/      React + TypeScript frontend (dashboard, assets, quotes, reports)
├── frontend/          Legacy vanilla-JS frontend — frozen, see frontend/README.md
├── nginx/            Reverse proxy config (templated per environment)
├── infra/            Bicep — Azure Container Apps
├── infra-vm/          Terraform — single-VM deployment
├── scripts/           Blue-green deploy, health checks, load/chaos tests, backups
├── errorbeacon/       Custom exception-monitoring service (own app/tests)
├── docs/              Full technical manual and operational runbooks
└── .github/workflows/ CI, image builds, deploys, secret rotation
```

## Deployment

Three production-shaped targets, one deploy story — all driven from
GitHub Actions (`workflow_dispatch`, or a `git tag vX.Y.Z` push), no
manual `docker push` or SSH required:

- **Azure Container Apps** — `infra/main.bicep`; native multi-revision
  traffic splitting drives the canary rollout.
- **Single VM** — `infra-vm/` (Terraform) + Caddy for weighted
  blue-green traffic shifting; exposed via Cloudflare Tunnel (no inbound
  port opened on the VM).
- **Render (free tier)** — `render.yaml` + `Dockerfile.render`, a
  single combined image built to fit Render's free-plan constraints (no
  background-worker or private-service type available).

The production infrastructure is not required to run the app locally —
for a first look, `docker compose up --build` is all you need.

## Testing

```bash
docker compose exec backend pip install -r backend/requirements-dev.txt
docker compose exec backend pytest backend/tests
```

Runs against real Postgres/Redis service containers, not mocks. CI also
runs `ruff`, `pip-audit`, Gitleaks, and a Trivy image scan on every
push, plus dedicated scripts for load testing, chaos/dependency-failure
testing, and security-boundary testing beyond normal unit coverage.

---

## Documentation

This README is intentionally a concise, recruiter-friendly overview.
For the full technical manual — feature tour, environment variables,
database/migrations, tracing setup, API reference, and a "making
changes safely" guide — see **[`docs/README.md`](docs/README.md)**.

| Doc | Covers |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Condensed, task-oriented guide for AI coding agents — repo map, commands, conventions |
| [`docs/README.md`](docs/README.md) | Full technical manual — features, architecture, API reference, dev guide |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Azure Container Apps deploy guide |
| [`docs/DEPLOYMENT_VM.md`](docs/DEPLOYMENT_VM.md) | Single-VM deploy guide |
| [`docs/POST_DEPLOYMENT.md`](docs/POST_DEPLOYMENT.md) | Day-2 operations |
| [`docs/SRE_STRATEGY.md`](docs/SRE_STRATEGY.md) | Reliability & on-call posture |
| [`docs/DB_POOLING.md`](docs/DB_POOLING.md) | Connection pooling & PgBouncer |
| [`blue-green.md`](blue-green.md) | How zero-downtime rollout works |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |

---

## What This Project Is Really About

This started as an inventory tracker; the scope grew because the
interesting engineering problems all live *after* "it works": how a
schema change ships without downtime, how an API and a background
worker share a finite connection budget without starving each other,
how a bad deploy gets caught before it takes 100% of traffic, how
monitoring itself is built to fail safely, and how any of that survives
a dependency actually going down instead of just staying up in theory.

That's the mindset behind Snipe-IT Lite, and it's actively maintained
as a working demonstration of building, deploying, testing, monitoring,
and operating a production-style application — not just shipping one.
