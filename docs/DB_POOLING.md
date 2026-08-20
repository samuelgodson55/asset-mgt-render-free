# Database connection routing and PgBouncer

The application now provisions PgBouncer as part of every deployment path that can support a private pooler. Application traffic is routed automatically; operators should **not** edit `DATABASE_URL` to switch between direct PostgreSQL and PgBouncer.

## Routing rule

- `USE_PGBOUNCER=true` (default): SQLAlchemy application traffic uses the configured pooler endpoint.
- `USE_PGBOUNCER=false`: SQLAlchemy uses the direct PostgreSQL URL. This is a deliberate break-glass setting only.
- `DIRECT_DATABASE_URL` is derived automatically from the supplied `DATABASE_URL` and is used for connection-budget probing and `pg_dump`, which must not run through transaction pooling.

## Deployment paths

### Azure Container Apps + PostgreSQL Flexible Server

Azure ACA runs PgBouncer as its own internal-only Container App (`pgbouncer`, `infra/main.bicep`), using the same `edoburu/pgbouncer` image and configuration as the VM/local Compose paths below -- not Azure Database for PostgreSQL Flexible Server's managed PgBouncer server parameters. Azure's managed PgBouncer only supports the GeneralPurpose/MemoryOptimized compute tiers, not the Burstable tier this deployment defaults to for cost (see `postgresSkuTier`'s own description); enabling it against a Burstable server fails deployment with `ServerConfigurationNotAllowed` on the `pgbouncer.*` server parameters. Running PgBouncer as an ordinary Container App instead keeps pooling available regardless of compute tier, at no DB compute-tier cost increase.

`pgbouncer` has `external: false` ingress (never internet-reachable) and is only resolvable at its short in-environment DNS name, `pgbouncer`, from other Container Apps in the same environment -- the same reachability model `redis` already uses. `backendApp` is given `PGBOUNCER_HOST=pgbouncer` (config.py's `route_database_through_pgbouncer()` reads this) when `usePgbouncer=true`; the migration Job stays direct-to-Postgres on `5432`, same as every other path.

The GitHub Environment/Repository Variable `USE_PGBOUNCER` controls the Bicep `usePgbouncer` parameter (defaults to enabled) and is passed straight through as `USE_PGBOUNCER` for every path below.

### Azure VM

`docker-compose.vm.yml` always starts a VM-local PgBouncer service. Backend blue/green slots, Celery worker, and Beat all inherit `USE_PGBOUNCER`. The direct `DATABASE_URL` remains in `/opt/snipeit/.env`; no URL rewriting is required by operators.

The GitHub Environment Variable `USE_PGBOUNCER` is passed through Terraform/cloud-init and the secrets-sync workflow.

### Local Docker Compose

PgBouncer is always started and the backend/worker/beat route through it by default. The one-shot migration service explicitly uses the direct Postgres endpoint because schema migrations should not run through transaction pooling.

### Render Free

Render's current Free Blueprint shape cannot add the required private PgBouncer service without changing the deployment tier. The Blueprint therefore explicitly sets `USE_PGBOUNCER=false` rather than routing the application to a nonexistent local service. This is the only deployment path in the repository that intentionally remains direct.

## Why migrations/backups bypass PgBouncer

PgBouncer is configured for transaction pooling. That is ideal for short application transactions but inappropriate for tools that depend on a stable PostgreSQL session or perform administrative/schema operations. Alembic and `pg_dump` therefore use the direct URL.

## Safe rollout

The default is enabled in the code and deployment infrastructure. Switching it off is a single environment setting, not a database edit. If PgBouncer is unhealthy, the application will fail readiness rather than silently mutate the production database connection configuration.

## Background DB admission and connection lifetime

HTTP admission control is not enough to protect PostgreSQL because Celery and
Beat are separate DB-owning processes. The runtime therefore reserves one
PgBouncer server connection for background DB work by default:

- `DB_BACKGROUND_CONNECTION_RESERVE=1` removes that connection from the API
  pool budget when PgBouncer is enabled.
- `DB_BACKGROUND_CONCURRENCY_LIMIT=1` allows at most one DB-using background
  task across the deployment at a time, enforced with Redis slot leases.
- Background tasks acquire the slot **before opening `SessionLocal()`** and
  release it before SMTP/other external I/O.
- `worker_prefetch_multiplier=1` prevents each Celery worker process from
  hiding a large batch of DB tasks behind its local broker prefetch buffer.

Notification and SLA tasks now snapshot the database state, close the session,
perform email delivery, and only reacquire a short DB lease when a final state
stamp is required. This prevents a slow SMTP server from occupying a database
connection.

The global PgBouncer invariant is therefore:

```text
API SQLAlchemy pool capacity
+ reserved background DB capacity
<= PgBouncer server pool
```

If operators set the background concurrency and reserve values inconsistently,
the implementation uses the smaller safe value for actual background admission
and reserves the larger value when sizing the API pool.

## 503 shedding: what the caller sees vs. what the operator sees

Two independent mechanisms turn "more concurrent DB load than this process
will push at the database" into an HTTP 503 rather than a pile-up of slow
requests:

- `middleware/db_concurrency.py` sheds a request **before** it ever tries to
  check out a DB connection, once the in-process admission queue is full
  (reason code `db_admission_queue_full`).
- `middleware/error_handling.py` catches `sqlalchemy.exc.TimeoutError` when a
  request **was** admitted but the SQLAlchemy pool had no free/overflow
  connection within `DB_POOL_TIMEOUT_SECONDS` (reason code
  `db_pool_exhausted`).

The caller always gets a generic, safe message (`{"detail": "...", "reason":
"<code>", "request_id": "..."}`) -- never pool sizes, queue depth, or internal
reasoning. The `reason` code and full context go to structured logs, an
OpenTelemetry counter (`http.server.rejections_503`, labeled by `route` and
`reason`, exported wherever traces already go when `OTEL_ENABLED=true`), and
-- only when rejections are **sustained**, not merely present -- a single
ErrorBeacon "degraded dependency" event. See `backend/overload_monitor.py`'s
own module docstring for the full design; the two knobs that decide what
counts as "sustained" are:

- `OVERLOAD_ALERT_WINDOW_SECONDS` (default 30) -- the rolling window a
  rejection count is measured over.
- `OVERLOAD_ALERT_THRESHOLD_COUNT` (default 10) -- how many rejections for the
  *same reason* within that window counts as sustained.
- `OVERLOAD_ALERT_COOLDOWN_SECONDS` (default 300) -- minimum gap between two
  ErrorBeacon alerts for the same reason, once triggered, so one long overload
  episode produces one alert per cooldown period, not one per rejected
  request.

A burst that clears in under a window is exactly what admission control and
the pool timeout are *for* -- it should show up in the per-route/per-reason
counters, not page anyone.

## Real connection numbers: tuning the safety margin and background reserve

`PGBOUNCER_SAFETY_MARGIN_PERCENT` and `DB_BACKGROUND_CONNECTION_RESERVE`
(above) were previously sized once from intuition and left alone, with no
easy way to tell whether they were too generous (wasted PgBouncer capacity)
or too thin (clients queueing, Postgres connections maxed out) under real
traffic. `backend/db_pool_metrics.py` closes that gap with three independent,
best-effort snapshots:

1. **This process's own SQLAlchemy pool** (`pool_size` / `checked_out` /
   `checked_in` / `overflow`) -- free, in-memory, no network call. Shows
   whether this process's *share* of the PgBouncer budget
   (`api_budget / total_processes`, see `backend/database.py`) is actually
   being saturated.
2. **PgBouncer's own `SHOW POOLS` / `SHOW STATS`** -- client connections
   waiting (`cl_waiting`), server connections active/idle, and average time a
   client spends waiting for a free server connection (`avg_wait_time_us`).
   `cl_waiting > 0` and a rising `avg_wait_time_us` are the clearest "the
   server pool itself is undersized" signal -- direct evidence for whether
   the safety margin can be tightened. Requires `USE_PGBOUNCER=true`.
3. **Postgres's own `pg_stat_activity`**, independent of PgBouncer's
   accounting -- catches anything eating into the server's connection budget
   that PgBouncer doesn't know about (the `migrate` job, a stray `psql`
   session, a direct-connection break-glass), and is the real denominator
   `DB_CONNECTION_SAFETY_MARGIN` needs to be sized against.

Every deployment path's PgBouncer service (ACA's `pgbouncer` Container App,
`docker-compose.yml`, `docker-compose.vm.yml`) sets `ADMIN_USERS`/
`STATS_USERS` to the same DB user the application already authenticates
with, so `SHOW POOLS`/`SHOW STATS` work with no separate monitoring
credential to provision or rotate.

Two ways to actually see the numbers:

- **On demand, no APM required:** `GET /api/diagnostics/db-pool`
  (`require_true_super_admin`-gated) returns all three snapshots plus the
  currently-configured tuning knobs in one response -- the fastest way to
  check "what's happening right now" before changing a setting.
- **As a trend, when `OTEL_ENABLED=true`:** the same three snapshots are
  exported as OpenTelemetry gauges (`db.pool.sqlalchemy`, `db.pool.pgbouncer`,
  `db.pool.postgres`), sampled on the same interval `telemetry.py`'s exporter
  is already configured with, wherever traces already go. Useful for seeing
  how the numbers move under real, sustained load rather than a single
  point-in-time read.

None of the probing above ever holds a connection open outside the sampling
window itself -- each uses a short-lived, unpooled connection with a tight
connect timeout, the same pattern `_probe_postgres_connection_budget()`
already uses for pool sizing at startup.


## TLS topology

When `PGBOUNCER_HOST` is explicitly set (the self-hosted ACA/VM/Compose
pooler), the backend connects to the pooler's internal `6432` listener without
client-side TLS. The self-hosted PgBouncer then connects to Azure PostgreSQL
with `SERVER_TLS_SSLMODE=require`, so the database leg remains encrypted.

`DIRECT_DATABASE_URL` is never modified and continues to use
`sslmode=require`. When `PGBOUNCER_HOST` is unset (Azure managed PgBouncer),
the backend preserves the direct URL's TLS settings for the managed pooler
endpoint.

### Admin-console telemetry protocol

PgBouncer's special `pgbouncer` admin database accepts the PostgreSQL simple query protocol for `SHOW` commands. The application therefore uses a short-lived psycopg2/libpq connection for `SHOW POOLS`, `SHOW STATS`, and optional `SHOW CONFIG` telemetry instead of sending those commands through SQLAlchemy. A failed telemetry probe is non-fatal and must never be interpreted as application-route failure.


## Customer-facing concurrency balance

For the default ACA `Standard_B2s` PostgreSQL SKU (2 vCores), the infrastructure
auto-derives the self-hosted PgBouncer server pool at **5x vCores = 10** total
server connections. This is the upper end of the documented 2-5x starting range: it
adds application headroom without treating PostgreSQL's much larger
`max_connections` value as a target.

The application still keeps 10% of that pool unused, reserves 1 connection for
background DB work, and divides the remaining API budget across the worst-case
backend replica count. The resulting SQLAlchemy pool is stable (the process share
is `pool_size`, with `max_overflow=0` by default) because PgBouncer already handles
transaction-level reuse. This avoids creating short-lived overflow connections
just to service ordinary request bursts.

This is deliberately different from PostgreSQL's `max_connections` number. For
example, a server reporting `429` does **not** mean the application should try to
use 429 concurrent database sessions. PostgreSQL's CPU, memory, query workload,
PgBouncer's server pool, and the application's concurrency guard are separate
capacity layers.

## Per-operation statement-timeout escape hatch

The normal database transaction timeout is `DB_STATEMENT_TIMEOUT_MS=30000` (30s).
Keep that global default: it protects customer-facing requests from a slow query
holding a scarce connection indefinitely.

If a **specific, reviewed, bounded** operation later proves that it legitimately
needs more time, do not raise the global timeout. Use
`database.set_transaction_statement_timeout(session, milliseconds)` immediately
before that operation. The helper issues `SET LOCAL statement_timeout`, so the
override applies only to the current transaction and disappears automatically
when the transaction ends. This is safe with PgBouncer transaction pooling and
prevents a report-specific exception from weakening timeouts for customer traffic.

Do not use the escape hatch for an unbounded query. First add/verify indexes,
limit the date range/rows, and inspect the query plan; only then give that one
operation a justified longer timeout.
