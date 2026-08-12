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
(`infra/main.bicep` + `.github/workflows/deploy-azure-*.yml`). Once that's
up and running, see [POST_DEPLOYMENT.md](POST_DEPLOYMENT.md) for the
optional next steps: SMTP, Google Drive backup uploads, and mapping a
custom domain.

**Deploying to a single Azure VM instead?** See
[DEPLOYMENT_VM.md](DEPLOYMENT_VM.md) for that path end-to-end: Terraform
(`infra-vm/`) provisions the VM itself, a Cloudflare Tunnel replaces both
inbound SSH and any open inbound app port, and
`.github/workflows/deploy-azure-vm.yml` builds/pushes both images and
rolls them out over that tunnel with the same migrate → deploy → smoke
test shape as the Container Apps path above.

---

## Table of Contents

- [Running CI Manually From GitHub](#running-ci-manually-from-github)
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
- **[Post-Deployment: SMTP, Google Drive backups, custom domain →](POST_DEPLOYMENT.md)**

---

## Running CI Manually From GitHub

The CI workflow is not limited to branch pushes and pull requests. It also
supports an on-demand run from GitHub Actions.

1. Open **GitHub → Actions → CI**.
2. Click **Run workflow**.
3. Choose the branch or commit to validate.
4. Keep **Run infra/main.bicep validation too** enabled unless you
   intentionally want to skip the ACA Bicep validation.
5. Start the workflow.

The manual run performs the full CI validation suite. It does **not** deploy
to Azure or publish Docker images. This is useful after changing workflow
files, documentation, configuration, tests, or infrastructure when you want
a clean CI result without creating a throwaway commit.

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
  actually ready, not just alive — see [Zero-downtime rollout mechanics:
  blue-green](#zero-downtime-rollout-mechanics-blue-green).
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

This is the **primary production target**: Azure Container Apps (ACA), with
a fast, zero-downtime deploy pipeline built to be the **cheapest realistic
way to run this app on Azure** while keeping full functionality and real
autoscaling. **Every deployment is manually triggered from the Actions tab**
(`workflow_dispatch`, `deploy-azure-aca.yml`) — a plain push to `develop`
or `main` never deploys anything by itself (it only runs `ci.yml`'s fast
lint/test/build gate), and neither does pushing a `git tag v1.x.x`. A
version tag only runs `release.yml`, which builds, Trivy-scans, and
publishes the official release images to Docker Hub and cuts a
CHANGELOG/GitHub Release entry (see
[Versioning & Cutting a Release](#versioning--cutting-a-release) below) --
it stops there. To actually put that version into production, run
`deploy-azure-aca.yml` yourself, pick `production`, and paste the version
into its `image_tag` input.

> **Deploy target vs. frontend build mode -- two separate settings, don't
> confuse them:**
>
> - **Deploy target** (`staging` or `production`): picked explicitly every
>   run, via `deploy-azure-aca.yml`'s `environment` dropdown on the Actions
>   tab (`workflow_dispatch`). There is no repo-level fallback for this
>   anymore -- the dropdown defaults to `production` if you don't touch it,
>   so an unattended/default run always targets production; pick `staging`
>   explicitly if that's what you want. A version-tag push never deploys by
>   itself at all -- see above.
> - **Frontend build mode** (minified-only vs. minified+obfuscated): a
>   separate **Variable** named `ENVIRONMENT`, set PER GitHub Environment --
>   `Settings → Environments → staging` (and again under `→ production`)
>   `→ Environment variables`, NOT the repo-level Actions Variables tab, and
>   NOT a per-Environment secret. Set the `production` Environment's copy to
>   `production` to ship a minified+obfuscated frontend build there;
>   `development`/anything else (or leaving it unset) on either Environment
>   makes that Environment's build minified-only -- the safe default, so an
>   Environment nobody deliberately configured never silently ships an
>   obfuscated, production-flagged build. This is read by the
>   `resolve-target` job (see that job's own comment in
>   `deploy-azure-aca.yml`) and fed into `frontend/Dockerfile`'s `BUILD_ENV`
>   build arg -- it has no effect on which Azure resource group/image name
>   this run touches, that's entirely the dropdown's job. The run's summary
>   (`GITHUB_STEP_SUMMARY`) prints which build mode was actually used, so
>   you can confirm it after the fact instead of guessing. This same
>   per-Environment variable is read the same way by `deploy-azure-vm.yml`
>   (see DEPLOYMENT_VM.md step 6). **Redeploying an existing `image_tag`
>   instead of building fresh skips this entirely** -- it reuses whatever
>   build mode that image was originally built with, regardless of what the
>   variable is set to now.
>
> The pipeline lives in `.github/workflows/` (`ci.yml`, `infra-deploy.yml`,
`deploy-azure-aca.yml`, `release.yml`, `build-push-images.yml`)
and the infrastructure lives in `infra/main.bicep`.

> **If you deployed an earlier version of this architecture** — either the
> original managed-services design (Flexible Server + Azure Cache + ACR +
> Key Vault + 4 Container Apps), the interim single-`app` cost-optimized
> version (one combined container + `db` + `redis`), or the 4-Container-App
> cost-optimized version that ran Postgres as a `db` Container App on Azure
> Files (**that version doesn't actually work** — Azure Files can't host
> Postgres's data directory at all, see `infra/main.bicep`'s "WHY POSTGRES
> IS A MANAGED SERVICE" comment) — this section describes the current
> shape, not an incremental change from any of them.

### The shape: `frontend`, `backend`, `redis` + a managed Postgres

`infra/main.bicep` provisions **three** Container Apps, split so `frontend`
and `backend` can scale independently instead of being coupled to the same
replica count, **plus** a standalone Azure Database for PostgreSQL Flexible
Server — a managed service, not a Container App:

| Service | What it is | Public? | Scaling |
|---|---|---|---|
| `frontend` | `frontend/Dockerfile`, UNCHANGED from local Docker Compose — serves the static build, reverse-proxies `/api/*` to `backend` | Yes — the only public entry point | 0-N, independent of `backend` |
| `backend` | FastAPI + embedded Celery worker/beat (`backend/Dockerfile`) | No — internal-only | 0-N, independent of `frontend` |
| `redis` | `redis:7-alpine`, official Docker Hub image | No — internal-only | Pinned to 1 |
| `postgresServer` | Azure Database for PostgreSQL Flexible Server (managed PaaS, **not** a Container App) | No — firewall-restricted to Azure services + optionally your own IP | N/A — no replica concept |

Plus one **Container Apps Job** (`migrate`) that runs `alembic upgrade head`
against `backend`'s own image, on demand — not a fifth standing service.

**Why Postgres is a managed service, not a Container App:** an earlier
version of this file ran Postgres as a fourth Container App (`db`) on a
persistent Azure Files share, the same pattern still used for `backend`'s
`backup-data`/`export-data` volumes. That fails at container *start*,
before Postgres ever serves a query:

```
F chmod: /var/lib/postgresql/data/pgdata: Operation not permitted
F initdb: error: could not change permissions of directory "/var/lib/postgresql/data/pgdata": Operation not permitted
```

Azure Files (an SMB/NFS share) doesn't implement real POSIX ownership/
permission bits, and Postgres's own `initdb` unconditionally `chmod 700`s
its data directory as a hard-coded safety check. There's no config flag,
env var, or CPU/memory setting that fixes this — it's a permanent
incompatibility between Azure Files and any database engine that needs
POSIX permissions on its data directory, not a resource-sizing problem.
Every Container Apps persistent-volume type is backed by Azure Files under
the hood, so no volume type inside Container Apps can host Postgres at all.
The fix is Azure Database for PostgreSQL Flexible Server: Postgres running
on Microsoft-managed, Postgres-aware storage instead. This also means you
now get automated backups with point-in-time restore and managed engine
patching for the one genuinely stateful, single-writer piece of this stack
— see `infra/main.bicep`'s top-of-file comment for the full reasoning.

`redis` doesn't hit the Azure Files problem: it runs with `--appendonly no`
(no on-disk persistence at all), so keeping it as a small Container App
(not a managed Azure Cache instance) is a safe, cheap trade.

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

**Why `redis` is still a Container App:** once `backend` scales past 1
replica, it needs a *shared* Celery broker (all replicas' embedded workers
pull from the same queue, not N independent ones), a shared cross-replica
login rate limiter, and a shared backup-job leader lock (so N replicas
don't all run pg_dump at 3am simultaneously) — all three already built into
this codebase, all three requiring Redis. Cutting Redis would mean pinning
`backend` to exactly 1 replica forever (no real autoscaling) or silently
breaking all three the first time it scales past 1. It's a small container
(0.25 vCPU/0.5 GiB, no persistent volume, "resets on restart" trade), and
unlike Postgres, it never touches Azure Files in the first place.

**Zero application code changes were needed for the backend/redis split, or
for moving Postgres to a managed service** — `postgresServer` and `redis`
communicate with `backend`/`migrate` over the exact same `DATABASE_URL`/
`REDIS_URL` env vars `config.py`/`celery_app.py` already read; only the
values differ (see `.env.azure.example`), and `database.py`'s connection
pooling (`pool_pre_ping`, `pool_recycle`, `connect_timeout`) was already
written with a managed Postgres provider's idle-connection behavior in
mind.

`backup_data` and `export_data` (Compose's named volumes) are Azure Files
shares, mounted into `backend` at the exact same paths (`/app/backups`,
`/app/export_results`). Postgres's own data directory lives on
`postgresServer`'s Microsoft-managed storage — not Azure Files, not
mounted into any Container App at all.

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
   az provider register --namespace Microsoft.Network
   az provider register --namespace Microsoft.DBforPostgreSQL
   ```
   (`infra-deploy.yml` also registers `Microsoft.DBforPostgreSQL` — and,
   conditionally, `Microsoft.Insights` if you set `ALERT_EMAIL_ADDRESS` —
   automatically on every run, so this step is a belt-and-suspenders
   one-time step, not strictly required before your first deploy. No
   `Microsoft.ContainerRegistry`, `Microsoft.Cache`, or `Microsoft.KeyVault`
   registration needed — none of those services are used.)

3. **Create the two resource groups** (or let `infra-deploy.yml` create them
   on first run):
   ```bash
   az group create --name rg-snipeit-lite-staging --location eastus2
   az group create --name rg-snipeit-lite-prod --location eastus2
   ```
   (`eastus2` is used here instead of `eastus` because brand-new/Free Trial
   subscriptions are frequently hit with `LocationIsOfferRestricted` on
   `eastus` specifically for Azure Database for PostgreSQL Flexible Server —
   `eastus2` and `centralus` are the two regions that most consistently work
   on Free Trial/Pay-As-You-Go subscriptions. If `eastus2` also gets
   restricted for your subscription, try `centralus` next — there's no way
   to know in advance which region a given subscription is cleared for, so
   this is trial and error. Whatever you pick, use the same region for both
   commands above and for the `AZURE_LOCATION` Variable in step 5, and keep
   `POSTGRES_SKU_NAME` on a `Standard_B*` (Burstable) tier — Burstable has
   the widest regional availability of the three Flexible Server tiers.)

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
   | `STAGING_RESOURCE_GROUP` / `PROD_RESOURCE_GROUP` | repo | The two resource group names from step 3 |
   | `DOCKERHUB_USERNAME` | repo | From step 1 |
   | `DOCKERHUB_TOKEN` | repo | From step 1 |
   | `POSTGRES_PASSWORD` | per-environment | Generate with `openssl rand -base64 24`, **not** `openssl rand -hex ...` — Azure Database for PostgreSQL Flexible Server requires 8-128 characters with at least 3 of {uppercase, lowercase, digit, symbol}; hex output is only digits + a-f (2 categories) and will be rejected. Different value per environment. |
   | `REDIS_PASSWORD` | per-environment | `openssl rand -hex 16` is fine here — no complexity rule, this isn't Flexible Server. Different per environment. |
   | `JWT_SECRET_KEY` | per-environment | Generate with `openssl rand -hex 32` |
   | `ROOT_ADMIN_BOOTSTRAP_PASSWORD` | per-environment | Optional — the root admin's initial password. Leave unset to have `0002_bootstrap_root_admin.py` generate a random one and print it once instead (see README's "Viewing the one-time-generated root admin password"). The root admin's username/display name (`SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_NAME`) are set separately — see the Variables table below, not here. |
   | `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` | per-environment | Optional — required together if `NOTIFICATIONS_ENABLED=true` (see the Variables list below). Any RFC 5321 SMTP server works (your own Postfix, SendGrid, Mailgun, AWS SES's SMTP endpoint, ...) — no vendor-specific SDK. See [POST_DEPLOYMENT.md](POST_DEPLOYMENT.md). |
   | `BREVO_API_KEY` | per-environment | Only read when `EMAIL_PROVIDER=brevo` — from your Brevo (formerly Sendinblue) account's API Keys page. |
   | `RESEND_API_KEY` | per-environment | Only read when `EMAIL_PROVIDER=resend` — from your Resend account's API Keys page. |
   | `ADMIN_NOTIFICATION_EMAILS` | per-environment | Optional — comma-separated extra recipients for extension-request alerts, on top of Admins/Managers/the Super Admin, who are covered automatically. |
   | `BACKUP_GDRIVE_OAUTH_CLIENT_ID` / `BACKUP_GDRIVE_OAUTH_CLIENT_SECRET` / `BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN` | per-environment | Optional — required together if `BACKUP_GDRIVE_ENABLED=true` (see the Variables list below). Produced by running `backend/scripts/gdrive_oauth_setup.py` **once, on your own machine, not in CI** — see [POST_DEPLOYMENT.md](POST_DEPLOYMENT.md). |
   | `BACKUP_GDRIVE_FOLDER_ID` | per-environment | Optional — the destination Drive folder's ID (from its URL), required alongside the three secrets above. |
   | `ALERT_EMAIL_ADDRESS` | per-environment | Optional — leave unset to skip creating any alerting resources (no cost, no action group). Set it to wire up the three Azure Monitor scheduled query alerts (backend error-rate spike, `/readyz` failing, daily backup missing) from `infra/main.bicep` to that address — see [SRE_STRATEGY.md](SRE_STRATEGY.md) section 2. **Leave this unset on a brand-new environment's first-ever `infra-deploy.yml` run.** The three alert rules query the `ContainerAppConsoleLogs_CL` table, which Azure only creates once a log line has actually been ingested — on a fresh Log Analytics workspace it doesn't exist yet, and the deployment fails with `Failed to resolve table or column expression named 'ContainerAppConsoleLogs_CL'` if you try to create the rules first. Deploy once with this unset, let `backend`/`frontend` run for a few minutes (or serve one request), then set this secret and re-run `infra-deploy.yml` for the same environment to add the alert rules on top of the already-running infra. |
   | `OTEL_EXPORTER_OTLP_HEADERS` | per-environment | Optional — only used if `OTEL_ENABLED=true` (see the Variables table below). The one OTel setting kept as a Secret rather than a Variable, since it commonly carries a collector API key (`Authorization=Bearer <token>`-style header). See README.md's "Distributed Tracing" section. | 
   Where does otel_exporter_otlp_header come from?
   
   It depends entirely on which OTLP-compatible backend you're sending traces to — this only matters if you're using the generic OTLP route (self-hosted collector, Grafana Cloud, Honeycomb, etc.) rather than otelAzureMonitorEnabled. You get the actual key/value from that vendor's dashboard:
   Honeycomb → x-honeycomb-team=<your API key> (from Honeycomb's Team Settings → API Keys)
    Grafana Cloud OTLP → typically Authorization=Basic <base64(instanceID:apiToken)> (from your Grafana Cloud stack's "OTLP" connection page)
    Self-hosted otel-collector → only needed if you've configured that collector to require an API key/bearer token itself — many self-hosted setups on a private network skip auth entirely, in which case leave this empty.
    Any other OTLP SaaS vendor → check their docs for "OTLP HTTP headers" or "OTLP authentication."
    If you don't have one of these

Leave otelExporterOtlpHeaders (and otelExporterOtlpEndpoint) empty and use otelAzureMonitorEnabled=true instead — that path needs no external vendor, no manually-obtained credential, and Azure provisions the App Insights connection string for you automatically, as covered above.

   Also set these repo/environment-level **Variables** (Settings → Secrets
   and variables → Actions → **Variables** tab, not Secrets — none of
   these are sensitive). Most of these share the same name/meaning with
   `infra-deploy-vm.yml`/`sync-secrets-vm.yml` on the VM deploy path — see
   `DEPLOYMENT_VM.md` step 6 for that path's own table, and its callout
   just below that table for the handful (mostly rate-limiting/lockout
   and notification-timing knobs) not yet wired as VM Variables:

   | Variable | Scope | Notes |
   |---|---|---|
   | `ENVIRONMENT` | per-environment | `production` for a minified+obfuscated frontend build (read by `deploy-azure-aca.yml`'s `resolve-target` job, fed into `frontend/Dockerfile`'s `BUILD_ENV`); `development`/unset for minified-only -- set independently on EACH GitHub Environment (`staging`, `production`). See the callout above -- this does NOT pick which Azure resource group/environment gets deployed, that's the workflow's `environment` dropdown. |
   | `FRONTEND_BUILD_TARGET` | per-environment | `react` to ship the React "Ledger" SPA (served at `/`), `legacy`/unset (default) to ship the legacy static site (also served at `/`) -- the two are mutually exclusive; there's no longer a combined option that ships both. Read by the same `resolve-target` job, fed into `frontend/Dockerfile`'s `--target` -- independent of `ENVIRONMENT` above, so build mode and which frontend ships are separate choices. Set independently on EACH GitHub Environment. This is the **standing default** -- for a one-off override on a single run, use the `frontend_type` dropdown on that workflow's "Run workflow" form instead (`(environment default)` / `react` / `legacy`); no Settings page needed, and nothing is saved past that run. See `frontend-app/README.md`'s "Choosing which frontend to ship" section. |
   | `AZURE_LOCATION` | repo | e.g. `eastus2` — see the note on region restrictions in step 3 above; `centralus` is the fallback if `eastus2` is also restricted on your subscription. Falls back to `eastus2` if unset. |
   | `CUSTOM_DOMAIN` | per-environment | Optional — leave unset to use the generated `*.azurecontainerapps.io` FQDN |
   | `NOTIFICATIONS_ENABLED` | per-environment | Optional, string `"true"`/`"false"` — master switch for all outbound email. Leave unset (defaults to off) until the four `SMTP_*` secrets above are set. See [POST_DEPLOYMENT.md](POST_DEPLOYMENT.md) for the full walkthrough. |
   | `BACKUP_GDRIVE_ENABLED` | per-environment | Optional, string `"true"`/`"false"` — leave unset (defaults to off, local-disk-only backups) until the four `BACKUP_GDRIVE_*` secrets above are set. See [POST_DEPLOYMENT.md](POST_DEPLOYMENT.md). |
   | `POSTGRES_SKU_NAME` | repo | Optional — sizes the Flexible Server. Default `Standard_B1ms` if unset. |
   | `POSTGRES_STORAGE_GB` | repo | Optional — sizes the Flexible Server. Default `32` if unset. |
   | `ENABLE_API_DOCS` | per-environment | Optional, string `"true"`/`"false"` — exposes `/docs`/`/redoc` (Swagger/ReDoc). Default `false` if unset. |
   | `SITE_NAME` | per-environment | Optional — display name shown in the UI/emails. Default `Snipe-IT Lite` if unset. |
   | `LOG_LEVEL` | per-environment | Optional — `backend`'s structured JSON log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`). Default `INFO` if unset. |
   | `LOGIN_RATE_LIMIT_MAX` | per-environment | Optional — failed-login attempts allowed per window before rate-limiting kicks in. Default `5` if unset. |
   | `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | per-environment | Optional — the window `LOGIN_RATE_LIMIT_MAX` is measured over, in seconds. Default `60` if unset. |
   | `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` | per-environment | Optional — failed logins before an account is locked out entirely (separate from, and on top of, the rate limit above). Default `5` if unset. |
   | `ACCOUNT_LOCKOUT_DURATION_MINUTES` | per-environment | Optional — how long a lockout from the setting above lasts. Default `15` if unset. |
   | `EMAIL_PROVIDER` | per-environment | Optional, `smtp` (default) \| `brevo` \| `resend` — which transport `send_email()` uses. Set this to `brevo` or `resend` instead of using the four `SMTP_*` secrets above if your outbound network blocks plain SMTP ports (both alternatives send over plain HTTPS). See `infra/main.bicep`'s `emailProvider` param. |
   | `SUPER_ADMIN_USERNAME` | per-environment | Optional — the root admin's login username. Default `superadmin` if unset. |
   | `SUPER_ADMIN_NAME` | per-environment | Optional — the root admin's display name. Default `Super Admin` if unset. |
   | `SMTP_PORT` | per-environment | Optional — only relevant once `NOTIFICATIONS_ENABLED=true`. Default `587` if unset. |
   | `SMTP_USE_TLS` | per-environment | Optional, string `"true"`/`"false"` — STARTTLS. Default `true` unless explicitly set to `"false"`. |
   | `SMTP_USE_SSL` | per-environment | Optional, string `"true"`/`"false"` — implicit TLS (e.g. port 465). Default `false` unless explicitly set to `"true"`. Mutually exclusive with `SMTP_USE_TLS` in practice — set at most one. |
   | `OVERDUE_DIGEST_HOURS_UTC` | per-environment | Optional — comma-separated hours of day (UTC, each 0-23) the overdue-assets digest fires, e.g. `8` or `8,20`. Same syntax as `BACKUP_HOURS_UTC` below. Default `8` if unset. |
   | `DUE_SOON_REMINDER_DAYS` | per-environment | Optional — how many days before an asset's return date the "due soon" reminder starts. Default `2` if unset. |
   | `DUE_SOON_DIGEST_HOURS_UTC` | per-environment | Optional — comma-separated hours of day (UTC, each 0-23) the due-soon digest fires. Same syntax as `OVERDUE_DIGEST_HOURS_UTC` above. Default `8` if unset. |
   | `SEND_INDIVIDUAL_HOLDER_REMINDERS` | per-environment | Optional, string `"true"`/`"false"` — also emails the individual asset holder, not just Admins/Managers. Default `true` unless explicitly set to `"false"`. |
   | `EXTENSION_REQUEST_SLA_HOURS` | per-environment | Optional — hours a `pending` Extension Request can go without a decision before the SLA-nudge digest escalates it. Default `24` if unset. |
   | `QUOTATION_SLA_HOURS` | per-environment | Optional — hours a `submitted` Quotation can go without a decision before the SLA-nudge digest escalates it. Default `24` if unset. |
   | `APPROVAL_SLA_CHECK_INTERVAL_MINUTES` | per-environment | Optional — how often (in minutes) the worker checks both queues above for anything past its SLA threshold. Default `60` if unset. |
   | `APPROVAL_SLA_ESCALATION_REPEAT_HOURS` | per-environment | Optional — hours before an already-escalated, still-undecided row is eligible to be re-escalated. Default `24` if unset. |
   | `SEND_QUOTATION_RECIPIENT_EMAILS` | per-environment | Optional, string `"true"`/`"false"` — whether a Quotation's own recipient gets emailed on every change (line items, notes, discount, assignment, approval, fulfillment), on top of the in-app bell notification which is always created regardless. Default `true` unless explicitly set to `"false"`. |
   | `DISPLAY_TIMEZONE` | per-environment | Optional — IANA zone (e.g. `America/New_York`) used to render timestamps in the UI, filenames, and emails. Default `Africa/Lagos` if unset. |
   | `CURRENCY_CODE` | per-environment | Optional — ISO 4217 code (e.g. `USD`) shown next to asset costs. Default `NGN` if unset. |
   | `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER` | per-environment | Optional, string `"true"`/`"false"` — whether Staff/Customer roles see remaining stock counts in the catalog. Default `false` if unset. |
   | `BACKUP_HOURS_UTC` | per-environment | Optional — UTC hour(s) the daily `pg_dump` backup job runs, e.g. `3`. Default `3` if unset. |
   | `BACKUP_RETENTION_COUNT` | per-environment | Optional — how many local backups to keep before pruning the oldest. Default `7` if unset. |
   | `OTEL_ENABLED` | per-environment | Optional, string `"true"`/`"false"` — master switch for OpenTelemetry distributed tracing. Off by default. See README.md's "Distributed Tracing" section. |
   | `OTEL_SERVICE_NAME` | per-environment | Optional — only relevant once `OTEL_ENABLED=true`. Default `snipeit-lite-backend` if unset. |
   | `OTEL_EXPORTER_OTLP_ENDPOINT` | per-environment | Required once `OTEL_ENABLED=true` — your OTLP collector's endpoint (e.g. an Application Insights- or Jaeger-compatible one). No default; leave unset while tracing is off. |
   | `OTEL_TRACES_SAMPLE_RATIO` | per-environment | Optional — fraction of requests traced, `0.0`–`1.0`. Default `1.0` if unset. |
   | `OTEL_AZURE_MONITOR_ENABLED` | per-environment | Optional, string `"true"`/`"false"` — additionally exports traces to Azure Monitor/Application Insights alongside the plain OTLP endpoint above. Default `false` if unset. |

   Most deployments only ever need to touch a handful of these —
   `POSTGRES_SKU_NAME`/`POSTGRES_STORAGE_GB`, the notification-timing
   ones, and `DISPLAY_TIMEZONE`/`CURRENCY_CODE`, are the ones most
   commonly customized; everything else is safe to leave unset and take
   the default shown.

6. **Run `infra-deploy.yml` manually once per environment** (Actions tab →
   "Deploy Azure Infrastructure" → Run workflow → choose `staging`, then run
   it again for `production`). This provisions everything: Log Analytics,
   Storage + Azure Files shares, the Container Apps Environment, all three
   Container Apps (`backend`/`frontend` start on a placeholder `latest` tag
   — the next step gives them real images), and the Azure Database for
   PostgreSQL Flexible Server. The Flexible Server takes noticeably longer
   to provision than the Container Apps (several minutes is normal) — this
   is expected, not a stuck deployment.

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

9. **Manually run `deploy-azure-aca.yml`** (Actions tab → "Run
   workflow"): `environment: staging` for Staging, or `environment:
   production` for Production — either way this builds real images, pushes
   them to Docker Hub, runs migrations, and rolls them out. To deploy a
   tagged release rather than building fresh, first `git push --tags` (see
   [Versioning & Cutting a Release](#versioning--cutting-a-release) below --
   this only builds and publishes the images, it does not deploy them),
   then run `deploy-azure-aca.yml` with `environment: production` and that
   version pasted into `image_tag`. From here on, every deploy — staging or
   production — is a manual `deploy-azure-aca.yml` run; a plain `git push`
   to any branch or tag never deploys anything by itself, only `ci.yml`
   (and, for a tag, `release.yml`'s build-and-publish step).

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
2. Merge that PR into `develop`. Only `ci.yml` runs on the merge itself --
   there's no automatic deploy anymore (see `deploy-azure-aca.yml`'s own
   `on:` block: `workflow_dispatch` only, no `push` trigger and no
   `workflow_call` entry point). When you want to verify it on staging, manually run
   `deploy-azure-aca.yml` (Actions tab → "Run workflow" → `environment:
   staging`, leave `image_tag` blank to build fresh off `develop`) --
   builds a SHA-tagged image and rolls it out to staging.
   **Nothing here touches `CHANGELOG.md` or any version number** — staging
   deploys stay SHA-based, same as before this pipeline had tags at all.
3. When you're ready to promote what's on `develop`, open a PR from
   `develop` into `main` and merge it. This is also just a merge — still no
   deploy, no version bump, no changelog entry. `main` can sit any number
   of merges ahead of what's actually live in production; that's expected,
   not a problem to fix.
4. **Decide it's time to cut a release, and only then tag:**
   ```bash
   git checkout main && git pull
   git tag v1.5.0                # bump MAJOR.MINOR.PATCH -- see below for which
   git push origin v1.5.0
   ```
   This publishes the official release artifact — it does NOT touch
   production by itself. See
   [The pipeline, branch by branch](#the-pipeline-branch-by-branch) for the
   stage-by-stage breakdown of what `release.yml` does with that tag
   (build + tag both images with `v1.5.0`, open the `CHANGELOG.md` PR, cut
   a GitHub Release). No deploy job runs from `release.yml`.
5. **When you're ready to actually ship it, deploy it yourself:** run
   `deploy-azure-aca.yml` from the Actions tab, `environment: production`,
   with `v1.5.0` pasted into `image_tag` — it pulls the exact image
   `release.yml` already built and Trivy-scanned rather than rebuilding.
   Run it whenever you choose; nothing forces this to happen right after
   the tag push.

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



If you're moving off an earlier version of this repo that ran Postgres as a
self-hosted `db` Container App on Azure Files — the version this section
replaces, and the one that doesn't actually work (see "The shape" section
above for why) — here's how to get your data onto the new
`postgresServer` (Azure Database for PostgreSQL Flexible Server):

```bash
# 1. Get a temporary shell into the OLD `db` container app (internal-only
#    ingress, so this needs `az containerapp exec`, not a direct psql
#    connection from your machine) and dump from inside that session.
az containerapp exec --name db --resource-group rg-snipeit-lite-prod --command /bin/sh
# (inside the container:)
pg_dump -U snipeit -d asset_db --format=custom --file=/tmp/migration.dump
# then copy /tmp/migration.dump out of the container -- e.g. `az
# containerapp exec` doesn't support file transfer directly, so the
# simplest path is usually: base64-encode it and paste it out, or run this
# whole dump/restore pair from a `backend` shell instead (see step 2) since
# `backend` already has network access to both the old `db` app (same
# environment, internal DNS) and the new `postgresServer` (public FQDN).

# 2. Simpler in practice: run BOTH pg_dump and pg_restore from a `backend`
#    shell (backend/Dockerfile already has pg_dump/psql installed) after
#    infra-deploy.yml has created `postgresServer` alongside the still-live
#    old `db` app -- no manual file transfer needed, since both source and
#    destination are reachable in one place.
az containerapp exec --name backend --resource-group rg-snipeit-lite-prod --command /bin/sh
# (inside that shell:)
pg_dump "postgresql://snipeit:<old-db-password>@db:5432/asset_db" \
  --format=custom --file=/tmp/migration.dump
pg_restore --no-owner --no-privileges \
  -d "postgresql://snipeit:<new-postgres-password>@<server-name>.postgres.database.azure.com:5432/asset_db?sslmode=require" \
  /tmp/migration.dump

# 3. Once verified, remove the old `db` Container App (infra-deploy.yml
#    already stopped managing it once infra/main.bicep no longer declares
#    it, but ARM doesn't delete resources it no longer manages on its own):
az containerapp delete --name db --resource-group rg-snipeit-lite-prod --yes
# Also safe to delete the now-unused `postgres-data` Azure Files share
# (Storage account -> File shares in the portal, or `az storage share
# delete`) once you've confirmed the restore succeeded.
```

If you're instead moving off the *original* managed-services design (an
even earlier Flexible Server + Azure Cache + ACR + Key Vault + 4
Container Apps shape) straight onto this version: your data is already on
a Flexible Server, just possibly a different one (different name/SKU) than
`infra/main.bicep` provisions now. Either point `postgresPassword`/a
manually-edited `postgresServer` name at your existing server (skip
provisioning a new one), or `pg_dump`/`pg_restore` between the two Flexible
Servers directly — both reachable over their public FQDNs, no
`containerapp exec` needed for either side:

```bash
pg_dump "postgresql://<user>:<pass>@<old-server>.postgres.database.azure.com/asset_db?sslmode=require" \
  --format=custom --file=migration.dump
pg_restore --no-owner --no-privileges \
  -d "postgresql://<user>:<pass>@<new-server>.postgres.database.azure.com/asset_db?sslmode=require" \
  migration.dump
```

If you're moving off the interim single-`app` version of this
cost-optimized design (one combined container instead of separate
`backend`/`frontend`, but still `db`/`redis` as Container Apps): follow the
`db` → `postgresServer` migration steps above first, then run
`infra-deploy.yml` against the new `infra/main.bicep` (it will remove the
old `app` Container App and create `backend`/`frontend` in its place), then
manually run `deploy-azure-aca.yml` once per GitHub Environment (`staging`,
then `production`) to populate both new apps' images.

### The pipeline, branch by branch

- **Manual `deploy-azure-aca.yml` run (`environment: staging`) → Staging**:
  build BOTH
  images (`backend`, `frontend`) in parallel → push each to its own Docker
  Hub repo, tagged with the commit SHA → Trivy scan (report-only, doesn't
  block) → run `migrate` job against the new `backend` image → roll out
  `backend` then `frontend`. Both run with min replicas 0 on staging — pure
  scale-to-zero.
- **`git tag v1.x.x` push → build + publish only, NOT a deploy**:
  `release.yml` builds BOTH images and pushes each tagged with the
  **version itself** (e.g. `:v1.4.2`, not just a SHA — see
  [Rollback](#rollback) for why that's the point), with Trivy **blocking**
  on CRITICAL findings for either image; opens a pull request against
  `main` with the new `CHANGELOG.md` section (never a direct commit — see
  [Cutting a production release](#azure-container-apps-production-deployment-cost-optimized)
  above) and cuts a GitHub Release. That's the entire tag-push pipeline —
  `release.yml` never calls `deploy-azure-aca.yml` and has no deploy job of
  its own.
- **Manual `deploy-azure-aca.yml` run (`environment: production`) →
  Production**: the only path that actually deploys to production. Leave
  `image_tag` blank to build fresh, or paste in an already-published
  version (e.g. `v1.4.2`, from the tag push above) to deploy that exact
  image without rebuilding. Runs `migrate` against the new `backend` image,
  rolls out `backend` then `frontend`, and smoke tests `frontend`'s `/` AND
  `/api/auth/me` (proving the whole chain — nginx's reverse proxy actually
  reaching `backend` — works, not just that `frontend` serves static
  files) — a failure triggers automatic rollback of both apps to their
  previously-deployed images. Both `backend` AND `frontend` run with min
  replicas 1 in production by default (see `infra-deploy.yml`'s "Resolve
  replica floors" step) — zero cold starts anywhere in the production
  request path, at the cost of two always-on replicas instead of one.
  `deploy-azure-aca.yml` has no `push` trigger and no `workflow_call` entry
  point at all — `workflow_dispatch` (either `staging` or `production`, see
  [Rollback](#rollback)) is the only way it ever runs.
- **`redis` and `postgresServer` are never touched by either pipeline** —
  `redis` runs a fixed official image, only changing when
  `infra/main.bicep` itself changes (re-run `infra-deploy.yml` manually);
  `postgresServer` is a managed PaaS resource with no image or deploy step
  at all.

### Zero-downtime rollout mechanics: blue-green

`backend` and `frontend` both run in Container Apps' **Multiple**
active-revisions mode (`infra/main.bicep`'s `activeRevisionsMode`), not
Single — two fully independent, individually addressable revisions run
side by side, with traffic between them a weight the deploy pipeline
controls explicitly. **Roles are fixed, not swapped from one deploy to
the next:** green is always the active/production revision, blue is
always the incoming candidate being validated — see
[`.github/scripts/aca-blue-green.sh`](.github/scripts/aca-blue-green.sh)'s
own top-of-file comment for the full rationale. Once a rollout finalizes,
the revision that was blue simply *is* green (the active role) from that
point on — there's no permanent "blue container" the way the VM path has
`backend-blue`/`backend-green`; a revision's own suffix is whatever this
run generated for it. `.github/workflows/deploy-azure-aca.yml`'s `deploy`
job drives the actual rollout through that script, per app, in this
order:

1. **Create the incoming ("blue") revision at 0% traffic** — nothing
   routes to it yet, so this step alone can never affect anything live.
2. **Health-check the replica that actually received the push**, not just
   the image CI already validated. Container Apps' own readiness probe
   (`/readyz` on `backend`, `/` on `frontend` — `infra/main.bicep`'s
   `probes` blocks) polls the incoming revision directly; the script
   waits for it to report `Healthy` before doing anything else.
3. **Direct smoke test the incoming revision's own slot** — `frontend`
   only, since it's the one app with a public FQDN (every revision gets
   its own once `activeRevisionsMode` is `Multiple`:
   `<app>---<suffix>.<domain>`). Real HTTP, hitting `/` and
   `/api/auth/me` (proving the reverse-proxy → `backend` chain, not just
   static files) — still zero production traffic, since nothing but this
   direct URL request reaches it. `backend`'s internal-only ingress isn't
   reachable this way from a GitHub-hosted runner; step 2 plus step 4's
   re-checks are what cover it.
4. **Migrate traffic gradually.** Production walks the incoming revision
   across 10% → 25% → 50% → 75% → 100%, re-verifying health after each
   step (a revision that was fine at 0% can still degrade under real
   concurrent load) — the same five-step ramp the VM path uses. Staging
   (min replicas 0 on both apps — no standing traffic to protect) jumps
   straight to 100% once step 2 passes.
5. **Spin down the active ("green") revision** — only after `backend` AND
   `frontend` have both reached 100% AND the end-to-end smoke test
   (`deploy-azure-aca.yml`'s `Smoke test` step) has passed against the
   fully-cut-over app. The active revision is deactivated at that point,
   not before — see [Rollback](#rollback) for why it's kept alive and at
   0% traffic (not deleted, not scaled down) for the entire rollout
   instead. Once spun down, the revision that was blue for this rollout
   is simply green (the active role) going forward.

`backend` always finishes its full rollout before `frontend`'s begins, so
`frontend`'s proxy target (`BACKEND_HOST`) is already the new `backend`
revision's traffic split by the time `frontend` itself starts rolling out.
`redis` is pinned to exactly 1 replica, still in Single revision mode, and
is never part of a rollout; `postgresServer` isn't a Container App at all,
so the concept doesn't apply to it either.

**Monitor a rollout live** — three equivalent ways, no SSH required in
any direction:

- **From a laptop**, no GitHub Actions access needed, any time during or
  after a deploy:
  ```bash
  bash .github/scripts/aca-blue-green.sh status backend rg-snipeit-lite-prod --watch
  ```
  Shows every revision's health, replica count, and live traffic weight,
  refreshed every 5 seconds.
- **The run's own `GITHUB_STEP_SUMMARY`** — `deploy-azure-aca.yml` writes
  the same information (active/incoming revision names per app) there, so
  the Actions tab alone is enough if you're not at a terminal.
- **The `/_deploy/` dashboard** — the ACA-path equivalent of the VM path's
  Caddy-served dashboard (see `DEPLOYMENT_VM.md`'s "Monitoring a
  rollout"). Its shell ships baked into the `frontend` image;
  `frontend`'s nginx (`nginx/default.conf.template`'s `/_deploy/`
  location) proxies `status.json`/`checks.log` **live**, per-request,
  straight through to the `deploy-status` Blob container
  (`infra/main.bicep`'s `deployStatusContainer`), which only
  `.github/scripts/aca-deploy-status.sh` (called from
  `deploy-azure-aca.yml` at every phase transition) ever writes to — the
  SAS token nginx reads it with is read-only. Reachable at
  `https://<domain>/_deploy/`, gated by HTTP Basic Auth (nginx's
  `auth_basic`, a different hash format than the VM path's Caddy —
  nginx needs an `$apr1$` hash, not bcrypt). **Set your own credentials
  before relying on this**, same reasoning as the VM path:
  ```bash
  openssl passwd -apr1 'your-password-here'
  ```
  Set the `DEPLOY_STATUS_USER` GitHub Environment *Variable* (defaults to
  `admin` if you skip it) and the `DEPLOY_STATUS_PASSWORD_APR1_HASH`
  *Secret* (paste the `$apr1$` hash above) on the `staging`/`production`
  Environment(s), the same place `AZURE_CLIENT_ID`/`PROD_RESOURCE_GROUP`
  already live — the next `deploy-azure-aca.yml` run picks them up
  automatically via its "Write ACA deploy status - init" step. If no
  storage account is found in the target resource group yet (a fresh
  environment that hasn't run `infra-deploy.yml`), that step warns and
  skips the dashboard for that run rather than failing the rollout. If the
  top card updates normally but the "Health Check Log" panel never gets
  any entries, see the Troubleshooting section's
  `.github/scripts/aca-deploy-status.sh` exec-bit entry below — that one
  script's file permissions, not storage or auth, are the most common
  cause. You need to make it an executable script to see the health log progress

### Rollback

**During a deploy (automatic):** because the rollout is blue-green (see
above), the active revision is never stopped or scaled down while the
incoming one is being proven — it just sits at 0% traffic until the very
end. If the final end-to-end smoke test fails, `deploy-azure-aca.yml`'s
`deploy` job calls `aca-blue-green.sh rollback` for whichever app(s) had
already finished their own rollout, which is just a **traffic-weight flip
back to the still-active revision** (no redeploy, no cold start, no image
pull) — the fastest possible recovery, and now available on staging too,
not just production, since it no longer depends on there having been
prior live traffic to roll back to. If an incoming revision instead fails
its OWN health check or direct smoke test mid-rollout (before ever
reaching real traffic), `aca-blue-green.sh` rolls that one back itself,
inline, without waiting for the smoke test step at all. Most bad releases
never need a manual rollback. The runbook below is for the cases that do:
a regression that passes the smoke test but is caught later, or a
rollback requested well after the deploy finished — at which point the
bad revision has already been fully cut over to and the old (former
"green") revision deactivated (see "Spin down the active slots" above),
so this is a genuine redeploy again, not a traffic flip.

**Step 1 — find out what's actually running right now.** Don't assume the
tag you *think* is live is the one that's live — ask Azure directly:
```bash
az containerapp show --name backend --resource-group rg-snipeit-lite-prod \
  --query "properties.template.containers[0].image" -o tsv
# -> e.g. <you>/snipeit-lite-backend:v1.4.2
```
(This is the same command `deploy-azure-aca.yml`'s own snapshot step runs —
see its `GITHUB_STEP_SUMMARY` on the most recent successful run of that
workflow in the Actions tab for a ready-made record of "what was live right
before," "what got deployed," and the active/incoming revision names for
both apps, without running anything yourself. Swap `--name backend` for
`--name frontend` to check that app too, and drop `-staging` from the
resource group for the staging environment.)

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
  gets the same migrate-first, blue-green-with-health-gates-and-automatic-
  smoke-test discipline as a forward deploy, rather than a raw image swap:
  ```bash
  gh workflow run deploy-azure-aca.yml -f environment=production -f image_tag=v1.4.1
  ```
  (Actions tab → "Deploy to ACA" → Run workflow → `environment: production`,
  `image_tag: v1.4.1` works identically if you'd rather not use the CLI.)
  This goes through the exact same blue-green rollout as any other deploy —
  `v1.4.1` becomes the new incoming ("blue") revision, health-gated and
  canaried in before it's promoted to the active ("green") role, with the
  currently-bad revision left as the fallback the whole time. ⚠️ Only safe
  if `v1.4.1`'s database schema is compatible with what's currently
  migrated — i.e. the bad release didn't itself introduce a migration that
  later code depends on. If it did, you need a forward-fixing migration
  instead of a rollback; see the "migrate first, deploy second" rule in
  `README.md`'s CI/CD section.

- **Direct, no pipeline**: use `aca-blue-green.sh` yourself, e.g. if you
  want to jump back further than one version without waiting on `ci`/
  `build-push`/`migrate` first. This still gets the same 0%-traffic
  replicate → health-gate → gradual-cutover treatment as the pipeline, just
  skipping `migrate` — only safe once you've confirmed the schema-
  compatibility caveat above yourself:
  ```bash
  az login   # or `az account show` to confirm you're already signed in
  bash .github/scripts/aca-blue-green.sh rollout backend rg-snipeit-lite-prod \
    <you>/snipeit-lite-backend:v1.4.1 "10,25,50,75,100" 20 false
  bash .github/scripts/aca-blue-green.sh rollout frontend rg-snipeit-lite-prod \
    <you>/snipeit-lite-frontend:v1.4.1 "10,25,50,75,100" 20 true
  ```
  Each command prints the active/incoming revision names it used as it
  goes; once you're satisfied the rolled-back version is good, spin down
  the revision it replaced:
  ```bash
  bash .github/scripts/aca-blue-green.sh finalize backend rg-snipeit-lite-prod <active-revision-name>
  bash .github/scripts/aca-blue-green.sh finalize frontend rg-snipeit-lite-prod <active-revision-name>
  ```
  ⚠️ A plain `az containerapp update --image ...` with no revision-suffix
  or traffic handling still WORKS to create a new revision, but since
  `backend`/`frontend` now run in Multiple revision mode with traffic
  pinned to explicit revision names (not `latestRevision: true` — see
  `infra/main.bicep`'s `ingress.traffic` comment), that new revision comes
  up at **0% traffic** and nothing will actually route to it until you also
  run `az containerapp ingress traffic set` yourself — use
  `aca-blue-green.sh rollout` above instead of reproducing that by hand.

  run `az containerapp ingress traffic set` yourself — use
  `aca-blue-green.sh rollout` above instead of reproducing that by hand.

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
- **`redis`**: pinned to exactly 1 replica, always. Do not raise this —
  there's no clustering here, and the Celery beat schedule assumes a
  single broker instance; this is the explicit trade for the cost savings.
- **`postgresServer`**: not a Container App, so replica counts don't apply.
  Vertical scaling instead — bump `postgresSkuName` (e.g. `Standard_B1ms`
  → `Standard_B2s`) and/or `postgresStorageGb` in `infra/main.bicep` (or
  via the `POSTGRES_SKU_NAME`/`POSTGRES_STORAGE_GB` GitHub Variables,
  see the one-time setup section above), then re-run `infra-deploy.yml`.
  Storage can only be **increased**, never decreased, so don't over-size
  it "just in case." If you outgrow Burstable entirely (sustained CPU, not
  just occasional bursts), switch `postgresSkuTier` to `GeneralPurpose`
  and `postgresSkuName` to a matching D-series size.

### Monitoring

**Watching a blue-green rollout in progress:**
```bash
bash .github/scripts/aca-blue-green.sh status backend rg-snipeit-lite-prod --watch
```
Refreshes every 5 seconds with a table of every revision for that app —
name, active/inactive, health state, replica count, and live traffic
weight — so you can watch the old ("blue") and new ("green") revisions
side by side as a deploy walks traffic between them. Drop `--watch` for a
single point-in-time snapshot instead of a loop; swap `backend` for
`frontend` to watch the other app. Works from anywhere with `az login`
access to the resource group — you don't need to be watching the GitHub
Actions run itself, though `deploy-azure-aca.yml` writes the same old-
revision/new-revision summary into that run's `GITHUB_STEP_SUMMARY` too.

All three Container Apps' console and system logs flow into one Log
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
Insights in this architecture. `postgresServer` has its own metrics/logs
surface separate from Log Analytics — Azure Portal → your Flexible Server
→ Monitoring, or:
```bash
az monitor metrics list --resource <postgresServer resource ID> \
  --metric cpu_percent,memory_percent,storage_percent --output table
```

### Cost

East US 2 pricing, ballpark — always check the
[Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)
before committing:

| Component | This architecture | Interim single-`app` version (also self-hosted Postgres — doesn't work) | Original managed-services version |
|---|---|---|---|
| Database | Azure Database for PostgreSQL Flexible Server, Burstable B1ms (1 vCore/2GiB) + 32 GiB storage | `db` container, 0.25 vCPU/0.5 GiB, 24/7 (fails at boot — Azure Files can't host Postgres, see "The shape" above) | same Flexible Server, Burstable B1ms |
| Cache/broker | `redis` container, 0.25 vCPU/0.5 GiB, 24/7 | same | Azure Cache for Redis, Basic C0 |
| App compute | `backend` (0-N) + `frontend` (0-N), independent scaling | one combined `app` (0-N) | 4 separate always-on Container Apps |
| Registry | Docker Hub, 2 images (free) | Docker Hub, 1 image (free) | Azure Container Registry, Basic |
| Secrets | Container Apps secrets (free) | same | Key Vault |
| Observability | Log Analytics only | same | Log Analytics + Application Insights |
| **Rough monthly total** | **~US$20-35/mo** | N/A — doesn't actually run | **~US$50-100+/mo** |

The Flexible Server is the one component here with a real fixed floor —
roughly US$12-15/month for the smallest Burstable SKU (1 vCore/2GiB) plus
a few dollars for 32 GiB of storage, billed 24/7 regardless of traffic,
since Flexible Server has no scale-to-zero tier. `redis` adds a small
additional 24/7 floor (0.25 vCPU/0.5 GiB). Everything else —
`backend`/`frontend`, Storage/Azure Files, Log Analytics — keeps the
scale-to-zero/pay-for-what-you-use profile this design has always had
(partially offset by the Container Apps Consumption plan's free monthly
grant: 180,000 vCPU-seconds + 360,000 GiB-seconds + 2M requests, shared
across `backend`/`frontend`/`redis`).

Compared to the *original* managed-services design (Flexible Server +
Azure Cache + ACR + Key Vault + 4 always-on Container Apps), this version
keeps the one piece that has no working scale-to-zero substitute
(Flexible Server) and cuts the rest — Azure Cache, ACR Basic, and Key
Vault each had their own fixed monthly floor on top of an already
always-on compute layer.

**Levers you can pull for even more savings:**
- `backendMinReplicas=0` on production too (small extra cold-start latency
  for occasional visitors, in exchange for `backend`'s compute approaching
  zero — `frontend` already defaults to 0 even in production).
- Skip Log Analytics entirely (remove `appLogsConfiguration` from
  `infra/main.bicep`) if `az containerapp logs show --follow`-only
  visibility is enough for you.
- Reduce backup/export Azure Files share quotas if your data footprint is
  small (Azure Files bills by GB actually used, so this mostly just caps a
  ceiling, not the actual bill).
- Keep `postgresGeoRedundantBackup=false` (the default) unless your
  recovery plan specifically needs to survive a full regional outage —
  geo-redundancy roughly doubles backup storage cost.
- `postgresStorageGb` can only go up, never down, so start at the 32 GiB
  minimum rather than over-provisioning.

### Managing Environment Variables & Secrets Safely

There is no Key Vault in this architecture. Sensitive values
(`JWT_SECRET_KEY`, `ROOT_ADMIN_BOOTSTRAP_PASSWORD`, `DATABASE_URL`,
`REDIS_URL`, `SMTP_PASSWORD`, and the Docker Hub token if using a private
repo) are stored as **Container Apps secrets** on `backend`/`migrate` —
encrypted at rest, referenced by `secretRef`, never shown in
`az containerapp show`'s output or GitHub Actions logs. `frontend` carries
no secrets at all — it never touches the database, Redis, JWTs, or SMTP
directly, only `BACKEND_HOST`/`BACKEND_PORT`/`ENABLE_API_DOCS` as plain
env vars. `postgresServer`'s own administrator password is a Flexible
Server property, not a Container Apps secret — Azure stores/manages it as
part of the server resource itself.

To rotate `POSTGRES_PASSWORD`: update the GitHub secret, then re-run
`infra-deploy.yml` for that environment — this updates both the Flexible
Server's administrator password AND the `DATABASE_URL` secret on
`backend`/`migrate` in one pass (Bicep deployments are idempotent). Don't
rotate by hand-editing the Flexible Server's password directly in the
Portal/CLI without also updating the GitHub secret — `backend` would keep
using the old, now-invalid `DATABASE_URL` until the next `infra-deploy.yml`
run overwrites it, causing a connection outage in the meantime. Same
caution applies to `REDIS_PASSWORD`.

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

### Azure Container Apps specific (cost-optimized architecture)

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
- **`backend`/`migrate` can't reach Postgres: connection refused / timeout
  / "no pg_hba.conf entry for host"** — `postgresServer` is a standalone
  managed resource reached over its public FQDN
  (`<server>.postgres.database.azure.com`), not the short in-environment
  DNS name used for `redis`/`backend`. Check:
  1. `az postgres flexible-server show --name <server-name> --resource-group
     <rg> --query state` — should be `Ready`.
  2. The `AllowAllAzureServicesAndResourcesWithinAzureIps` firewall rule
     still exists (`az postgres flexible-server firewall-rule list --name
     <server-name> --resource-group <rg>`) — this is what lets
     `backend`/`migrate` (which have no static outbound IP on the
     Consumption plan) reach the server at all. It should never need manual
     recreation from `infra/main.bicep` alone; if it's missing, something
     bypassed the Bicep template.
  3. `DATABASE_URL` includes `?sslmode=require` and points at the FQDN, not
     a bare hostname like `db` (a leftover from an old self-hosted `db`
     Container App config that no longer exists in this architecture).
  4. If you're debugging from your own machine rather than from inside
     `backend`, you'll need your own IP added via `infra/main.bicep`'s
     `postgresAdminClientIp` parameter (redeploy after setting it) — the
     "Allow Azure services" rule above only covers Azure's own backbone,
     not arbitrary internet clients.
- **`backend` starts but every login fails with a Redis error** — same
  general check as above but for `redis:6379`, which IS reached over the
  short in-environment DNS name (it's still a Container App, unlike
  Postgres); also confirm the `REDIS_PASSWORD` secret used to build
  `REDIS_URL` matches what `redis`'s `--requirepass` was actually started
  with (a password rotation on one side without redeploying the other will
  break this silently — always change it via `infra/main.bicep`'s
  `redisPassword` parameter and redeploy both, not by hand-editing one
  container app's env vars).
- **Postgres password rejected: "password does not meet complexity
  requirements"** — Azure Database for PostgreSQL Flexible Server requires
  8-128 characters with at least 3 of {uppercase, lowercase, digit,
  symbol}. `openssl rand -hex ...` output is only digits + `a`-`f` (2
  categories) and will always be rejected — use `openssl rand -base64 24`
  instead (see the one-time setup section's GitHub secrets table above).
- **`initdb`/`chmod: Operation not permitted` in a `db` container's
  logs** — you're looking at logs from an old, no-longer-declared self-
  hosted `db` Container App (Postgres on Azure Files, which does not and
  cannot work — see "The shape" section above for the full explanation).
  This isn't something to fix in place; migrate that data onto
  `postgresServer` instead (see "moving off an earlier version" above)
  and then delete the old `db` Container App once the migration is
  verified — `az containerapp delete --name db --resource-group <rg>
  --yes`.
- **`migrate` job succeeds instantly with no actual migration applied** —
  usually means the job is still pointed at an old `backend` image tag.
  `deploy-azure-aca.yml`'s migrate job always runs
  `az containerapp job update --image` immediately before
  `az containerapp job start` for exactly this reason; if you're triggering
  the job manually, do the same.
- **First request of the day is slow** — expected on staging (both apps
  default to min replicas 0, pure cold-start-on-idle tradeoff). Should NOT
  happen on production — both `backendMinReplicas` and `frontendMinReplicas`
  default to `1` there (see `infra-deploy.yml`'s "Resolve replica floors"
  step), so neither app should ever scale to zero. `postgresServer` itself
  has no cold start either way — Flexible Server has no scale-to-zero tier,
  it's always running. If you're seeing a slow first request on production
  anyway, confirm both `backendMinReplicas`/`frontendMinReplicas` actually
  show `1` in the deployed environment (`az containerapp show --name
  backend --query properties.template.scale.minReplicas`, same for
  `frontend`) — a manual `az deployment group create` that skips
  `infra-deploy.yml` and its defaults, or a bicep default left as-is, would
  silently reintroduce a cold start (small extra cost either way to keep
  both warm, see the Cost section above).
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
- **`/_deploy/` dashboard's top status card updates fine (phase, image
  tag, traffic split) but "Health Check Log" stays permanently empty
  ("No checks recorded yet"), even on a rollout that otherwise finalizes
  as `Done`/`HEALTHY`** — almost always a missing execute bit on
  `.github/scripts/aca-deploy-status.sh` in the checked-out repo, not a
  storage or auth problem. The two write paths are wired differently:
  - Every top-card write (`init`/`write "rolling_out_backend"`/
    `write "done"`/etc.) is called from `deploy-azure-aca.yml` as
    `bash .github/scripts/aca-deploy-status.sh ...` — explicit `bash`,
    so it runs regardless of the file's permission bits. This is why the
    card itself can look completely healthy while the log is empty.
  - Every per-check log line (`backend-gate1-waiting`,
    `backend-readyz:green`, `backend-first-deploy`, etc.) instead comes
    from `.github/scripts/aca-blue-green.sh` invoking the SAME script
    directly as a command — `"$status_script" check ...` — gated behind
    `[ -x "$status_script" ]`. If that bit isn't set, the guard fails on
    every single call, and because this whole mechanism is deliberately
    best-effort (nothing here is allowed to fail an otherwise-healthy
    deploy), it fails silently: no warning in the Actions log, no error
    on the dashboard, just an empty log.

  Confirm with:
  ```bash
  git ls-files -s .github/scripts/aca-deploy-status.sh
  ```
  `100644` = not executable (the bug); `100755` = executable (fine). Fix
  it once, from any clone, without touching the file's content:
  ```bash
  git update-index --chmod=+x .github/scripts/aca-deploy-status.sh
  git commit -m "fix: restore exec bit on aca-deploy-status.sh"
  git push
  ```
  Takes effect on the NEXT `deploy-azure-aca.yml` run — `checks.log` is
  reset fresh by `init` at the start of every rollout, so an already-empty
  log from a past run stays empty; it won't retroactively backfill.

  This is ACA-specific — the VM path's equivalent
  (`scripts/blue-green-deploy.sh`) writes `status.json`/`checks.log`
  straight to local disk itself rather than shelling out to a second
  script, so it has no execute-bit dependency to lose in the first place.
  Worth a quick `git ls-files -s .github/scripts/*.sh` sanity check after
  any operation that can silently drop file modes — a fresh clone on a
  platform/tool that doesn't preserve them, a squash-merge, or hand-editing
  a file through a web UI — since the same class of bug can in principle
  affect any script one of these workflows invokes directly rather than
  via `bash <path>`.
