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
[Azure Container Apps Production Deployment](#azure-container-apps-production-deployment)
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
- [Azure Container Apps Production Deployment](#azure-container-apps-production-deployment)
- [Troubleshooting](#troubleshooting)

---

## Before You Deploy: Safety Checklist

Go through every item below before this app is reachable from outside your
own machine. Most of these are also called out inline in `.env.example` and
`README.md`'s **Running In Production** section — they're repeated here
because they're the ones most likely to bite you specifically in a
multi-instance/production setup.

- **`ENVIRONMENT=production`** in your real `.env`. This isn't cosmetic —
  `config.py` uses it to refuse to boot at all if `JWT_SECRET_KEY` or
  `SUPER_ADMIN_PASSWORD` are still placeholder/weak values. Treat a
  startup crash here as the app protecting you, not a bug.
- **Generate real secrets.** `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`,
  `SUPER_ADMIN_PASSWORD` — none of these should be the values shipped in
  `.env.example`. Generate a real JWT secret with:
  ```bash
  python3 -c "import secrets; print(secrets.token_hex(32))"
  ```
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
`backend/api/audit.py`) now streams the file straight off disk via
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
  point your orchestrator's health check / load balancer target group at
  it for each `backend` replica.
- `db` and `redis` already have `healthcheck:` blocks in
  `docker-compose.yml` that `backend`/`worker`/`beat` all `depends_on:
  condition: service_healthy` — a fresh `docker compose up` won't start
  the app tier racing against a Postgres/Redis that isn't ready yet.
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

## Azure Container Apps Production Deployment

This is the **primary production target**: Azure Container Apps (ACA), fully
automated end to end. Push to `develop` and Staging updates itself; merge to
`main` and Production updates itself — nothing manual after the one-time
setup below. The pipeline lives in `.github/workflows/`
(`ci.yml`, `infra-deploy.yml`, `deploy-azure-staging.yml`,
`deploy-azure-production.yml`) and the infrastructure lives in
`infra/main.bicep`.

### Why Container Apps, and what changes vs. Docker Compose

Every app-shaped piece of `docker-compose.yml` maps 1:1 onto its own Azure
Container App, same names, same images, same `command:` overrides:

| Compose service | Azure Container App | Notes |
|---|---|---|
| `backend` | Container App `backend` | Internal ingress only — never public |
| `worker` | Container App `worker` | No ingress at all — scales on Celery queue depth, not HTTP |
| `beat` | Container App `beat` | Pinned to exactly 1 replica, always — same rule as Compose |
| `frontend` | Container App `frontend` | External ingress — the ONLY public entry point |

**`db` and `redis` are the one deliberate departure** from a literal
container-per-container port: they become **Azure Database for PostgreSQL
Flexible Server** and **Azure Cache for Redis** instead of containers. A
stateful container in Container Apps still gets recreated/rescheduled like
any other revision, and Container Apps' own storage isn't a substitute for a
real database engine's backup/point-in-time-restore/failover story. The
managed services are drop-in over the wire — `DATABASE_URL` and `REDIS_URL`
keep the exact env var names `config.py`/`celery_app.py` already read, only
the values change (TLS-required Postgres connection string, TLS-only
`rediss://` Redis connection string — see `.env.azure.example` for both).
**Zero application code changes were needed for this.**

`backup_data` and `export_data` (Compose's named volumes) become **Azure
Files shares**, mounted into `backend`/`worker` at the exact same paths
(`/app/backups`, `/app/export_results`) — `backend/services/backup_service.py`
and `backend/tasks/export_tasks.py`'s shared-disk assumptions (multiple
replicas reading/writing the same files) keep working unmodified.

### One-time setup

1. **Create an Azure AD App Registration with federated credentials** (OIDC
   — GitHub Actions authenticates to Azure with zero stored secrets):
   ```bash
   az ad app create --display-name snipeit-lite-github-actions
   # note the appId (AZURE_CLIENT_ID) and your tenant/subscription IDs
   az ad app federated-credential create --id <appId> --parameters '{
     "name": "github-main",
     "issuer": "https://token.actions.githubusercontent.com",
     "subject": "repo:<org>/<repo>:ref:refs/heads/main",
     "audiences": ["api://AzureADTokenExchange"]
   }'
   # repeat with "subject": "repo:<org>/<repo>:ref:refs/heads/develop" for staging,
   # and "repo:<org>/<repo>:environment:<name>" if you use GitHub Environments
   ```
   Grant that app **Contributor** on the two resource groups you create next
   (`az role assignment create ...`).

2. **Create the resource groups** (one per environment, so staging and
   production can never accidentally touch each other's resources):
   ```bash
   az group create --name rg-snipeit-lite-staging --location eastus
   az group create --name rg-snipeit-lite-prod --location eastus
   ```

3. **Add GitHub repo secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `AZURE_CLIENT_ID` | App Registration's Application (client) ID |
   | `AZURE_TENANT_ID` | Your Azure AD tenant ID |
   | `AZURE_SUBSCRIPTION_ID` | Target subscription ID |
   | `AZURE_LOCATION` | e.g. `eastus` |
   | `PROD_RESOURCE_GROUP` | `rg-snipeit-lite-prod` |
   | `STAGING_RESOURCE_GROUP` | `rg-snipeit-lite-staging` |
   | `POSTGRES_ADMIN_PASSWORD` | Generate: `openssl rand -base64 24` |
   | `JWT_SECRET_KEY` | Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
   | `SUPER_ADMIN_PASSWORD` | Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(18))"` |
   | `CUSTOM_DOMAIN` | Optional — leave empty to use the generated `*.azurecontainerapps.io` domain |

   Use **different** values for staging vs. production where it matters
   (`POSTGRES_ADMIN_PASSWORD`, `JWT_SECRET_KEY`, `SUPER_ADMIN_PASSWORD`) —
   easiest via two GitHub **Environments** (`staging`, `production`) each
   with their own copy of those three secrets, referenced by
   `infra-deploy.yml`'s `environment:` key.

4. **Provision the infrastructure** — run the *Deploy Azure Infrastructure*
   workflow (Actions tab → `infra-deploy.yml` → Run workflow → pick
   `staging`, then run it again and pick `production`). This creates
   everything in the table above via `infra/main.bicep` and takes ~10–15
   minutes (Postgres Flexible Server is the slow part).

5. **First app deploy** — push to `develop` (staging) or merge to `main`
   (production). The pipeline builds real images and replaces the
   placeholder ones the infra deploy created.

### The pipeline, branch by branch

Same branching model as the Compose deployment above, re-targeted at Azure:

| Branch | Workflow | What happens |
|---|---|---|
| `feature/*` | `ci.yml` | Lint, test, dependency audit, secret scan, image build + scan. No deploy. |
| `develop` | `ci.yml` → `deploy-azure-staging.yml` | Builds images (frontend: minified, not obfuscated), pushes to ACR, runs `alembic upgrade head` via the `migrate` Container Apps Job, rolls out to staging, smoke tests. |
| `main` | `ci.yml` → `deploy-azure-production.yml` | Same, but frontend is bundled + minified + **obfuscated** (`BUILD_ENV=production`), Trivy scan is blocking on CRITICAL, and a failed smoke test triggers an **automatic rollback** to the previously-deployed image. |

```
push to main
  -> ci.yml (lint, test, audit, secret scan)
  -> build backend + frontend images, push to ACR (tag: commit SHA)
  -> Trivy scan (blocking on CRITICAL in prod)
  -> migrate job: alembic upgrade head (against the OLD, still-live containers)
  -> az containerapp update --image ... for backend, worker, beat, frontend
     (Container Apps' health-probed rolling replacement — see probes below)
  -> smoke test against the live frontend FQDN
  -> automatic rollback to the previous image if the smoke test fails
```

### Zero-downtime rollout mechanics

Every container app runs in **Single revision mode** with `Liveness`/
`Readiness` HTTP probes against `GET /healthz` (`backend`) and `GET /`
(`frontend`) — see `infra/main.bicep`'s `probes` blocks. When
`az containerapp update --image ...` runs, Container Apps starts new
replicas on the new image, waits for them to pass their readiness probe,
shifts traffic over, and only then tears down the old replicas — the same
guarantee `docker compose up -d --no-deps --scale backend=N backend` gives
you manually in the Compose deployment, done automatically by the platform
here. `migrate` always runs **before** this step, against the still-live old
containers, for the exact same "never a mid-flight schema mismatch" reason
called out in the Compose section above.

### Rollback

The production workflow rolls back **automatically** on a failed smoke
test. To roll back manually at any time (e.g. a bug shipped that passed the
smoke test):
```bash
# Find the previous working image tag (recent commit SHAs, or check ACR):
az acr repository show-tags --name <acr-name> --repository snipeit-backend --orderby time_desc -o table

az containerapp update --name backend --resource-group rg-snipeit-lite-prod --image <acr>/snipeit-backend:<previous-sha>
az containerapp update --name worker  --resource-group rg-snipeit-lite-prod --image <acr>/snipeit-backend:<previous-sha>
az containerapp update --name beat    --resource-group rg-snipeit-lite-prod --image <acr>/snipeit-backend:<previous-sha>
```
As with the Compose rollback rule above: **never** roll back a migration
just to roll back code — only run `alembic downgrade -1` via the `migrate`
job if the migration itself needs undoing, and only if nothing depending on
the new schema has shipped anywhere else.

### Scaling

`backend` and `frontend` scale on HTTP concurrency (KEDA's built-in HTTP
scale rule — see `infra/main.bicep`'s `scale.rules`). `worker` is the more
interesting one: rather than the Compose deployment's manual
`--scale worker=3` before a known traffic spike, `worker` here autoscales on
**Celery queue depth** via KEDA's Redis List Length scaler — a replica is
added for every 5 queued export jobs, up to `workerMaxReplicas`, and scales
back down automatically once the queue drains. `beat` is hard-pinned to
`minReplicas: 1, maxReplicas: 1` — never change this (see the duplicate
notification-email reasoning repeated throughout this doc and
`celery_app.py`'s `beat_schedule` comment).

### Monitoring

Every container app and the `migrate` job log to the **same Log Analytics
workspace** (`infra/main.bicep`'s `containerAppEnv.appLogsConfiguration`),
so one place covers the whole stack.

**Quick checks (CLI):**
```bash
# Live-tail a specific app's logs
az containerapp logs show --name backend --resource-group rg-snipeit-lite-prod --follow

# Current replica count, CPU/memory, restarts
az containerapp replica list --name backend --resource-group rg-snipeit-lite-prod -o table

# Revision health + traffic split
az containerapp revision list --name backend --resource-group rg-snipeit-lite-prod -o table
```

**Log Analytics (KQL)** — Azure Portal → your Log Analytics workspace →
Logs. Because `LOG_FORMAT=json` (see `.env.azure.example`), the backend's
structured log fields parse straight into the `Log_s` column and are easy to
query further:
```kql
// Errors across every app in the last hour
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h)
| where Log_s has "ERROR"
| project TimeGenerated, ContainerAppName_s, Log_s
| order by TimeGenerated desc

// Correlate one request across nginx + backend using the X-Request-ID
// that nginx/default.conf.template already injects
ContainerAppConsoleLogs_CL
| where Log_s has "<request-id>"
| order by TimeGenerated asc

// Restart/crash loop detection
ContainerAppSystemLogs_CL
| where TimeGenerated > ago(24h)
| where Log_s has "Restarted" or Log_s has "OOMKilled"
| summarize count() by ContainerAppName_s
```

**Application Insights** (already wired via `APPLICATIONINSIGHTS_CONNECTION_STRING`
in every app's env, see `infra/main.bicep`'s `sharedEnv`) gives you request
rate/latency/failure-rate charts and dependency tracking out of the box for
FastAPI once the `opentelemetry-instrumentation-fastapi` package is added to
`backend/requirements.txt` — a good next step, not yet wired into the
application code as of this deployment.

**Alerts** — set these up once in the Azure Portal (Monitor → Alerts →
Create alert rule) against the Log Analytics workspace or the container
apps directly, with an Action Group emailing/paging your team:
- `backend`/`frontend` `Replica Restart Count` > 3 in 10 minutes
- `backend` HTTP 5xx rate > 5% over 5 minutes (from `ContainerAppConsoleLogs_CL` or App Insights)
- Postgres Flexible Server `cpu_percent` > 80% for 15 minutes
- Redis `usedmemorypercentage` > 90%
- `migrate` job execution status = `Failed` (catches a bad migration before it becomes an outage)

**Dashboards** — Azure Portal → Container Apps Environment → Metrics gives
you built-in charts (requests, replica count, CPU/memory per app) with zero
setup; pin the ones you check most to a shared Azure Dashboard for the team.

### Cost

Rough East US, pay-as-you-go estimates (your region/traffic will shift these
— treat as a planning starting point, not a quote; verify with the
[Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)):

| Resource | Configuration | ~Monthly |
|---|---|---|
| Container Apps (`backend`) | 0.5 vCPU/1 GiB, min 1 replica, light traffic | ~$15–25 |
| Container Apps (`worker`) | 0.5 vCPU/1 GiB, min 1 replica, light export volume | ~$15 |
| Container Apps (`beat`) | 0.25 vCPU/0.5 GiB, always 1 replica (can't scale to 0) | ~$7 |
| Container Apps (`frontend`) | 0.25 vCPU/0.5 GiB, min 1 replica | ~$10–20 |
| Postgres Flexible Server | Burstable B1ms, 32 GiB storage | ~$16 |
| Azure Cache for Redis | Basic C0 (250 MB) | ~$16 |
| Container Registry | Basic | ~$5 |
| Storage Account + Azure Files | 2×20 GiB shares | ~$2–3 |
| Log Analytics + App Insights | Small app, low log volume | ~$0–10 |
| Key Vault | Standard, low operation count | <$1 |
| **Total, always-warm production** | | **~$90–115/month** |

The first 180,000 vCPU-seconds, 360,000 GiB-seconds, and 2 million requests
per subscription per month are free and offset part of the Container Apps
line items above (idle usage is billed at a reduced rate, roughly a third
of the active rate, but isn't fully covered by that free grant — see
[Container Apps billing](https://learn.microsoft.com/azure/container-apps/billing)).

**This is already the cheapest configuration that's still genuinely
production-shaped** — Burstable Postgres and Basic Redis are the bottom
tier of each managed service, and every Container App is sized to its
actual resource need (`beat` and `frontend` at 0.25 vCPU, not the more
generous defaults many quickstarts ship with). The main lever left is
**replica count**, and it's already wired up:

- **Staging scales to zero automatically.** `infra-deploy.yml` sets
  `backendMinReplicas=0`/`workerMinReplicas=0` for staging and `=1` for
  production (see that workflow's "Resolve replica floor" step) — staging
  costs close to $0 in Container Apps compute between deploys, and the
  cold start (a few seconds) is a non-issue for a QA environment nobody's
  actively waiting on.
- **Production's `backendMinReplicas`/`workerMinReplicas` are still just
  Bicep parameters** (`infra/main.bicep`) — if production traffic is also
  genuinely low/bursty (e.g. a small internal team, not a 24/7 public
  service), set both to `0` there too and accept the cold start on the
  first request after idle. That alone removes roughly $30/month.
- **`beat` can never scale to zero** (it has to keep running to fire the
  schedule) but is already the cheapest possible size — leave it as is.
- If cost matters more than the managed-service guarantees described
  above, the biggest single line items to reconsider are Postgres and
  Redis — running them as Container Apps instead (closer to the original
  Compose shape) saves roughly $30/month combined, at the cost of losing
  automated backups/point-in-time restore and dealing with container
  storage persistence yourself. I'd only make that trade for a
  low-stakes staging environment, not production.
- **Worth knowing:** Microsoft has announced Azure Cache for Redis
  retiring in 2028, with **Azure Managed Redis** as the migration target.
  Basic C0 remains fine to deploy today and there's no rush, but if you're
  optimizing for the multi-year picture rather than this month's bill,
  provisioning Azure Managed Redis instead from the start is worth a look —
  say the word and I'll swap `infra/main.bicep`'s `redis` resource over.

### Managing Environment Variables & Secrets Safely

Three different categories of "environment variable" exist in this
pipeline, each handled differently on purpose:

1. **Plain config** (`LOG_LEVEL`, `CURRENCY_CODE`, `ENABLE_API_DOCS`, ...) —
   not sensitive, set directly as Container App env vars in
   `infra/main.bicep`'s `sharedEnv`. Visible in `az containerapp show` and
   the Azure Portal — that's fine, nothing here is a secret.
2. **Real secrets** (`JWT_SECRET_KEY`, `SUPER_ADMIN_PASSWORD`, the Postgres
   password, `SMTP_PASSWORD`) — these are the ones worth being careful
   with, and they already flow through **Azure Key Vault**, not plain
   Container Apps env vars:
   ```
   GitHub encrypted secret  ──(OIDC login, no stored secret)──>  Bicep @secure() parameter
                                                                       │
                                                                       v
                                                          Key Vault secret (encrypted at rest,
                                                          RBAC-controlled, access-logged)
                                                                       │
                                                                       v
                             Container App "secretRef" + managed identity
                             (the app reads the value at start; it's never
                              written into the app's own env-var list)
   ```
   Nothing secret ever lands in this repo, a committed file, or a
   Container Apps env var you can read back in plaintext from the Portal.
3. **Local dev secrets** (`.env`) — unchanged from before: git-ignored,
   never touches Azure at all.

**How to check what's set, without exposing values:**
```bash
# Confirm a secret reference exists (never prints the value)
az containerapp secret list --name backend --resource-group rg-snipeit-lite-prod -o table

# Confirm Key Vault has the secret (again, no value shown without --query)
az keyvault secret list --vault-name <kv-name> -o table
```

**How to rotate a secret safely:**
```bash
# 1. Write the new value straight into Key Vault (never through a GitHub
#    Actions log, never through a plain `az containerapp update --set-env-vars`)
az keyvault secret set --vault-name <kv-name> --name jwt-secret-key --value "$(openssl rand -hex 32)"

# 2. Container Apps caches secret values per revision -- force every app
#    that reads it to pick up the new value:
az containerapp update --name backend --resource-group rg-snipeit-lite-prod
az containerapp update --name worker  --resource-group rg-snipeit-lite-prod
az containerapp update --name beat    --resource-group rg-snipeit-lite-prod
```
Rotating `JWT_SECRET_KEY` specifically logs out every currently-signed-in
user at once (every existing token fails verification against the new
secret) — same caveat as the Compose deployment's own safety checklist,
just easier to trigger by accident now that it's a one-command rotation.
Plan it for a quiet period, and roll it out to `backend`/`worker`/`beat`
together (never rotate on just one) — this is the exact same "every
replica needs the identical secret" rule called out earlier in this doc.

**Other guardrails already in place:**
- **OIDC federated login** (`azure/login@v2` in every workflow) — GitHub
  Actions authenticates to Azure with a short-lived token, not a stored
  client secret. There's no Azure credential sitting in GitHub at all to
  leak.
- **`gitleaks` in `ci.yml`** catches anything that looks like a secret
  before it's ever committed, independent of the Key Vault setup above.
- **GitHub Environments** — set `staging`/`production` up as GitHub
  Environments (Settings → Environments) with their own copy of
  `POSTGRES_ADMIN_PASSWORD`/`JWT_SECRET_KEY`/`SUPER_ADMIN_PASSWORD`, and
  add **required reviewers** on the `production` environment specifically
  — `infra-deploy.yml` already references `environment:
  ${{ github.event.inputs.target }}`, so this takes effect immediately
  with no workflow changes, and means a human has to approve before
  production secrets/infra can be touched.
- **None of the deploy workflows ever `echo`, log, or pass a secret value
  as a plain `--set-env-vars` argument** — every secret-bearing step in
  `deploy-azure-production.yml`/`-staging.yml`/`infra-deploy.yml` either
  references a `secretRef` already wired up in `infra/main.bicep`, or (for
  the one-time infra deploy) passes the value as a Bicep `@secure()`
  parameter, which Azure Resource Manager deliberately omits from
  deployment history/activity logs.

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

### Azure Container Apps specific

- **`az containerapp update` succeeds but the app still serves old
  behavior** — check `az containerapp revision list` for the active
  revision's `healthState`; if the new replicas never passed their
  readiness probe (`GET /healthz`), Container Apps never shifted traffic to
  them and the old revision is still serving. Check
  `az containerapp logs show` for the new revision's startup errors.
- **Backend can't reach Postgres: `SSL connection required`** — Azure
  Database for PostgreSQL Flexible Server requires TLS; make sure
  `DATABASE_URL` has `?sslmode=require` (see `.env.azure.example` and
  `infra/main.bicep`'s `databaseUrl` variable).
- **Celery worker can't connect to Redis** — Azure Cache for Redis is
  TLS-only on port 6380; the URL scheme must be `rediss://` (two `s`s), not
  `redis://`. `infra/main.bicep` already builds this correctly — only an
  issue if you've hand-edited a container app's env vars.
- **`worker` never scales past 1 replica despite a growing queue** — check the
  KEDA scaler's `auth` block still points at a valid `redis-conn` secret
  (an access key rotation on the Redis resource without a matching infra
  redeploy will break this silently).
- **`migrate` job "succeeds" instantly with no actual migration applied** —
  usually means the job is still pointed at an old image tag.
  `deploy-azure-production.yml`'s migrate job always runs
  `az containerapp job update --image` immediately before
  `az containerapp job start` for exactly this reason; if you're triggering
  the job manually, do the same.
- **`frontend` returns 502 for every `/api/*` request** — `backend`'s
  ingress is internal-only by design (see `infra/main.bicep`); confirm it's
  actually `Healthy` (`az containerapp revision list --name backend`)
  rather than a networking issue — an unhealthy backend behind internal
  ingress looks identical to a networking failure from nginx's side.
