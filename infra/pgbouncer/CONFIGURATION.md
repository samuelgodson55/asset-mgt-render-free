# PgBouncer sizing and TLS rules

## Azure ACA

- A self-hosted `edoburu/pgbouncer` Container App is used.
- The total server pool is derived from the PostgreSQL SKU at `4 x vCores` unless
  `pgbouncerServerPoolSize > 0` is supplied explicitly.
- The application leaves 10% of the effective pool unused for operational headroom.
- Background DB work reserves 1 connection by default.
- The application probes direct PostgreSQL `max_connections` and
  `superuser_reserved_connections` and never admits more than the live safe budget.
- PgBouncer's upstream PostgreSQL connection uses `SERVER_TLS_SSLMODE=require`.
- The backend's client connection to the internal `pgbouncer:6432` listener uses
  `sslmode=disable` because the self-hosted listener does not terminate TLS.

## Direct PostgreSQL fallback

When `USE_PGBOUNCER=false`, the application uses `DIRECT_DATABASE_URL` unchanged.
For Azure PostgreSQL that URL normally contains `sslmode=require`.

## Azure managed PgBouncer

If a future deployment uses Azure's managed PgBouncer endpoint instead of the
self-hosted Container App, leave `PGBOUNCER_HOST` unset. The routing layer then
keeps the PostgreSQL URL's existing SSL settings, including `sslmode=require`.
