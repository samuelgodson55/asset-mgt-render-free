# PgBouncer configuration contract

Azure Container Apps uses the repository's **self-hosted `edoburu/pgbouncer` Container
App**. It is deliberately separate from Azure Flexible Server's managed PgBouncer
feature so the default Burstable PostgreSQL SKU remains supported.

The deployment:

1. derives one authoritative server-side pool budget from the PostgreSQL SKU;
2. splits that budget across the fixed PgBouncer replicas;
3. keeps `PGBOUNCER_SAFETY_MARGIN_PERCENT` (10% in ACA) unused as headroom;
4. reserves background DB capacity separately; and
5. distributes the remaining API budget across expected backend processes.

The backend keeps `DIRECT_DATABASE_URL` pointed at PostgreSQL and routes application
traffic to `pgbouncer:6432` when `USE_PGBOUNCER=true`.

## TLS topology

The direct PostgreSQL URL uses `sslmode=require`.

For the self-hosted PgBouncer topology, `PGBOUNCER_HOST` is explicitly set to the
internal `pgbouncer` service name. The backend therefore uses `sslmode=disable`
**only on the client-to-PgBouncer leg**. This is required because the current
`edoburu/pgbouncer` listener on 6432 does not terminate client TLS.

The PgBouncer-to-Azure-PostgreSQL leg remains encrypted because the PgBouncer
container uses:

`SERVER_TLS_SSLMODE=require`

This means disabling TLS on the internal pooler listener does **not** disable TLS
to the actual database.

When `PGBOUNCER_HOST` is unset, the routing layer treats the endpoint as Azure
managed PgBouncer and preserves the direct URL's SSL settings.

`DIRECT_DATABASE_URL` is never rewritten by the routing layer.
