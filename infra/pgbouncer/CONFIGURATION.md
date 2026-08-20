# PgBouncer sizing rules

## Azure ACA

- `pgbouncer.enabled=true`
- `pgbouncer.default_pool_size` is derived from the requested Azure PostgreSQL
  SKU at `4 x vCores` unless an explicit `pgbouncerServerPoolSize > 0` is supplied.
- Application safety margin: 10% (`PGBOUNCER_SAFETY_MARGIN_PERCENT`).
- Background DB reserve: 1 connection by default.
- The application probes direct PostgreSQL `max_connections` and
  `superuser_reserved_connections` and never admits more than the live safe
  database budget.

## Direct PostgreSQL fallback

When `USE_PGBOUNCER=false`, the application derives its pool from the live
PostgreSQL connection settings and subtracts the configured safety margin.
A probe failure falls back to a deliberately small bounded pool; it never
expands the pool.
