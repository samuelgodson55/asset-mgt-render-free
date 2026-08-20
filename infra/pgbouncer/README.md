# PgBouncer configuration contract

Azure Flexible Server managed PgBouncer is the production pooler for ACA.
The deployment does **not** hard-code Azure's generic `default_pool_size=50`.
Instead, `infra/main.bicep` derives the managed pool from the Azure PostgreSQL
compute SKU at 4 connections per vCore (within Microsoft's recommended 2-5x
vCore starting range), unless `pgbouncerServerPoolSize` is explicitly set.

The application then:

1. caps that configured pool by the live PostgreSQL `max_connections` budget;
2. leaves `PGBOUNCER_SAFETY_MARGIN_PERCENT` (10% in ACA) unused;
3. reserves background DB capacity separately; and
4. distributes the remaining API budget across expected backend processes.

Azure's own `pgbouncer.default_pool_size` default is 50, but this repository
intentionally uses a smaller workload-derived starting point instead of
assuming that generic default is appropriate for every server size.
