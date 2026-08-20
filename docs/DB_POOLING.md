# Database connection routing and PgBouncer

The application now provisions PgBouncer as part of every deployment path that can support a private pooler. Application traffic is routed automatically; operators should **not** edit `DATABASE_URL` to switch between direct PostgreSQL and PgBouncer.

## Routing rule

- `USE_PGBOUNCER=true` (default): SQLAlchemy application traffic uses the configured pooler endpoint.
- `USE_PGBOUNCER=false`: SQLAlchemy uses the direct PostgreSQL URL. This is a deliberate break-glass setting only.
- `DIRECT_DATABASE_URL` is derived automatically from the supplied `DATABASE_URL` and is used for connection-budget probing and `pg_dump`, which must not run through transaction pooling.

## Deployment paths

### Azure Container Apps + managed PostgreSQL

Azure ACA uses Azure Database for PostgreSQL Flexible Server's managed PgBouncer. The backend keeps the managed PostgreSQL URL as its authoritative/direct URL and automatically changes only the port to `6432` when `USE_PGBOUNCER=true`; TLS remains enabled. There is no PgBouncer sidecar. The migration Job is direct-to-Postgres on `5432`.

The GitHub Environment/Repository Variable `USE_PGBOUNCER` controls the Bicep parameter. It defaults to enabled. For ACA, Bicep enables Azure-managed PgBouncer; for VM/local Compose the pooler service is already present and the application automatically targets `pgbouncer:6432`.

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
