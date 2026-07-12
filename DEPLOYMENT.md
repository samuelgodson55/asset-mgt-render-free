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

---

## Table of Contents

- [Before You Deploy: Safety Checklist](#before-you-deploy-safety-checklist)
- [Production Setup](#production-setup)
- [Load Balancing & Scaling For Peak Use](#load-balancing--scaling-for-peak-use)
- [Speed: Background Workers Use Disk, Not RAM](#speed-background-workers-use-disk-not-ram)
- [Health Checks & Monitoring](#health-checks--monitoring)
- [Backups & Disaster Recovery](#backups--disaster-recovery)
- [Rolling Out Updates Without Downtime](#rolling-out-updates-without-downtime)
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
