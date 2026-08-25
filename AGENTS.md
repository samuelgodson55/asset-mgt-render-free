# AGENTS.md

Instructions for AI coding agents (and anyone skimming for the fast path)
working in this repository. Humans should still read
[`README.md`](README.md) and [`docs/README.md`](docs/README.md) for the
full picture — this file is the condensed, task-oriented version.

## What this is

**Snipe-IT Lite** — a self-hosted IT asset registry / equipment checkout
system: inventory, custody/checkout, quote-to-checkout approval workflow,
audit trail, and admin/reporting. FastAPI + PostgreSQL backend, a React
frontend, Celery/Redis for background jobs, and a self-built exception
monitoring service (ErrorBeacon). Ships to Azure Container Apps, a
Terraform-managed VM, or Render's free tier from one CI/CD pipeline, with
zero-downtime blue-green rollouts on every path.

## Repo map

```
backend/        FastAPI app — API routes, services, SQLAlchemy models, Alembic migrations, tests
frontend-app/    React + TypeScript + Vite — the ACTIVE frontend (see "Frontend" below)
frontend/        Legacy vanilla-JS + Tailwind frontend — FROZEN, see "Frontend" below
errorbeacon/     Separate FastAPI service: exception aggregation, Telegram/email alerting, AI triage
shared/          Code shared between backend/ and errorbeacon/ (currently: log/error sanitization)
nginx/           Reverse-proxy templates (one per frontend variant)
infra/           Bicep — Azure Container Apps
infra-vm/        Terraform — single-VM deployment
scripts/         Deploy, health-check, load/chaos/security test, and backup-related scripts
build-frontend/  Build tooling for the legacy frontend (minify/obfuscate per environment)
build-tailwind/  Tailwind build for the legacy frontend's CSS
.github/         CI, image build, deploy, and secret-sync workflows
docs/            Full technical manual + operational runbooks (see "Where to read more")
```

## Frontend: `frontend-app/` is active, `frontend/` is frozen

There are two mutually exclusive frontend builds. **Only one matters for
new work:**

- **`frontend-app/`** (React + TypeScript + Vite + Tailwind v4) — the
  default everywhere, and the only frontend that should receive new
  features, refactors, UX work, or new test coverage.
- **`frontend/`** (vanilla HTML/JS, pre-compiled Tailwind) — **frozen and
  going out of phase.** It stays in the repo, in CI, and deployable
  (`FRONTEND_BUILD_TARGET=legacy` / the `frontend-legacy-only` Docker
  target) only for environments that still run it. See
  [`frontend/README.md`](frontend/README.md). **Do not** add features,
  refactor, restyle, or add test coverage there. A genuine security fix or
  a build/deploy breakage is still in scope; anything else, don't spend
  time on it — point that effort at `frontend-app/` instead.

If a task doesn't specify which frontend, assume `frontend-app/`.

## Setup & running locally

Docker is the only hard requirement.

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # → JWT_SECRET_KEY in .env
docker compose up --build
```

- App: http://localhost:8080 — API docs (Swagger): http://localhost:8080/docs
- Demo logins are in `README.md`'s Quick Start table (local/demo DB only).
- `docker compose up --build` ships `frontend-app/` by default. To build the
  legacy site instead, set `FRONTEND_BUILD_TARGET=frontend-legacy-only` in
  `.env` (only do this if the task specifically concerns the legacy site).

Backend without Docker: see `docs/README.md`'s **Running Without Docker**
section — start Postgres only, create a venv, `pip install -r
backend/requirements.txt -r backend/requirements-dev.txt`, then
`uvicorn main:app --reload` from `backend/`.

## Build / lint / test commands

**Backend** (`backend/`):
```bash
# Inside the running compose stack (backend/Dockerfile ships a lean,
# test-dep-free image on purpose, so install them into the running
# container first — this doesn't persist across a rebuild):
docker compose exec backend pip install pytest pytest-asyncio httpx
docker compose exec backend pytest tests -v                # NOT backend/tests — see note below

# Natively from the repo root (faster iteration, closest match to CI):
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest backend/tests -v                                    # this is the CI-matching invocation

ruff check backend/                                         # lint, config in backend/ruff.toml
pip-audit                                                    # dependency vuln scan (CI also runs this)
```
`backend/Dockerfile` does `COPY backend/ /app/`, so inside the container
everything lands at `/app/` directly — the path is `tests`, not
`backend/tests`. From the repo root (native or `pytest.ini`-relative), it's
`backend/tests`. Mixing these up is the most common way to get a
confusing "no tests collected" or "file not found." See
`docs/LOCAL_TESTING_GUIDE.md` if either path doesn't work as expected.

Most of `backend/tests` runs against a throwaway SQLite DB per test — no
Docker needed. Two files need the real Postgres/Redis service containers:
`test_migrations.py` (real `alembic upgrade head`/`downgrade`) and
`test_redbeat_scheduling.py` (real RedBeat distributed lock). New
backend logic needs a real test in `backend/tests/`, not just a manual
check. Database changes go through Alembic — never hand-edit schema or use
`create_all()`; see `docs/README.md`'s **Database & Migrations** and
**Making Changes Safely** sections before touching `models.py`.

**`frontend-app/`** (Node 24, matching CI's `actions/setup-node@v7`):
```bash
cd frontend-app
npm ci
npm run lint     # oxlint
npm test         # vitest run (jsdom + Testing Library)
npm run build    # tsc -b && vite build — also catches type errors
npm run dev      # Vite dev server with hot reload
```

**`frontend/`** (legacy — only run these if you're actually touching this directory):
```bash
cd build-frontend && npm ci && cd ..
cd frontend/tests && npm ci && npm test
```

**`errorbeacon/`** (separate FastAPI service, own venv/requirements):
```bash
cd errorbeacon
pip install -r requirements-dev.txt
pytest tests
```

**Everything CI runs** is enumerated in `.github/workflows/ci.yml` — when
in doubt about whether something is required to pass, that file is the
source of truth. `docs/LOCAL_TESTING_GUIDE.md` walks through reproducing
all of it locally step by step.

## Run only the tests your change needs

**Do not run the full backend suite (or any full suite) by default.**
`backend/tests` alone is 300+ tests against real Postgres/Redis and gets
slow; running it, plus `frontend-app/`'s suite, plus `errorbeacon/`'s,
for a change that touches one file burns time and tokens for no signal.
CI itself only runs a suite when its path actually changed (see the
`changes`/path-filter job in `.github/workflows/ci.yml`) — match that
scoping locally:

- **Backend, one file or area:**
  `pytest backend/tests/test_checkouts_and_extensions.py -v`, or narrow
  further with `-k <pattern>`.
- **Frontend-app, one file:** `npm test -- src/path/to/Thing.test.tsx`
  (vitest accepts a path filter the same way).
- **Only touched `errorbeacon/`?** Run its suite, not the backend's —
  they're separate services with separate dependencies.
- **Only touched `frontend/` (legacy)?** Run only its own tests
  (`frontend/tests`) — don't also run `frontend-app/`'s.
- Run the **broader/full suite for the area you touched** once, near the
  end, as a final sanity check before calling the change done — not on
  every intermediate edit. Only reach for *every* suite across
  backend/frontend-app/frontend/errorbeacon together when the change is
  genuinely cross-cutting (e.g. a shared-contract or shared/ change) or
  when explicitly asked to.
- Skip `scripts/load-test.py`, `scripts/chaos-test.sh`,
  `scripts/security-boundary-test.py`, and `scripts/seed-large-dataset.py`
  unless the task specifically concerns resilience/load/security testing
  or you're asked to run them — they're slow, some are destructive
  against whatever DB they're pointed at, and none of them gate a normal
  change.
- `ruff check` / `npm run lint` / `npm run build` (type-check) are cheap —
  fine to run every time, unlike the test suites above.

## Conventions worth knowing before you edit

- **RBAC is server-side, always.** Five roles
  (`super_admin`/`admin`/`manager`/`staff`/`customer`). Never rely on the
  UI hiding a button — enforce permission checks in the API route/service.
- **Every mutating action is audited.** If you add a new mutating endpoint,
  wire it into the audit log the same way existing ones are (see
  `services/audit_service.py` and any existing API route for the pattern).
- **Migrations are additive-only** and reviewed, not blindly trusted from
  autogenerate — read the generated file in `backend/alembic/versions/`
  before applying it.
- **Structured logs, not `print()`** — use `logging_config.py`'s logger so
  correlation IDs (`request_id`) stay attached.
- **Secrets never enter the repo** — Gitleaks runs in CI
  (`.gitleaksignore` documents accepted false positives); don't add real
  secrets to `.env.example`, fixtures, or test data.
- **Work outward from the database, one layer at a time** for any
  non-trivial change: model/migration → schema → service → API route →
  frontend. `docs/README.md`'s **Making Changes Safely** section has
  worked examples for adding a field, an endpoint, and a page.
- **Config lives in `.env`/`config.py`, never hardcoded.** `.env.example`
  (and `errorbeacon/.env.example`) are the committed templates — real
  values go in the git-ignored `.env`, and any new setting needs a
  placeholder + comment added to the example file too.
- **Cheap pre-flight checks before you dig in:** `scripts/check-migration-
  safety.py` for anything touching `backend/alembic/versions/`, and
  `scripts/check-compose-drift.py` / `check-bicep-compose-drift.py` if
  you edit `docker-compose.yml`, `docker-compose.vm.yml`, or the Bicep
  files — all three are fast and catch exactly the kind of drift/mistake
  a full test run wouldn't surface.
- **Don't touch `infra/`, `infra-vm/`, or add a new deploy target** unless
  the task asks for it — same "don't expand scope silently" rule as the
  observability/frontend one at the bottom of this file.

## Where to read more

| Need | Doc |
|---|---|
| Full feature tour, architecture, file/function reference, API reference | [`docs/README.md`](docs/README.md) |
| Deploying to Azure Container Apps | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| Deploying to a single Azure VM | [`docs/DEPLOYMENT_VM.md`](docs/DEPLOYMENT_VM.md) |
| Day-2 ops after a fresh deploy (SMTP, backups, custom domain) | [`docs/POST_DEPLOYMENT.md`](docs/POST_DEPLOYMENT.md) |
| On-call posture, SLOs, alerting, runbooks | [`docs/SRE_STRATEGY.md`](docs/SRE_STRATEGY.md) |
| Security/CVE-hygiene gap register (priority-scored backlog) | [`docs/SECURITY_GAPS.md`](docs/SECURITY_GAPS.md) |
| Connection pooling / PgBouncer | [`docs/DB_POOLING.md`](docs/DB_POOLING.md) |
| Distributed tracing (OpenTelemetry) | [`docs/TELEMETRY.md`](docs/TELEMETRY.md) |
| Exception monitoring (ErrorBeacon) coverage | [`docs/ERRORBEACON_COVERAGE.md`](docs/ERRORBEACON_COVERAGE.md), [`errorbeacon/README.md`](errorbeacon/README.md) |
| Reproducing CI locally | [`docs/LOCAL_TESTING_GUIDE.md`](docs/LOCAL_TESTING_GUIDE.md) |
| Zero-downtime blue-green rollout mechanics | [`blue-green.md`](blue-green.md) |
| Release history | [`CHANGELOG.md`](CHANGELOG.md) |

## PR / change hygiene

- Add or update tests for behavior you change — don't rely on manual
  verification alone.
- Add a `CHANGELOG.md` entry for user-visible or operationally relevant
  changes, following the existing `### Highlights` / `### Added` /
  `### Changed` / `### Fixed` structure. New entries go at the
  `<!-- release-notes-insertion-point -->` marker near the top, above the
  most recent version — don't hand-pick a version number; leave that to
  whoever cuts the release unless asked otherwise.
- Skip the heavy `scripts/*-test.py`/`.sh` tools (load, chaos, security-
  boundary, correlation, session-redis, etc.) and full multi-suite test
  runs unless the task is actually about resilience/load/security or you
  were asked — see **Run only the tests your change needs** above.
- If a change affects both frontends' shared contract (API shape,
  auth/session behavior), check `frontend-app/` — the legacy frontend
  should be touched only if it's already broken by the change and only to
  the minimum needed to keep it building/running (see "Frontend" above).
- Don't introduce a new observability stack, a new frontend, or a new
  deployment target without being asked — `docs/SRE_STRATEGY.md`'s §8
  ("What to deliberately not do") captures the deliberate scope limits
  already in place, and they extend to agent-driven changes too.
- If a change touches CI workflows, `dependabot.yml`, Dockerfiles, or
  auth/session/CORS/header code, check `docs/SECURITY_GAPS.md` first —
  it's a live-tracked list of known coverage gaps, and a change in those
  areas is exactly where one of those gaps tends to get either fixed or
  accidentally widened.
