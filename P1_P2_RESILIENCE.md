# P1 + P2 Resilience and Performance Validation

This is the validation layer for the Asset Inventory Quotes application's
**efficiency, speed, productivity and zero-downtime** goals.

## P1: dependency failure scenarios

Run the local stack first:

```bash
docker compose up -d --build
```

Then:

```bash
./scripts/chaos-test.sh
```

On Windows Git Bash, the same command works from the repository root.

The test deliberately stops and restores:

1. Redis
2. Celery worker
3. ErrorBeacon
4. PostgreSQL

The chaos script now tests backend `/readyz` **inside the backend container**.
It does not use `http://localhost:8080/readyz`, because port 8080 belongs to
the frontend proxy and that proxy does not expose backend readiness directly.

Expected behavior:

| Failure | Expected application behavior |
|---|---|
| Redis down | API stays live; login rate limiting fails open |
| Worker down | API stays live; queued background work waits |
| ErrorBeacon down | API stays live; monitoring failure never becomes an application failure |
| PostgreSQL down | `/healthz` remains 200; `/readyz` becomes 503 so traffic is not sent to the replica |
| PostgreSQL restored | backend `/readyz` returns 200 again |

The suite also verifies that a real frontend-proxied telemetry request gets a
request ID, that the backend sends that same ID to ErrorBeacon as
`X-Request-ID`, and that ErrorBeacon persists the same ID. This is the
end-to-end correlation test that the standalone `/v1/test` call cannot prove.

### Additional P1 tests

Backend:

```bash
cd backend
pytest tests/test_resilience.py -q
```

Frontend:

```bash
cd frontend-app
npm test -- --run tests/lib/api-retry.test.ts
```

Safe browser reads automatically retry a brief network/502/503/504 failure up
to three attempts with exponential backoff and jitter. Mutating requests are
never automatically retried, preventing accidental duplicate writes.

## Request correlation and logs

Correlation now crosses the entire monitoring path:

```text
Browser / client
      │
      ▼
FastAPI request ID
      │
      ├── backend JSON logs
      │
      ├── ErrorBeacon payload
      │       │
      │       └── X-Request-ID forwarded to ErrorBeacon
      │
      └── Telegram incident
```

ErrorBeacon also stamps its own Uvicorn access logs with the same request ID.
Therefore `docker compose logs errorbeacon` can be searched using the exact
ID shown in Telegram. Use:

```bash
./scripts/trace-request.sh YOUR_REQUEST_ID --since 15m
```

For ErrorBeacon itself:

```bash
docker compose logs --since 15m errorbeacon | grep YOUR_REQUEST_ID
```

A standalone ErrorBeacon `/v1/test` request generates one ID at the
ErrorBeacon boundary. An application-originated error reuses the Asset
application's existing ID across both services.

## P2: performance/load testing

Basic health/load gate:

```bash
python scripts/load-test.py --url http://localhost:8080/healthz --requests 500 --concurrency 25 --max-error-rate 0 --max-p95-ms 1000
```

Authenticated read-path test:

```bash
python scripts/load-test.py --base-url http://localhost:8080 --path '/api/assets?limit=25' --login-email 'YOUR_USER' --login-password 'YOUR_PASSWORD' --requests 300 --concurrency 20 --max-error-rate 0 --max-p95-ms 1000
```

The runner reports:

- requests
- concurrency
- throughput (requests/sec)
- expected HTTP status
- HTTP status distribution
- error rate
- p50 latency
- p95 latency
- p99 latency

The load runner uses persistent HTTP/1.1 connections by default, with one
connection per worker thread. Responses are fully consumed before a connection
is reused, including responses larger than 4 KiB, so unread response bytes
cannot corrupt the next request. Stale/closed keep-alive connections are
reconnected and retried once at the transport layer. HTTP 4xx/5xx responses are
not retried and are counted as failures unless they match `--expected-status`.

Use `--no-keep-alive` only as a diagnostic comparison when investigating
cold-connection or Docker/host networking behaviour.

Use the same command against the **staging ACA revision before 100% traffic**
and against the inactive VM blue/green slot before cutover.

### Recommended initial gates

These are starting gates, not universal SLAs:

- Error rate: **0%** for health/readiness
- Error rate: **<1%** for authenticated read load while establishing a baseline
- p95 health endpoint: **<1 second**
- p95 normal API reads: establish a baseline, then fail a release if it regresses materially
- No sustained growth in DB connections, Redis latency, CPU or memory during the run

## Concurrency protections added

The application now serializes the critical stock-changing operations:

- quotation fulfillment locks the quotation row and the affected asset rows
- checkout returns lock the checkout row and asset row
- stock reconciliation locks the asset row
- asset isolation locks the asset row
- asset recall locks the asset row
- direct asset checkout already locks the asset row

This prevents duplicate fulfillment, double returns, overselling and stale
`available_quantity` writes under concurrent requests.

## Zero-downtime rule

Never use a destructive database migration in the same release that still has
old code serving traffic. Use:

**Expand → deploy compatible code → migrate/use → contract later.**

The deployment health gates must remain:

`/healthz` → `/readyz` → direct smoke test → staged traffic → 100% → old revision retirement.

## P0: final operator validation

After P1/P2 are in the repository, perform the real-environment checks:

1. Full backend pytest suite in CI.
2. Full React lint/typecheck/test/build in CI.
3. Bicep compilation in CI.
4. Build/push both production images.
5. Deploy a staging/inactive ACA revision and run `load-test.py`.
6. Run the VM blue/green deployment and run `load-test.py` against the inactive slot.
7. Perform a real rollback test.
8. Verify Telegram ErrorBeacon alerts during a deliberately generated 500.
9. Verify ErrorBeacon remains reachable while the application revision is rolled back.
10. Record p50/p95/p99 baselines and use them as the release performance budget.

## Request ID semantics

Different IDs in backend and ErrorBeacon `GET /healthz` log lines are **expected**: each Docker health probe is a separate HTTP request. IDs must match only across a forwarded request chain such as `browser -> nginx -> backend -> ErrorBeacon -> Telegram`. FastAPI forwards its request ID as `X-Request-ID`; ErrorBeacon preserves it.

## Redis outage behavior

Dedicated Compose `worker` and `beat` services use `CELERY_BROKER_CONNECTION_MAX_RETRIES=none` so a Redis outage does not cause crash/restart loops. They retry in-process while the web/API tier remains independent. Render/embedded-worker deployments keep the bounded default unless overridden.


## Windows Git Bash and local database

The chaos script exports `MSYS_NO_PATHCONV=1` so Git Bash does not rewrite `/readyz` when invoking Docker. The local Compose stack uses a one-shot `migrate` service with `alembic upgrade head`; `AUTO_INIT_DB` is disabled for the Compose backend to avoid an unversioned `create_all()` database. Do not run `alembic stamp head` for normal local setup.
