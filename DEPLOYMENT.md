# Deploying Snipe-IT Lite To Production

This is the companion to `README.md`, focused entirely on taking this app
from "runs on my laptop" to "runs reliably for real people, safely, and can
handle a rush of traffic." `README.md` still owns the feature tour, local
Quick Start, and file/function reference — this document assumes you've
already read its **Deploying Across Environments (nginx Reverse Proxy)**
and **Running In Production** sections and want the deeper how-to on three
things specifically:

1. **Safety** — the concrete steps to take before this is reachable by
   anyone outside your own machine.
2. **Setup** — how the pieces (Postgres, Redis, `backend`, `worker`,
   `beat`, `frontend`/nginx) fit together in a real deployment.
3. **Load balancing** — how to run more than one `backend`/`worker`
   instance at once (e.g. during a busy checkout period at the start of a
   semester/quarter), and everything that had to be made safe for that.

If you're deploying to Render's free tier, most of this doesn't apply —
that path is intentionally a single, non-scaled container (see
`render.yaml`'s top-of-file comment). This guide is for a real
docker-compose-based (or equivalent container-orchestrator) deployment:
your own VM/server, a cloud provider's container service, Kubernetes, etc.

**Deploying to Azure Container Apps?** Everything below still applies
conceptually (safety checklist, migration ordering, scaling shape, backup
strategy) — jump straight to
[Azure Container Apps Production Deployment (Cost-Optimized)](#azure-container-apps-production-deployment-cost-optimized)
for the fully automated, Azure-native version of this same pipeline
(`infra/main.bicep` + `.github/workflows/deploy-azure-*.yml`).

---

## Table of Contents

- [Before You Deploy: Safety Checklist](#before-you-deploy-safety-checklist)
- [Production Setup](#production-setup)
- [Load Balancing & Scaling For Peak Use](#load-balancing--scaling-for-peak-use)
- [Speed: Background Workers Use Disk, Not RAM](#speed-background-workers-use-disk-not-ram)
- [Health Checks & Monitoring](#health-checks--monitoring)
- [Backups & Disaster Recovery](#backups--disaster-recovery)
- [Rolling Out Updates Without Downtime](#rolling-out-updates-without-downtime)
- [Azure Container Apps Production Deployment (Cost-Optimized)](#azure-container-apps-production-deployment-cost-optimized)
  - [Versioning & Cutting a Release](#versioning--cutting-a-release)
  - [Rollback](#rollback)
- [Troubleshooting](#troubleshooting)

---

## Before You Deploy: Safety Checklist

Go through every item below before this app is reachable from outside your
own machine. Most of these are also called out inline in `.env.example` and
`README.md`'s **Running In Production** section — they're repeated here
because they're the ones most likely to bite you specifically in a
multi-instance/production setup.

- **`ENVIRONMENT=production`** in your real `.env`. This isn't cosmetic —
  `config.py` uses it to refuse to boot at all if `JWT_SECRET_KEY` is
  still a placeholder/weak value. Treat a startup crash here as the app
  protecting you, not a bug.
- **Generate real secrets.** `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`,
  and (optionally) `ROOT_ADMIN_BOOTSTRAP_PASSWORD` — none of these should
  be the values shipped in `.env.example`. Generate a real JWT secret
  with:
  ```bash
  python3 -c "import secrets; print(secrets.token_hex(32))"
  ```
  There is deliberately no standing `SUPER_ADMIN_PASSWORD` env var —
  `config.py`'s comment on `SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_NAME`
  explains why: the root admin's password is a normal database-backed
  hash, set once by `alembic/versions/0002_bootstrap_root_admin.py`
  (either from `ROOT_ADMIN_BOOTSTRAP_PASSWORD` if you set it, or a
  randomly generated one printed to stderr exactly once if you don't —
  see README's "Viewing the one-time-generated root admin password"),
  then rotated afterward the same way any other account's password is.
  Every `backend`, `worker`, and `beat` replica must be given the exact
  SAME `JWT_SECRET_KEY` — if they ever drift apart, tokens issued by one
  replica will fail to verify against another, which (with a load
  balancer in front) looks like random, intermittent "please log in
  again" failures instead of an obvious full outage.
- **Never commit `.env`.** It's already in `.gitignore` — keep it that
  way. Inject secrets via your platform's own secret manager (Render
  environment groups, Docker Swarm/Kubernetes secrets, your cloud
  provider's secret store) rather than baking them into the image or a
  committed file.
- **`AUTO_INIT_DB=false` and `AUTO_SEED_DEMO_DATA=false`.** Run
  `alembic upgrade head` as its own explicit, one-time deploy step (see
  README's **Database & Migrations** section) instead of letting a
  container's own boot sequence touch schema. This matters even more with
  multiple `backend` replicas — you do NOT want N replicas each racing to
  run migrations against the same database on startup. Only the demo
  seed data should ever be disabled in production regardless of replica
  count; it creates publicly-known demo credentials.
- **Lock `CORS_ORIGINS` down** to your real frontend domain(s) only —
  never leave the localhost defaults in a real deployment.
- **`ENABLE_API_DOCS=false`** for both the `backend` and `frontend`/nginx
  services (same `.env` key drives both). Verify it by requesting `/docs`
  and `/openapi.json` against your deployed URL — both should 404.
- **Terminate TLS in front of nginx.** This app's own nginx layer (see
  `nginx/default.conf.template`) does not terminate HTTPS itself. Put a
  managed load balancer, reverse proxy, or cert-manager setup in front of
  it. This also matters for load balancing: your TLS-terminating layer is
  usually also where you'd put an external load balancer if you ever scale
  beyond what a single nginx container can push through (see the load
  balancing section below for the more common case of scaling `backend`/
  `worker` behind the SAME nginx instance, which doesn't require this).
- **Review rate limiting and lockout thresholds** —
  `LOGIN_RATE_LIMIT_MAX`/`LOGIN_RATE_LIMIT_WINDOW_SECONDS` and
  `ACCOUNT_LOCKOUT_MAX_ATTEMPTS`/`ACCOUNT_LOCKOUT_DURATION_MINUTES` — for
  your actual expected traffic. The login rate limiter is Redis-backed
  (shared across every `backend` replica — see
  `middleware/rate_limit.py`), so these thresholds now mean what they say
  regardless of how many replicas you run; that wasn't true before.
- **Back up the Postgres volume itself**, separately from this app's own
  application-level `pg_dump` backups (see
  [Backups & Disaster Recovery](#backups--disaster-recovery) below) — a
  managed Postgres provider usually handles this for you.
- **Set a real `BACKUP_GDRIVE_*`** (or equivalent off-box) backup
  destination if you're running this on any host without its own
  durable, snapshotted disk — local `BACKUP_DIR` files alone don't
  survive a lost volume.

## Production Setup

The shape of a real deployment is the same six services
`docker-compose.yml` already defines locally:

| Service    | What it does                                              | Scale it? |
|------------|-------------------------------------------------------------|-----------|
| `db`       | Postgres — the source of truth                              | No (use a managed/replicated Postgres instead of scaling this container yourself) |
| `redis`    | Celery broker/result backend, rate-limit counters, locks     | No (use a managed/clustered Redis for real HA; a single container is a single point of failure) |
| `backend`  | FastAPI app — everything under `/api/*`                      | **Yes** — see below |
| `worker`   | Celery worker — generates CSV/PDF exports out-of-band        | **Yes** — see below |
| `beat`     | Celery Beat — fires the overdue/due-soon notification digest | **No, never** — see below |
| `frontend` | nginx — serves the static site and reverse-proxies `/api/*`  | Only with an external load balancer in front (see caveat below) |

Bring the whole stack up the same way you would locally:

```bash
cp .env.example .env
# fill in real secrets -- see the safety checklist above
docker compose up --build -d
docker compose exec backend alembic upgrade head
```

The one thing genuinely different from local dev is what sits in FRONT of
`frontend`: a real TLS-terminating load balancer/proxy, rather than a
browser hitting port 8080 directly.

## Load Balancing & Scaling For Peak Use

This section covers spinning up multiple `backend`/`worker` instances —
e.g. during a semester/quarter's equipment checkout rush — and everything
that had to be made instance-count-safe for that to actually work
correctly rather than just *appear* to work.

### Scaling up

```bash
# Handle more concurrent API traffic:
docker compose up -d --scale backend=3

# Handle more concurrent CSV/PDF export jobs:
docker compose up -d --scale worker=3

# Scale both together for a known busy period, then back down once it passes:
docker compose up -d --scale backend=3 --scale worker=3
docker compose up -d --scale backend=1 --scale worker=1
```

No code changes or redeploys are needed to do this — it's a pure capacity
knob. A few notes:

- **`backend` and `worker` no longer have a fixed `container_name`** in
  `docker-compose.yml` (they used to) — a fixed name can only ever belong
  to one running container, which blocked `--scale` from starting a
  second replica at all. Compose now names each replica automatically
  (`snipeit-lite-backend-1`, `-2`, ...).
- **`db`, `redis`, and `frontend` are NOT meant to be scaled this way.**
  Scaling `frontend` with `--scale` would try to bind the same host port
  (`8080:80`) from multiple containers and fail outright — if you
  eventually need more nginx throughput than one container can push,
  that's a job for an external load balancer with its own TLS
  termination in front of multiple `frontend` replicas on a port range,
  which is a bigger infrastructure decision than this project makes for
  you. For nearly all real-world load on an internal IT-asset tracker,
  scaling `backend`/`worker` behind a single nginx is more than enough.

### How requests actually get spread across replicas

`nginx/default.conf.template` doesn't hardcode the backend's IP — it
resolves the `backend` hostname through Docker's embedded DNS
(`resolver ${RESOLVER_IP} valid=10s;`) on every single request rather than
caching one IP at startup. When `backend` has multiple replicas, Docker's
embedded DNS rotates which container IP it hands back on successive
lookups, so requests naturally spread across all running replicas — no
nginx `upstream {}` block or extra load-balancer config needed for this
piece. This was already true before this change; it's what made scaling
`backend` safe to turn on with nothing more than the `--scale` flag above.

### Why this used to NOT be safe, and what changed

Three things silently broke (or would have) under multiple replicas
before this round of changes — each is now fixed:

1. **Login rate limiting was in-memory, per-process.** Each `backend`
   replica kept its own separate counter of login attempts per IP, so an
   attacker spread across N replicas effectively got `N ×` the intended
   limit, and a real user could get rate-limited on one replica while a
   fresh allowance sat unused on another. **Fixed:** `middleware/rate_limit.py`
   now keeps counts in Redis (`INCR`/`EXPIRE` on a windowed key), shared
   by every replica. See that file's module docstring for the full
   design, including the deliberate "fail open if Redis is briefly
   unreachable" behavior.
2. **The daily scheduled backup would have run once PER REPLICA.** The
   backup scheduler is a plain daemon thread inside the `backend` process
   (see `services/backup_service.py`) so it keeps working without any
   Celery/Redis dependency — but that also meant every replica would
   independently wake up at the scheduled hour and each run its own full
   `pg_dump`. **Fixed:** `_acquire_scheduled_backup_lock()` takes a
   short-lived Redis lock (`SET key value NX EX 300`) before firing —
   only the one replica that wins the lock for that scheduled run
   actually executes the backup; every other replica logs that it was
   skipped and moves on.
3. **Celery Beat was embedded inside the `worker` process (`-B` flag).**
   Scaling `worker` would have meant every replica ran its own Beat
   scheduler, each firing the overdue/due-soon notification digest tasks
   independently — duplicate emails to every checkout holder, once per
   `worker` replica. **Fixed:** Beat now runs as its own dedicated `beat`
   service, entirely separate from `worker`. **Never scale `beat` past one
   replica** — there's no lock protecting it the way the backup scheduler
   has one, because the correct fix for a periodic scheduler is "exactly
   one process runs it," not "let many race for a lock every time." This
   is exactly why `beat` is its own service now instead of a flag on
   `worker`: it decouples "how many workers process export jobs" from
   "how many things fire the notification schedule."

### No sticky sessions needed

Auth is a JWT bearer token (see `security.py`/`deps.py`) verified
independently by whichever replica happens to receive a request — there's
no server-side session state a load balancer would need to route
consistently back to the same instance. Any replica can validate any
valid token from any other replica (as long as they share the same
`JWT_SECRET_KEY` — see the safety checklist above), so plain round-robin
distribution is sufficient.

### Shared volumes multiple replicas depend on

Both `backup_data` and `export_data` (see docker-compose.yml) are
named volumes mounted into every `backend`/`worker` replica — this is
what lets any replica serve a backup or export file that a DIFFERENT
replica actually created:

- `backup_data` (`/app/backups`) — written by whichever replica's backup
  scheduler wins the lock above (or whichever replica a person happens to
  hit when clicking "Create Backup Now"); read by whichever replica
  serves a later download/restore request.
- `export_data` (`/app/export_results`) — written by whichever `worker`
  replica picks up a given export job; read by whichever `backend`
  replica serves the eventual download request. See the next section for
  why this moved to disk in the first place.

**Known remaining limitation:** `backup_service.py`'s `index.json`
(the small metadata file listing existing backups) is protected by an
in-process `threading.Lock`, which only guards against two requests
colliding within the SAME replica — it does not stop two DIFFERENT
replicas from writing it at the exact same instant. In practice this is
low-risk (manual backup creation is an infrequent, human-triggered
action, and the scheduled path is already serialized by the Redis lock
above), but if you scale `backend` heavily and see backup-listing
oddities, moving backup metadata into a real database table instead of a
shared JSON file is the natural next fix.

## Speed: Background Workers Use Disk, Not RAM

CSV/PDF export generation (the audit ledger export) runs on the `worker`
service, out-of-band from the request/response cycle — see
`backend/tasks/export_tasks.py` and `backend/celery_app.py`. This used to
return the finished file's entire contents as the Celery task result:
base64-encoded bytes, stashed by Celery in Redis (an **in-memory**
datastore) for up to `EXPORT_RESULT_TTL_SECONDS`.

That's fine for one person occasionally clicking "export." It gets
expensive fast under real, concurrent load:

- Base64 inflates the payload by roughly a third on top of the file's
  real size.
- A wide-date-range audit-ledger PDF can already be tens of megabytes on
  its own.
- Every one of those bytes sat in Redis's RAM for the FULL TTL window
  (an hour, by default), whether or not anyone had actually downloaded
  the file yet — and Redis is also what the Celery job queue and the
  rate limiter both depend on, so bloating its memory usage slows down
  everything else sharing that same Redis instance too.
- Once `worker` is scaled to multiple replicas to handle a peak (see
  above), several large exports can easily be in flight and expiring
  concurrently.

**The fix:** `generate_audit_export` now writes the finished file to plain
disk, under `settings.EXPORT_RESULT_DIR` (a directory on the shared
`export_data` volume — see above), and returns only a small JSON dict
(filename, content type, and the file's path) as the actual Celery/Redis
result. Redis goes back to holding kilobytes instead of megabytes per
export. `GET /audit-logs/export/{task_id}/download` (see
`backend/api/audit_api.py`) now streams the file straight off disk via
FastAPI's `FileResponse` instead of base64-decoding it out of a Redis
blob.

A lightweight sweep (`_sweep_expired_exports()`) runs at the start of
every new export job and deletes any file on disk older than
`EXPORT_RESULT_TTL_SECONDS` — there's no separate always-on cleanup
schedule needed just for this; disk usage stays bounded as a side effect
of normal export traffic.

**If you want this to go further:** for very high-volume exporting, the
same `EXPORT_RESULT_DIR` approach extends naturally to a mounted
object-storage bucket (S3/GCS/etc., FUSE-mounted or swapped in behind
the same `open()`/`FileResponse` calls) instead of a plain Docker volume —
useful if you ever run `worker`/`backend` on separate hosts entirely
rather than the same Docker Compose network.

## Health Checks & Monitoring

- `GET /healthz` (see `backend/main.py`) returns a simple liveness check —
  no DB dependency, just "is the process up and answering HTTP." Point
  your orchestrator's *liveness* health check / load balancer target
  group at it for each `backend` replica.
- `GET /readyz` (see `backend/main.py` and `database.py`'s
  `get_schema_status()`) is the separate *readiness* check — it queries
  the database and compares its current Alembic revision against what
  this build of the code expects, returning `503` (not `500`) until they
  match. Point your orchestrator's *readiness* probe here instead of
  `/healthz` — a liveness failure kills and restarts the container, which
  is the wrong response to "migrations haven't finished yet" or "the DB
  had a brief blip," while a readiness failure just holds traffic back
  from that replica until the next poll succeeds. `infra/main.bicep`
  wires these up exactly this way for `backend` on Azure Container Apps
  (`Liveness` → `/healthz`, `Readiness` → `/readyz`), which is what lets
  a rolling deploy hold traffic back from a new revision until it's
  actually ready, not just alive — see [Zero-downtime rollout
  mechanics](#zero-downtime-rollout-mechanics).
- `db` and `redis` already have `healthcheck:` blocks in
  `docker-compose.yml` that `backend`/`worker`/`beat` all `depends_on:
  condition: service_healthy` — a fresh `docker compose up` won't start
  the app tier racing against a Postgres/Redis that isn't ready yet.
- `backend/Dockerfile` and `frontend/Dockerfile` now each carry their own
  image-level `HEALTHCHECK` instruction (`backend` hits `GET /healthz`
  via Python's stdlib — no curl/wget in that slim image; `frontend` hits
  `GET /` via BusyBox `wget` — nginx:alpine ships it). Docker Compose
  automatically uses a service's image `HEALTHCHECK` unless the service
  overrides it, so `docker compose ps`/`docker ps` now report `backend`
  and `frontend` as `healthy`/`unhealthy`, not just `running` — the same
  signal `db`/`redis` always had, extended to the rest of the stack.
  `frontend`'s own `depends_on: backend: condition: service_healthy`
  relies on this: nginx won't start proxying `/api/*` until `/healthz`
  is genuinely answering `200`, not merely until the backend process has
  started.
  - `worker` and `beat` build from the SAME image as `backend`
    (`build: ./backend`) and would otherwise inherit that same
    HTTP-based check — but neither serves HTTP on port 8000, so
    `docker-compose.yml` overrides it per-service: `worker` gets the
    standard `celery -A celery_app inspect ping` (round-trips a real
    control command through the same Redis broker it consumes from);
    `beat` explicitly disables the inherited check (`healthcheck:
    disable: true`) — there's no equivalent liveness probe for a
    RedBeat-scheduled Beat process (its schedule lives in Redis, not a
    local `celerybeat-schedule` file to watch), so it correctly falls
    back to `restart: unless-stopped` for crash recovery, same as it
    always has.
- A global "unhandled exception" safety net
  (`backend/middleware/error_handling.py`) now catches anything that
  isn't already a deliberate `HTTPException` anywhere in the app,
  guaranteeing every 500 — not just the ones an endpoint explicitly
  raises itself — gets a full traceback in the logs (tagged with
  `request_id`, same as every other log line — see the structured
  logging bullet below) AND a `{"detail": ..., "request_id": ...}`
  response body the frontend/support agent can actually correlate back
  to that log line. Registered as the innermost middleware layer
  (deliberately NOT `@app.exception_handler(Exception)` — see that
  file's module docstring for why that alternative would have silently
  dropped CORS headers from every unhandled 500) so CORS/security
  headers still apply exactly as they would to any other response.
- Structured JSON logging is already wired up (`LOG_LEVEL`/`LOG_FORMAT` —
  see `backend/logging_config.py`) with a correlation ID
  (`X-Request-ID`) threaded from nginx through to every backend log line
  for a given request — this is what lets you trace one request across
  whichever replica happened to handle it.
- Watch Redis memory usage after adopting the disk-backed export change
  above — it should now track "a handful of small job-result keys and
  rate-limit counters," not file sizes. A sudden jump back up is a good
  signal something reverted to the old in-Redis behavior.

## Backups & Disaster Recovery

This app's own `pg_dump`-based backups (see README's **Backups** section
and `services/backup_service.py`) are a convenience layer for
"restore to a few minutes ago without leaving the dashboard" — they are
**not** a substitute for your infrastructure provider's own Postgres
backup/point-in-time-recovery story. Use both:

- Enable `BACKUP_GDRIVE_ENABLED` (or point `EXPORT_RESULT_DIR`/
  `BACKUP_DIR` at durable, off-box storage) so this app's own backups
  survive a lost container/volume, not just a container restart.
- Separately, make sure your actual Postgres host/provider has its own
  backup and point-in-time-recovery configured — this app has no way to
  do that for you from inside its own containers.

## Rolling Out Updates Without Downtime

With `backend` scaled to more than one replica, you can update the app
without a hard outage:

```bash
docker compose build backend worker beat
docker compose up -d --no-deps --scale backend=3 backend
docker compose up -d --no-deps worker beat
```

Compose replaces replicas one at a time by default; as long as you always
keep at least one healthy `backend` replica up, nginx keeps routing to
whichever replicas are currently answering. Run `alembic upgrade head` as
its own explicit step (not on container boot — see the safety checklist)
BEFORE rolling out backend code that depends on a new column/table, same
as in a single-instance deployment.

## Azure Container Apps Production Deployment (Cost-Optimized)

This is the **primary production target**: Azure Container Apps (ACA), fully
automated end to end, and deliberately built to be the **cheapest realistic
way to run this app on Azure** while keeping full functionality, real
autoscaling, and a fast, zero-downtime deploy pipeline. Push to `develop`
and Staging updates itself. Production works differently: **a pushed
`git tag v1.x.x` is what triggers a production release** — merging to
`main` alone does *not* deploy anything by itself anymore (see
[Versioning & Cutting a Release](#versioning--cutting-a-release) below for
the full walkthrough). Nothing manual after the one-time setup below either
way. The pipeline lives in `.github/workflows/` (`ci.yml`, `infra-deploy.yml`,
`deploy-azure-staging.yml`, `release.yml`, `deploy-azure-production.yml`)
and the infrastructure lives in `infra/main.bicep`.

> **If you deployed an earlier version of this architecture** — either the
> original managed-services design (Flexible Server + Azure Cache + ACR +
> Key Vault + 4 Container Apps) or the interim single-`app` cost-optimized
> version (one combined container + `db` + `redis`) — this section
> describes the current shape, not an incremental change from either. See
> `infra/main.bicep`'s top-of-file comment for what changed and why each
> change is safe at this scale.

### The shape: `frontend`, `backend`, `db`, `redis`

`infra/main.bicep` provisions **four** Container Apps, split so `frontend`
and `backend` can scale independently instead of being coupled to the same
replica count:

| Service | What it is | Public? | Scaling |
|---|---|---|---|
| `frontend` | `frontend/Dockerfile`, UNCHANGED from local Docker Compose — serves the static build, reverse-proxies `/api/*` to `backend` | Yes — the only public entry point | 0-N, independent of `backend` |
| `backend` | FastAPI + embedded Celery worker/beat (`backend/Dockerfile`) | No — internal-only | 0-N, independent of `frontend` |
| `db` | `postgres:16-alpine`, official Docker Hub image | No — internal-only | Pinned to 1 |
| `redis` | `redis:7-alpine`, official Docker Hub image | No — internal-only | Pinned to 1 |

Plus one **Container Apps Job** (`migrate`) that runs `alembic upgrade head`
against `backend`'s own image, on demand — not a fifth standing service.

**Why `frontend` and `backend` are split, not combined:** a burst of pure
asset-browsing traffic used to scale up the same container that also ran
the embedded Celery worker, even with zero real API calls happening — and
vice versa. Splitting them means each scales to its *own* actual load.

**Why `frontend` reuses `frontend/Dockerfile` completely unmodified, rather
than becoming a fully separate origin:** `frontend/js/api.js` hardcodes
`API_URL = '/api'` as a relative path, and every request sends
`credentials:'include'` (cookie-based auth) — both only work same-origin.
nginx is the traffic cop that keeps that true: the browser only ever talks
to `frontend`'s one public origin; nginx quietly reverse-proxies `/api/*`
to `backend` over the Container Apps environment's internal DNS
(`nginx/default.conf.template`'s `BACKEND_HOST`/`BACKEND_PORT` env vars —
resolver auto-detected at boot, see
`nginx/docker-entrypoint.d/15-detect-resolver-ip.sh`). Zero frontend code
changes were needed for any of this.

> **Considered and deliberately not used: Azure Static Web Apps for
> `frontend`.** SWA's Free tier is genuinely free and would be a natural
> fit for a static frontend — except linking a Container Apps backend
> through SWA's reverse proxy (what would keep `/api/*` same-origin) is a
> **Standard-plan-only** feature (~US$9/month), not available on Free. The
> Free tier alone would mean the browser calling `backend` on a different
> origin, which breaks the cookie-based auth this app uses today (would
> require moving to Bearer tokens, a real security-relevant code change).
> `frontend` staying a Container App gets equivalent real-world cost
> (scale-to-zero + the Consumption plan's free monthly grant typically
> lands this under a few dollars a month) with zero code risk. Revisit this
> only if you're prepared to also do the Bearer-token auth migration.

**Why `redis` is still here, not just three services:** once `backend`
scales past 1 replica, it needs a *shared* Celery broker (all replicas'
embedded workers pull from the same queue, not N independent ones), a
shared cross-replica login rate limiter, and a shared backup-job leader
lock (so N replicas don't all run pg_dump at 3am simultaneously) — all
three already built into this codebase, all three requiring Redis. Cutting
Redis would mean pinning `backend` to exactly 1 replica forever (no real
autoscaling) or silently breaking all three the first time it scales past
1. It's a small container (0.25 vCPU/0.5 GiB, no persistent volume, same
"resets on restart" trade as before) — cheap insurance for correctness.

**Zero application code changes were needed for the backend/db/redis
split** — `db` and `redis` communicate with `backend`/`migrate` over the
exact same `DATABASE_URL`/`REDIS_URL` env vars `config.py`/`celery_app.py`
already read; only the values differ (see `.env.azure.example`).

`backup_data` and `export_data` (Compose's named volumes) are Azure Files
shares, mounted into `backend` at the exact same paths (`/app/backups`,
`/app/export_results`). Postgres's own data directory is ALSO an Azure
Files share, mounted into `db` — this is what makes it safe to
restart/redeploy `db` without losing data.

### Docker Hub instead of Azure Container Registry

TWO custom images now — `<you>/snipeit-lite-backend` (from
`backend/Dockerfile`) and `<you>/snipeit-lite-frontend` (from
`frontend/Dockerfile`) — both pushed to **Docker Hub**, not Azure Container
Registry (ACR's cheapest tier still has a fixed monthly floor whether or
not you ever push an image).

- **Default: both public repositories.** Zero registry credentials
  anywhere in this deployment. Neither image contains secrets (JWT key, DB
  passwords, etc. are all injected as Container Apps secrets at runtime,
  never baked into either image).
- **Optional: private repositories.** Docker Hub's free plan includes
  exactly **one** private repo — if you want both `backend` and `frontend`
  private, you'll need a paid Docker Hub plan, or keep one of the two
  public. Set `dockerHubUsername`/`dockerHubToken` (a Docker Hub Personal
  Access Token) when deploying `infra/main.bicep` either way — the same
  credentials authenticate pulls for both images (one Docker Hub account),
  and also help you avoid Docker Hub's anonymous per-IP pull rate limit
  even on public repos.

### One-time setup

1. **Create a Docker Hub account and access token** (Docker Hub → Account
   Settings → Security → New Access Token, "Read & Write" scope).

2. **Register the Azure resource providers `infra/main.bicep` needs** (one
   time, per subscription):
   ```bash
   az provider register --namespace Microsoft.App
   az provider register --namespace Microsoft.OperationalInsights
   az provider register --namespace Microsoft.Storage
   ```
   (No `Microsoft.ContainerRegistry`, `Microsoft.DBforPostgreSQL`,
   `Microsoft.Cache`, or `Microsoft.KeyVault` registration needed.)

3. **Create the two resource groups** (or let `infra-deploy.yml` create them
   on first run):
   ```bash
   az group create --name rg-snipeit-lite-staging --location eastus
   az group create --name rg-snipeit-lite-prod --location eastus
   ```

4. **Set up OIDC federated login** for GitHub Actions: an Azure AD App
   Registration, Contributor on both resource groups, a federated
   credential per environment. Full steps:
   [Azure docs: Connect GitHub Actions to Azure](https://learn.microsoft.com/azure/developer/github/connect-from-azure).

5. **Add GitHub repository secrets** (Settings → Secrets and variables →
   Actions), split across two GitHub **Environments** (`staging`,
   `production`) where the value differs, repo-level where it doesn't:

   | Secret | Scope | Notes |
   |---|---|---|
   | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | per-environment | From the App Registration in step 4 |
   | `AZURE_LOCATION` | repo | e.g. `eastus` |
   | `STAGING_RESOURCE_GROUP` / `PROD_RESOURCE_GROUP` | repo | The two resource group names from step 3 |
   | `DOCKERHUB_USERNAME` | repo | From step 1 |
   | `DOCKERHUB_TOKEN` | repo | From step 1 |
   | `POSTGRES_PASSWORD` | per-environment | Generate with `openssl rand -hex 16`, different per environment |
   | `REDIS_PASSWORD` | per-environment | Same, different per environment |
   | `JWT_SECRET_KEY` | per-environment | Generate with `openssl rand -hex 32` |
   | `ROOT_ADMIN_BOOTSTRAP_PASSWORD` | per-environment | Optional — the root admin's initial password. Leave unset to have `0002_bootstrap_root_admin.py` generate a random one and print it once instead (see README's "Viewing the one-time-generated root admin password"). Note: the root admin's username/display name (`SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_NAME`) aren't wired as GitHub secrets at all here — `infra/main.bicep` hardcodes them to `superadmin`/`Super Admin`; edit the bicep file directly if you want different values. |
   | `CUSTOM_DOMAIN` | per-environment | Optional — leave unset to use the generated `*.azurecontainerapps.io` FQDN |
   | `ALERT_EMAIL_ADDRESS` | per-environment | Optional — leave unset to skip creating any alerting resources (no cost, no action group). Set it to wire up the three Azure Monitor scheduled query alerts (backend error-rate spike, `/readyz` failing, daily backup missing) from `infra/main.bicep` to that address — see [SRE_STRATEGY.md](SRE_STRATEGY.md) section 2. |

6. **Run `infra-deploy.yml` manually once per environment** (Actions tab →
   "Deploy Azure Infrastructure" → Run workflow → choose `staging`, then run
   it again for `production`). This provisions everything: Log Analytics,
   Storage + Azure Files shares, the Container Apps Environment, and all
   four Container Apps (`backend`/`frontend` start on a placeholder
   `latest` tag — the next step gives them real images).

7. **Allow Actions to open pull requests** (Settings → Actions → General →
   Workflow permissions → check **"Allow GitHub Actions to create and
   approve pull requests"**, then Save). This is **off by default** on
   every repo and easy to miss — without it, `release.yml`'s `changelog`
   job (which opens the `CHANGELOG.md` PR against `main` using the default
   `GITHUB_TOKEN`) fails with a 403 the first time you push a tag. Nothing
   else in this pipeline needs this setting — only that one PR-creation
   step.

8. **(Recommended) Turn on squash merging, off everything else**
   (Settings → General → Pull Requests → uncheck "Allow merge commits" and
   "Allow rebase merging", check "Allow squash merging", and check "Default
   to pull request title for squash merge commits"). Not required for
   anything to function, but it means each PR you merge into `develop`/
   `main` becomes exactly one commit — which keeps the changelog entries
   `release.yml` generates from `git log <prev-tag>..<tag>` (one bullet per
   commit) one bullet per feature/fix instead of one bullet per intermediate
   commit inside the branch.

9. **Push to `develop`** for Staging, or **push a `git tag v1.x.x`** off
   `main` for Production — either builds real images, pushes them to Docker
   Hub, runs migrations, and rolls them out (`deploy-azure-staging.yml` for
   the former; `release.yml` → `deploy-azure-production.yml` for the
   latter — see [Versioning & Cutting a Release](#versioning--cutting-a-release)
   below for the full tagging walkthrough). From here on, deploys are just
   `git push`/`git push --tags`.

### Versioning & Cutting a Release

This project uses plain [semantic versioning](https://semver.org/) tags —
`vMAJOR.MINOR.PATCH` — as the trigger for a production release. Nothing
else about a tag's format is special-cased anywhere in the pipeline; `v1`,
`1.4.2` (no `v`), or `v1.4.2-rc1` will NOT match `release.yml`'s
`push: tags: ['v*.*.*']` trigger (or will match in ways you don't want —
`v1.4.2-rc1` does match `v*.*.*` and WILL cut a real production release, so
don't use pre-release-looking tags unless you mean it).

**Your day-to-day workflow, feature branch to production:**

1. Branch off `develop`, do the work, open a PR into `develop`. Only
   `ci.yml` runs on the feature branch itself — no deploy, no versioning.
2. Merge that PR into `develop` → `deploy-azure-staging.yml` builds and
   rolls out a SHA-tagged image to staging automatically. Verify it there.
   **Nothing here touches `CHANGELOG.md` or any version number** — staging
   deploys stay SHA-based on every merge, same as before this pipeline had
   tags at all.
3. When you're ready to promote what's on `develop`, open a PR from
   `develop` into `main` and merge it. This is also just a merge — still no
   deploy, no version bump, no changelog entry. `main` can sit any number
   of merges ahead of what's actually live in production; that's expected,
   not a problem to fix.
4. **Decide it's time to release, and only then tag:**
   ```bash
   git checkout main && git pull
   git tag v1.5.0                # bump MAJOR.MINOR.PATCH -- see below for which
   git push origin v1.5.0
   ```
   This is the one command that actually changes production. Everything
   from here on is automatic — see
   [The pipeline, branch by branch](#the-pipeline-branch-by-branch) for the
   stage-by-stage breakdown of what `release.yml` does with that tag
   (build + tag both images with `v1.5.0`, open the `CHANGELOG.md` PR, cut
   a GitHub Release, and deploy).

**Which part of `MAJOR.MINOR.PATCH` to bump** — a quick rule of thumb for a
project at this stage (pre-1.0 conventions can be looser, but pick one and
stay consistent):
- **PATCH** (`v1.4.2` → `v1.4.3`): bug fixes, no schema change, no API
  contract change a client of `/api/*` would notice.
- **MINOR** (`v1.4.2` → `v1.5.0`): new features, additive Alembic
  migrations (new nullable columns/tables — see the "migrate first, only
  ever ADD" rule in `README.md`'s CI/CD section), anything backward
  compatible.
- **MAJOR** (`v1.4.2` → `v2.0.0`): breaking changes — a migration that
  renames/drops something old code depends on, a `/api/*` contract change,
  the Bearer-token auth migration mentioned earlier in this doc, etc.

**What tagging does NOT require:** you do not need to be on a clean `main`
with nothing else in flight, and you do not need to wait for a previous
release's `CHANGELOG.md` PR to be merged first — `release.yml`'s `deploy`
job only needs the images it just built, never the changelog PR (see
[The pipeline, branch by branch](#the-pipeline-branch-by-branch)).

**Fully automating the version number too:** everything above still has you
typing the version number by hand. If you'd rather have it computed for you
from [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`/`fix:`/`feat!:` PR titles) — via a tool like
[release-please](https://github.com/googleapis/release-please-action),
which maintains a standing "Release PR" with the next version + changelog
pre-computed, and creates the tag the moment you merge it — that's a
separate, optional layer on top of this pipeline, not a requirement of it;
ask if you want it wired in.



If you're moving off the original managed-services design (Azure Database
for PostgreSQL Flexible Server):

```bash
# 1. Dump from the old Flexible Server
pg_dump "postgresql://<user>:<pass>@<old-server>.postgres.database.azure.com/asset_db?sslmode=require" \
  --format=custom --file=migration.dump

# 2. Get a temporary shell into the new `db` container app (internal-only by design)
az containerapp exec --name db --resource-group rg-snipeit-lite-prod --command /bin/sh

# 3. Restore through that session (or copy the dump in and run pg_restore inside `db` directly)
pg_restore --no-owner --no-privileges -d asset_db migration.dump
```

If you're moving off the interim single-`app` version of this
cost-optimized design (one combined container instead of separate
`backend`/`frontend`): no database migration needed — `db`/`redis` are
unchanged. Just run `infra-deploy.yml` against the new `infra/main.bicep`
(it will remove the old `app` Container App and create `backend`/
`frontend` in its place), then push to `main`/`develop` to populate both
new apps' images.

### The pipeline, branch by branch

- **`develop` push → Staging** (`deploy-azure-staging.yml`): build BOTH
  images (`backend`, `frontend`) in parallel → push each to its own Docker
  Hub repo, tagged with the commit SHA → Trivy scan (report-only, doesn't
  block) → run `migrate` job against the new `backend` image → roll out
  `backend` then `frontend`. Both run with min replicas 0 on staging — pure
  scale-to-zero.
- **`git tag v1.x.x` push → Production** (`release.yml` calling
  `deploy-azure-production.yml`): `release.yml` builds BOTH images and
  pushes each tagged with the **version itself** (e.g. `:v1.4.2`, not just a
  SHA — see [Rollback](#rollback) for why that's the point), with Trivy
  **blocking** on CRITICAL findings for either image; opens a pull request
  against `main` with the new `CHANGELOG.md` section (never a direct commit
  — see [Cutting a production release](#azure-container-apps-production-deployment-cost-optimized)
  above) and cuts a GitHub Release; and, **in parallel, not sequentially**,
  calls `deploy-azure-production.yml`, which runs `migrate` against the new
  `backend` image, rolls out `backend` then `frontend`, and smoke tests
  `frontend`'s `/` AND `/api/auth/me` (proving the whole chain — nginx's
  reverse proxy actually reaching `backend` — works, not just that
  `frontend` serves static files) — a failure triggers automatic rollback
  of both apps to their previously-deployed images. `backend` runs with min
  replicas 1 in production by default; `frontend` still scales to zero even
  in production (a cold start on static/proxy responses is much shorter
  than on `backend`'s Python process). `deploy-azure-production.yml` itself
  has no `push` trigger of its own anymore — it only runs when `release.yml`
  calls it, or via manual `workflow_dispatch` (see [Rollback](#rollback)).
- **`db` and `redis` are never touched by either pipeline** — fixed
  official images, only change when `infra/main.bicep` itself changes
  (re-run `infra-deploy.yml` manually).

### Zero-downtime rollout mechanics

Container Apps' Single revision mode creates new replicas of `backend` (and
separately, `frontend`) alongside the old ones, waits for each app's
readiness probe to pass, then shifts traffic and removes the old replicas —
no separate load balancer step needed. `backend` is always updated before
`frontend` in the pipeline, so `frontend`'s proxy target is already correct
by the time `frontend` itself rolls out. `db`/`redis` are pinned to exactly
1 replica always and are never part of a rolling update.

### Rollback

`deploy-azure-production.yml`'s `deploy` job snapshots both currently-live
image tags before updating (its "Snapshot currently-live images" step), and
automatically re-points both `backend` and `frontend` at them if the
post-deploy smoke test fails — most bad releases never need a manual
rollback at all. The runbook below is for the cases that do: a regression
that passes the smoke test but is caught later, or a rollback requested
well after the deploy finished.

**Step 1 — find out what's actually running right now.** Don't assume the
tag you *think* is live is the one that's live — ask Azure directly:
```bash
az containerapp show --name backend --resource-group rg-snipeit-lite-prod \
  --query "properties.template.containers[0].image" -o tsv
# -> e.g. <you>/snipeit-lite-backend:v1.4.2
```
(This is the same command `deploy-azure-production.yml`'s own snapshot step
runs — see its `GITHUB_STEP_SUMMARY` on the most recent successful run of
that workflow in the Actions tab for a ready-made record of "what was live
right before" and "what got deployed," without running anything yourself.)

**Step 2 — name the actual previous version**, not an abstract placeholder.
Tags sort by creation date, so the previous release is whichever one comes
right after the currently-live tag from step 1 in this list:
```bash
git fetch --tags
git tag --sort=-creatordate | head -n 5
```
```
v1.4.2   <- currently live (confirmed in step 1)
v1.4.1   <- this is PREV_TAG -- the one to roll back to
v1.4.0
v1.3.0
v1.2.4
```
(This is the exact same `git tag --sort=-creatordate` lookup
`release.yml`'s `changelog` job uses to build each `CHANGELOG.md` section —
so `CHANGELOG.md`'s `## [v1.4.2]` entry and this rollback are always talking
about the same "previous version.") You can also skim `CHANGELOG.md` on
`main`, or `gh release list --limit 5`, to confirm what changed in the
version you're rolling back past before you commit to it — note
`CHANGELOG.md` on `main` may briefly lag a version or two behind if its
automated PR (`changelog/vX.Y.Z`) hasn't been merged yet; the GitHub
Release always has the same notes immediately, with no merge required, if
you need them sooner.

**Step 3 — roll back.** Two equivalent ways to do it, in order of
preference:

- **Preferred: re-run the pipeline against the older tag**, so the rollback
  gets the same migrate-first-and-wait-for-healthy discipline (and
  automatic smoke test) as a forward deploy, rather than a raw image swap:
  ```bash
  gh workflow run deploy-azure-production.yml -f image_tag=v1.4.1
  ```
  (Actions tab → "Deploy to Azure (Production)" → Run workflow →
  `image_tag: v1.4.1` works identically if you'd rather not use the CLI.)
  ⚠️ Only safe if `v1.4.1`'s database schema is compatible with what's
  currently migrated — i.e. the bad release didn't itself introduce a
  migration that later code depends on. If it did, you need a
  forward-fixing migration instead of a rollback; see the "migrate first,
  deploy second" rule in `README.md`'s CI/CD section.

- **Direct, no pipeline**: swap the images by hand, e.g. if the automatic
  rollback already fired but you want to jump back further than one
  version:
  ```bash
  az containerapp update --name backend --resource-group rg-snipeit-lite-prod \
    --image <you>/snipeit-lite-backend:v1.4.1
  az containerapp update --name frontend --resource-group rg-snipeit-lite-prod \
    --image <you>/snipeit-lite-frontend:v1.4.1
  ```
  This skips `migrate` and the smoke test entirely — only use it once
  you've confirmed step 3's schema-compatibility caveat above yourself.

### Scaling

- **`backend`**: scales 0–3 replicas by default (configurable via
  `backendMinReplicas`/`backendMaxReplicas`) on concurrent HTTP request
  count. Since `RUN_EMBEDDED_WORKER=true`, every `backend` replica also
  runs its own Celery worker — see this doc's earlier "Load Balancing &
  Scaling For Peak Use" section for the multi-replica-safe patterns
  (Redis-backed rate limiter, Redis leader lock for the scheduled backup)
  that make this safe.
- **`frontend`**: scales 0–3 replicas by default (configurable via
  `frontendMinReplicas`/`frontendMaxReplicas`), independent of `backend`.
  Static/proxy responses are cheap, so its per-replica concurrency
  threshold is set higher than `backend`'s (see `infra/main.bicep`).
- **`db` and `redis`**: pinned to exactly 1 replica, always. Do not raise
  this — Postgres is single-writer and there's no clustering/failover story
  here; this is the explicit trade for the cost savings. If you outgrow a
  single-instance Postgres container, move `db` back to a managed service
  rather than trying to scale the container itself.

### Monitoring

All four Container Apps' console and system logs flow into one Log
Analytics workspace (`infra/main.bicep`'s `logAnalytics` resource, 30-day
retention). Query it from the Azure Portal (Log Analytics workspace →
Logs) or the CLI:
```bash
az monitor log-analytics query \
  --workspace <workspace-id> \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'backend' | order by TimeGenerated desc | take 100"
```
For live tailing without Log Analytics at all: `az containerapp logs show
--name backend --resource-group rg-snipeit-lite-prod --follow` (or
`--name frontend` for nginx's access/error logs). There is no Application
Insights in this architecture.

### Cost

East US pricing, ballpark — always check the
[Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)
before committing:

| Component | This architecture (4 apps) | Interim single-`app` version | Original managed-services version |
|---|---|---|---|
| Database | `db` container, 0.25 vCPU/0.5 GiB, 24/7 | same | Flexible Server, Burstable B1ms |
| Cache/broker | `redis` container, 0.25 vCPU/0.5 GiB, 24/7 | same | Azure Cache for Redis, Basic C0 |
| App compute | `backend` (0-N) + `frontend` (0-N), independent scaling | one combined `app` (0-N) | 4 separate always-on Container Apps |
| Registry | Docker Hub, 2 images (free) | Docker Hub, 1 image (free) | Azure Container Registry, Basic |
| Secrets | Container Apps secrets (free) | same | Key Vault |
| Observability | Log Analytics only | same | Log Analytics + Application Insights |
| **Rough monthly total** | **~US$10-22/mo** | **~US$10-20/mo** | **~US$50-100+/mo** |

Splitting `frontend` out adds roughly US$0-2/month over the single-`app`
version (one more small scale-to-zero container), in exchange for
`frontend` and `backend` each scaling to their own actual load instead of
both being sized for whichever is busier — often close to a wash in
practice, and sometimes a net win if the two workloads' traffic shapes
differ a lot. The real cost floor either way is `db` + `redis` running 24/7
at the smallest possible size, since they're stateful and can't scale to
zero (partially offset by the Container Apps Consumption plan's free
monthly grant: 180,000 vCPU-seconds + 360,000 GiB-seconds + 2M requests,
shared across all four apps).

**Levers you can pull for even more savings:**
- `backendMinReplicas=0` on production too (small extra cold-start latency
  for occasional visitors, in exchange for `backend`'s compute approaching
  zero — `frontend` already defaults to 0 even in production).
- Skip Log Analytics entirely (remove `appLogsConfiguration` from
  `infra/main.bicep`) if `az containerapp logs show --follow`-only
  visibility is enough for you.
- Reduce `postgresVolumeQuotaGb`/backup/export share quotas if your data
  footprint is small (Azure Files bills by GB actually used, so this mostly
  just caps a ceiling).

### Managing Environment Variables & Secrets Safely

There is no Key Vault in this architecture. Sensitive values
(`JWT_SECRET_KEY`, `ROOT_ADMIN_BOOTSTRAP_PASSWORD`, `DATABASE_URL`,
`REDIS_URL`, `SMTP_PASSWORD`, and the Docker Hub token if using a private
repo) are stored as **Container Apps secrets** on `backend`/`migrate` —
encrypted at rest, referenced by `secretRef`, never shown in
`az containerapp show`'s output or GitHub Actions logs. `frontend` carries
no secrets at all — it never touches the database, Redis, JWTs, or SMTP
directly, only `BACKEND_HOST`/`BACKEND_PORT`/`ENABLE_API_DOCS` as plain
env vars.

To rotate a secret (e.g. `POSTGRES_PASSWORD`): update the corresponding
GitHub secret, then re-run `infra-deploy.yml` for that environment — Bicep
deployments are idempotent, so this updates the secret on `db`, `redis`,
`backend`, and `migrate` consistently in one pass. Don't rotate by
hand-editing one container app's secrets directly; the others will drift
out of sync.

## Troubleshooting

- **"Backup failed: Permission denied" after adding the `export_data`
  volume** — a freshly created named volume is owned by `root:root`
  regardless of what the image's own `chown` did at build time (same
  root cause as the existing `backup_data` permission fix — see
  `backend/docker-entrypoint.sh`'s docstring). That entrypoint already
  fixes ownership of BOTH `BACKUP_DIR` and `EXPORT_RESULT_DIR` on every
  container start; if you're running a customized entrypoint, make sure
  it does the same for `EXPORT_RESULT_DIR`.
- **Random "please log in again" errors that seem to come and go** —
  usually means your `backend` replicas don't all have the exact same
  `JWT_SECRET_KEY`. Check every replica's environment; they must match
  exactly.
- **Notification emails arriving in duplicate** — check that `beat` is
  running as exactly one replica (`docker compose ps beat`) and that you
  haven't accidentally re-added the old `-B` flag to `worker`'s command.
- **Export downloads 404 shortly after finishing** — the file's already
  been swept off disk because it's older than `EXPORT_RESULT_TTL_SECONDS`
  (default: 1 hour). Increase that setting if your users routinely wait
  longer than that between generating and downloading an export.
- **Scheduled backup didn't run on a particular replica's logs, but did
  run somewhere** — expected behavior, not a bug: only the replica that
  wins `_acquire_scheduled_backup_lock()` for that scheduled time
  actually runs it; every other replica logs that it was skipped. Check
  `backup_data`'s `index.json` (via the System Backups panel in the
  dashboard) to confirm the backup exists, rather than grepping one
  specific replica's logs.

### Azure Container Apps specific (cost-optimized 4-app architecture)

- **`az containerapp update` succeeds but it still serves old behavior** —
  check `az containerapp revision list --name backend` (or `--name
  frontend`) for the active revision's `healthState`; if the new replica
  never passed its readiness probe, Container Apps never shifted traffic to
  it and the old revision is still serving. Check `az containerapp logs
  show --name backend` (or `frontend`) for the new revision's startup
  errors.
- **`frontend` loads but every `/api/*` call 502s** — `backend` is
  internal-ingress-only by design; confirm it's actually `Healthy`
  (`az containerapp revision list --name backend`) rather than a
  networking issue. Also confirm `frontend`'s `BACKEND_HOST`/`BACKEND_PORT`
  env vars are still `backend`/`8000` (see `infra/main.bicep`'s
  `frontendApp` resource) — there is no public hostname for `backend` at
  all in this architecture, nginx is the only thing that reaches it.
- **`backend` can't reach Postgres: connection refused / timeout** — `db`
  is internal-ingress-only by design; confirm it's actually `Healthy`
  (`az containerapp revision list --name db`) rather than a networking
  issue. Also confirm `DATABASE_URL` is hitting the short internal name
  `db:5432`, not a public hostname.
- **`backend` starts but every login fails with a Redis error** — same
  check as above but for `redis:6379`; also confirm the `REDIS_PASSWORD`
  secret used to build `REDIS_URL` matches what `redis`'s
  `--requirepass` was actually started with (a password rotation on one
  side without redeploying the other will break this silently — always
  change it via `infra/main.bicep`'s `redisPassword` parameter and
  redeploy both, not by hand-editing one container app's env vars).
- **Postgres data is gone after a redeploy** — check that the
  `postgres-data` Azure Files share still has content
  (`az storage file list --share-name postgres-data ...`) and that `db`'s
  volume mount didn't get dropped from a hand-edited revision. This should
  never happen from `infra/main.bicep` alone — if it does, it's a strong
  signal something bypassed the Bicep template.
- **`migrate` job succeeds instantly with no actual migration applied** —
  usually means the job is still pointed at an old `backend` image tag.
  `deploy-azure-production.yml`'s migrate job always runs
  `az containerapp job update --image` immediately before
  `az containerapp job start` for exactly this reason; if you're triggering
  the job manually, do the same.
- **First request of the day is slow** — expected on staging (both apps
  default to min replicas 0) and, for `frontend` specifically, even on
  production (it defaults to min replicas 0 there too — its cold start is
  short). If `backend`'s cold start is the slow part on production, confirm
  `backendMinReplicas` is actually `1` there (small extra cost, see the
  Cost section above).
- **`/docs` (Swagger UI) 404s even with `ENABLE_API_DOCS=true`** — this
  flag has to match on BOTH `backend` (gates FastAPI's own docs routes) and
  `frontend` (gates nginx's passthrough route in
  `nginx/default.conf.template`) — `infra/main.bicep`'s `enableApiDocs`
  parameter sets both from one place; if you've hand-edited either
  container app's env vars directly, they can drift out of sync.
- **Image pull failures on a public Docker Hub repo** — Docker Hub
  anonymous pulls are rate-limited per IP; if you're hitting that limit,
  either set `dockerHubUsername`/`dockerHubToken` (authenticated pulls get
  a much higher limit even for a public repo) or make the repo private,
  which requires the same two parameters anyway (remember: Docker Hub's
  free plan only includes ONE private repo, so making both `backend` and
  `frontend` private needs a paid plan).
