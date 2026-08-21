# Snipe-IT Lite — Continuous SRE Strategy

This is the operating rhythm for keeping the Azure Container Apps
deployment healthy day-to-day, not a one-time setup task. It assumes
you've already read `DEPLOYMENT.md`'s **Health Checks & Monitoring** and
**Azure Container Apps Production Deployment** sections — this document
picks up where those leave off: what to actually *do*, on a schedule,
using what's already built.

**Starting point worth naming:** this app already has more reliability
plumbing than most projects this size — `/healthz` + `/readyz` split
(now backed by real Docker-level `HEALTHCHECK` instructions on the
`backend` and `frontend` images too, not just Container Apps' own
probes — see DEPLOYMENT.md's **Health Checks & Monitoring**), a global
unhandled-exception safety net (`middleware/error_handling.py`) that
guarantees every 500 — not just the ones a route explicitly raises
itself — logs a full traceback tagged with that request's correlation
ID and hands the caller back that same ID to report, Redis-backed rate
limiting that's replica-safe, RedBeat so `beat` never double-fires
across replicas, a migrate → deploy → smoke-test → auto-rollback
pipeline, structured JSON logs with a correlation ID, and both local +
off-box (Google Drive) backups. The strategy below is mostly about
**using that machinery on a schedule**. All three gaps this document
originally flagged (or has since grown to cover) are now closed at the
code level: alerting on top of the Log Analytics data you're already
paying for is implemented in `infra/main.bicep`, opt-in behind one
secret (see §2); automated dependency updates are already running via
`.github/dependabot.yml` (see §4); and distributed tracing
(OpenTelemetry, `backend/telemetry.py`) — which answers the question
alerting and logs can't, "which *part* of a slow/failing request was
actually slow" — is opt-in behind `OTEL_ENABLED` (see §3's weekly
cadence and §6.5). What's left for all three is cadence and turning the
relevant flag on, not missing tooling. `docker-compose.yml`'s local
`jaeger` service now runs current-stable Jaeger v2 (v1 is EOL as of
2025-12-31 — see that service's own comment for the full migration
note), and §6.6 below adds a request-ID-first fast path
(`docker compose logs`, `az containerapp logs show`, and request-ID/trace-ID queries) for triaging an
error in seconds using nothing but `docker compose logs`, whether or not
tracing itself is even turned on yet.

---

## Table of contents

1. [Service Level Objectives](#1-service-level-objectives-keep-these-small-and-honest)
2. [Close the alerting gap](#2-close-the-alerting-gap-do-this-once-first)
3. [The continuous cadence](#3-the-continuous-cadence)
4. [Dependency-update gap — closed](#4-dependency-update-gap--closed)
5. [Runbooks](#5-runbooks-write-these-down-before-you-need-them)
6. [Troubleshooting `az containerapp exec`](#6-troubleshooting-az-containerapp-exec)
   - [6.1 Notification emails silently never sending](#61-postmortem-notification-emails-silently-never-sending-despite-correct-smtp-secrets)
   - [6.2 A running replica gets pulled out from under an open `exec` session](#62-a-running-replica-gets-pulled-out-from-under-an-open-exec-session)
   - [6.3 `ClusterExecFailure` / websocket `close 1011` on an interactive shell](#63-clusterexecfailure--websocket-close-1011-on-an-interactive-shell)
   - [6.4 Rehearsing the partition-drop runbook safely](#64-rehearsing-the-partition-drop-runbook-safely)
   - [6.5 Using distributed traces to find where time actually went](#65-using-distributed-traces-to-find-where-time-actually-went)
   - [6.6 Fast request-ID triage without opening Jaeger](#66-fast-request-id-triage-without-opening-jaeger)
7. [Audit log partitioning & annual archive](#7-audit-log-partitioning--annual-archive)
8. [What to deliberately *not* do](#8-what-to-deliberately-not-do)

---

## 1. Service Level Objectives (keep these small and honest)

For a single-tenant internal tool, don't over-engineer error budgets.
Three numbers are enough to know if you're drifting:

| SLI | Target | How to measure |
|---|---|---|
| Availability | 99.5% monthly (~3.6 hrs/month) | `/healthz` probe success rate (already polled by Container Apps) |
| Readiness | 99% monthly | `/readyz` — catches "schema doesn't match code" and DB blips separately from liveness |
| Checkout-path latency | p95 < 800ms | Log Analytics on `backend`'s request logs (see queries below) — once you know it's drifting, §6.5 covers how to find *which part* of the request is actually slow via distributed tracing |
| Backup freshness | A restorable backup < 25 hrs old, always | `services/backup_service.py`'s daily job + Drive upload succeeding |

If you blow through the availability target two months running, that's
your signal to spend a cycle on reliability work instead of features —
that's the whole point of having the number.

---

## 2. Close the alerting gap (do this once, first)

`main.bicep` deliberately dropped Application Insights to save on
ingestion cost — correct call for this app's size — but by default nothing
is *watching* the Log Analytics workspace and paging you. `infra/main.bicep`
now includes three Azure Monitor **scheduled query alerts** (billed
per-rule, cents/month, no Application Insights required) that close this,
as code — but they're opt-in: they only deploy if the `ALERT_EMAIL_ADDRESS`
GitHub secret is set (see DEPLOYMENT.md's setup table). Set that secret and
re-run `infra-deploy.yml` for the environment you want alerting on, and the
action group + all three rules below appear automatically. Leave it unset
and this section costs nothing and creates nothing, exactly as before.

Three alerts cover the app's actual failure modes:

**a) Backend error-rate spike** (KQL against the Container Apps console logs):
```kql
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "backend"
| where Log_s has "ERROR" or Log_s has "\"status_code\":5"
| summarize ErrorCount = count() by bin(TimeGenerated, 5m)
| where ErrorCount > 10
```
Every one of those `ERROR` lines now carries a `request_id` field —
including for a genuinely unhandled exception, thanks to
`middleware/error_handling.py`'s global safety net (see that file's
docstring) — so once this alert fires, add `| project TimeGenerated,
Log_s` and grep the matching `request_id` values straight out of the
JSON; if a user or support ticket already has one (it's in the error
response body they saw), you can jump directly to their specific
request instead of scanning every ERROR line in the window.

**b) `/readyz` failing** (schema/code mismatch or DB unreachable — the
case a liveness-only check would miss):
```kql
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "backend"
| where Log_s has "readyz" and Log_s has "\"ready\": false"
| summarize count() by bin(TimeGenerated, 5m)
```

**c) Daily backup didn't run** (absence-of-signal alert — alert if the
success log line is *missing* in a 26-hour window, not if a failure line
appears):
```kql
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "backend"
| where Log_s has "backup" and Log_s has "success"
| summarize LastSuccess = max(TimeGenerated)
| extend HoursSinceSuccess = datetime_diff('hour', now(), LastSuccess)
| where HoursSinceSuccess > 26
```

All three are wired to one Action Group (email only for now — add an SMS/
voice receiver directly on the action group in the Azure Portal if you want
that too) so you get a push notification instead of finding out from a
user. This was a couple hours of one-time Azure Portal work; it's now
`infra/main.bicep`'s `alertActionGroup`/`alertBackendErrorRate`/
`alertReadyzFailing`/`alertBackupMissing` resources instead, gated behind
the `alertEmailAddress` parameter.

**First-deploy ordering gotcha:** don't set `alertEmailAddress` /
`ALERT_EMAIL_ADDRESS` on a brand-new environment's very first deploy. All
three queries above read `ContainerAppConsoleLogs_CL`, which Azure only
creates the first time a log line actually lands in it — on a fresh Log
Analytics workspace it doesn't exist yet, and `scheduledQueryRules`
validates its KQL against the live schema at deploy time, so the deploy
fails outright. Deploy once with the alerting parameter unset, let the apps
run briefly so the table materializes, then set it and re-run infra-deploy
to layer the alert rules on afterward. See `infra/main.bicep`'s
`alertEmailAddress` parameter description for the same note.

---

## 3. The continuous cadence

This is the actual "something I can be doing continuously" part —
put it in a recurring calendar block or a lightweight ticket template.

### Daily (5 minutes, or fully automate via §2's alerts)
- [ ] Confirm no alert fired overnight (or check Container Apps logs if
  alerting isn't wired up yet).
- [ ] Glance at `/readyz` on the live URL if you skipped the alert.

### Weekly (~20 minutes)
- [ ] Skim Container Apps replica count history — did `backend` scale
  above 1 unexpectedly? Worth knowing *why* even if it recovered on its
  own (traffic spike vs. a hung request pool).
- [ ] Check Redis memory (per DEPLOYMENT.md's note: it should track "a
  handful of job-result keys and rate-limit counters," not file sizes —
  a jump means something reverted to storing export files in Redis).
- [ ] Review the GitHub Actions run history for `ci.yml` — any flaky
  test worth fixing before it becomes "everyone just re-runs it"?
- [ ] *(if `OTEL_ENABLED=true` — see §6.5)* Sort the last week's traces
  by duration (Application Insights: **Performance** blade, or Jaeger:
  sort by Duration) and skim the slowest handful. Catches a
  quietly-degrading query or dependency before it's slow enough to blow
  the p95 SLO above and trigger an actual incident.

### Monthly (~1 hour)
- [ ] **Restore drill**: download the latest backup and restore it into
  a scratch Postgres (not production!) to confirm the file is actually
  restorable, not just "present." A backup you've never restored is a
  hope, not a backup.
- [ ] Rotate `JWT_SECRET_KEY` and confirm all replicas picked it up
  cleanly (a mismatched secret across replicas manifests as intermittent
  "please log in again" — see DEPLOYMENT.md's safety checklist). Do this
  during low-traffic hours since it invalidates existing sessions.
- [ ] Review `LOGIN_RATE_LIMIT_MAX`/`ACCOUNT_LOCKOUT_*` thresholds against
  actual observed traffic from the past month.
- [ ] Check for pending Trivy CRITICAL findings that were allowed through
  as non-blocking, if any accumulated.

### Annual (once a year, or whenever disk space actually requires it)
- [ ] **Audit log partition retirement**: check `audit_logs` partition
  sizes (`docker compose exec backend python scripts/audit_partition_status.py`
  on a VM/docker-compose deployment, or `az containerapp exec --name backend
  --resource-group <rg> --command "python scripts/audit_partition_status.py"`
  on Azure Container Apps) and, for any year that's safe to let go of,
  follow §7's runbook below in full — verify the Drive backup restores
  cleanly in a scratch setup BEFORE dropping anything. This is the only
  place in the whole cadence that permanently discards data, so it's
  deliberately its own once-a-year item, not folded into the
  monthly/quarterly lists above.

### Quarterly (~half a day)
- [ ] **Dependency sweep**: bump `backend/requirements.txt` and the two
  `package-lock.json`s (build-frontend, build-tailwind) deliberately,
  run the full `ci.yml` suite, and merge as its own PR — not mixed into
  a feature branch. (See §4 — Dependabot automates the *finding*, you
  still own the *merging*.)
- [ ] Re-run a load test against a staging-like environment ahead of any
  known traffic spike (e.g. start-of-semester checkout rush) and sanity
  check `backendMaxReplicas` (currently 3) and `frontendMinReplicas`
  against what you saw.
- [ ] Postmortem review: re-read any incident write-ups from the quarter
  (see §5) — are the same root causes repeating?
- [ ] Confirm the Azure Container Apps runtime/base images
  (`postgres`, `redis` official images) haven't fallen multiple major
  versions behind.

---

## 4. Dependency-update gap — closed

**Done:** [`.github/dependabot.yml`](../.github/dependabot.yml) now exists in
the repo, covering every package manifest — `backend/requirements.txt`,
all three `package.json`s (`build-frontend`, `build-tailwind`,
`frontend/tests`), both Dockerfiles' base images, and the GitHub Actions
themselves — on a weekly schedule.

**What this changes about your job:** dependency drift stops being
something you have to remember to go check for quarterly, and becomes a
steady trickle of small PRs that show up on their own. Concretely:

- **Weekly**, Dependabot opens PRs (up to the configured
  `open-pull-requests-limit` per ecosystem) against whichever manifests
  have updates available. Each one runs through the exact same `ci.yml`
  gate as a human PR — no extra CI wiring was needed, since `ci.yml`
  already triggers via `workflow_call` on any push/PR.
- **This does NOT auto-merge anything.** Dependabot proposes, you decide.
  The quarterly checklist item below ("dependency sweep") is now "review
  and merge the PRs that accumulated" rather than "go hunt for what's
  stale" — a meaningfully smaller task.
- It's complementary to `ci.yml`'s existing `pip-audit` step, not
  redundant with it: `pip-audit` catches "a CVE was just published against
  a version we already have pinned" on every push; Dependabot catches
  "a newer version exists" on a schedule, CVE or not. You want both —
  one covers a sudden new vulnerability, the other prevents the slow
  multi-year drift that makes a big-bang upgrade painful later.

The quarterly checklist item in §3 now reads, in practice: skim whatever
Dependabot PRs piled up since last quarter, merge the safe ones in one
batch, and only manually go looking for anything outside its scope (e.g.
the Postgres/Redis Docker Hub image *major* version, which Dependabot will
flag but you'll still want to read the release notes for before bumping).

---

## 5. Runbooks (write these down *before* you need them)

Keep these as a short doc your future self (or anyone else) can follow
at 2am without reasoning from scratch:

- **Backend replica count stuck > 1 and not recovering** → check for a
  hung request pool or a slow downstream (Postgres connection limit is
  the usual suspect at small scale) before assuming it's just load.
- **Login failures spike, but only for some users** → check `JWT_SECRET_KEY`
  consistency across replicas first (see monthly rotation note above) —
  this is the textbook "intermittent, replica-specific" symptom.
- **`/readyz` returns 503 after a deploy** → almost always means the
  migrate job (`migrateJob` in `main.bicep`) didn't finish before traffic
  shifted, or a manual image update bypassed the pipeline. Don't restart
  the container — check `alembic upgrade head` ran.
- **Redis is down** → this takes out rate limiting, exports, RedBeat
  scheduling, *and* the backup lock all at once, since it's deliberately
  the one shared instance. There's no automatic failover here (by
  design, per DEPLOYMENT.md — "use a managed/clustered Redis for real
  HA"); if this graduates beyond a small internal tool, this is the
  first thing to move to Azure Cache for Redis.
- **A user/support ticket reports "an unexpected error occurred" with a
  request ID** → that ID (and the generic message itself) comes from
  `middleware/error_handling.py`'s global handler — grep it directly
  against Log Analytics (`Log_s has "<request_id>"`) or `docker compose
  logs backend | grep <request_id>` locally; the matching log line has
  the full traceback, `exc_info` included, because it's the SAME line
  the handler wrote when the exception happened.
- **`docker compose ps` shows `worker` or `beat` as constantly
  restarting, but `backend` looks fine** → check `docker compose logs
  worker`/`beat` first, not the container's health status column:
  `worker`'s healthcheck is a real Celery `inspect ping` (a genuine
  liveness signal), but `beat` deliberately has its Docker healthcheck
  disabled (see `docker-compose.yml`'s comment on that service — there's
  no reliable liveness probe for a RedBeat-scheduled process), so a
  restart loop there means the container is actually exiting/crashing,
  not failing a health probe — check for the RedBeat
  lock-contention/`LockNotOwnedError` case documented in
  `celery_app.py`'s comments first.
- **Need to roll back a bad release** → if the deploy is still running (or
  just finished failing), you likely don't need to do anything —
  `deploy-azure-aca.yml`'s blue-green rollout already flips traffic back
  to the still-active revision automatically on a failed health check
  or smoke test (see DEPLOYMENT.md's "Zero-downtime rollout mechanics" and
  Rollback sections). Watch it happen with
  `bash .github/scripts/aca-blue-green.sh status backend <resource-group> --watch`.
  For a rollback requested well after a deploy finished cleanly (the old
  active revision is already spun down), use `workflow_dispatch` on
  `deploy-azure-aca.yml` with the previous `image_tag`, per DEPLOYMENT.md's
  Rollback section — this still gets a fresh migrate/health-gate/smoke-test
  pass, just like a forward deploy. Don't hand-run
  `az containerapp update --image` directly — with `backend`/`frontend` now
  in Multiple revision mode with pinned traffic, that creates a new
  revision at 0% traffic and silently routes nothing to it; use
  `aca-blue-green.sh rollout` instead (see DEPLOYMENT.md's Rollback
  section for the exact commands).

For each real incident going forward, write a short postmortem: what
broke, what the user-facing impact was, what the fix was, and one
concrete follow-up action. Even three sentences beats nothing — the
value is almost entirely in the "did we do the follow-up action" part.

---

## 6. Troubleshooting `az containerapp exec`

Real incidents from operating the staging environment, kept here (rather
than only in a closed support ticket) because both the root causes and
the workarounds are non-obvious and worth not re-discovering from
scratch next time.

### 6.1 Postmortem: notification emails silently never sending, despite correct SMTP secrets

**Symptom:** `NOTIFICATIONS_ENABLED=true`, `SMTP_HOST`/`SMTP_USERNAME`/
`SMTP_PASSWORD`/`SMTP_FROM_EMAIL` all correctly set (confirmed both as
GitHub Actions secrets and, after re-running `infra-deploy.yml`, as live
Container App env vars/secrets) — no emails ever arrived, not the daily
digest, not extension-request notifications, with nothing useful in
`az containerapp logs show` beyond the normal uvicorn boot lines.

**Root cause:** two files named `start.sh` existed in the repo — one at
the project root (correctly launching the embedded Celery worker+beat
when `RUN_EMBEDDED_WORKER=true`) and the stale original at
`backend/start.sh` (missing that logic entirely, uvicorn-only). The
backend CI build (`.github/workflows/deploy-azure-aca.yml`) builds with `context: backend`, so
`backend/Dockerfile`'s `COPY . /app/` only ever picked up
`backend/start.sh` — the *wrong* one. `infra/main.bicep` correctly sets
`RUN_EMBEDDED_WORKER=true` on the `backend` Container App, but the script
actually running inside the shipped image never read that variable, so
`celery_app.py`'s `.delay(...)` calls (`tasks.send_email_task`, the daily
digest/due-soon Beat jobs, audit exports) all queued into Redis with
**no worker ever consuming them** — silent, not loud, and completely
invisible from `NOTIFICATIONS_ENABLED`/SMTP config alone, because those
settings were never the problem.

**Fix:** the corrected `start.sh` now lives at (and only at)
`backend/start.sh` — the one path the Docker build context can actually
see — and the confusing root-level duplicate was deleted so this can't
silently drift again. Confirm the fix landed by checking for this line
near the top of a fresh `az containerapp logs show` output, right before
the uvicorn line:
```
start.sh: RUN_EMBEDDED_WORKER=true -- launching embedded Celery worker+beat in the background (low priority)
```
No such line (or a boot log jumping straight to `start.sh: lean mode
enabled ...`) means the embedded worker isn't starting, regardless of
what SMTP/notification settings say.

**Takeaway for future changes to `backend/start.sh` / `render-start.sh`:**
before trusting a fix to either script, verify with `find . -iname
start.sh` (or equivalent) that there isn't a second copy elsewhere in the
tree that the actual Docker build context is silently preferring instead.

**Separately worth remembering:** `deploy-azure-aca.yml`
only ever runs `az containerapp update
--image ...` — it never touches env vars or secrets. All SMTP/
notification/Google-Drive configuration is applied exclusively by
`infra-deploy.yml` (a separate, manually-triggered `workflow_dispatch`
pipeline). Updating a GitHub *secret* alone does nothing until
`infra-deploy.yml` is re-run — a normal push to `develop`/`main` will
**not** pick up a changed SMTP secret on its own.

### 6.2 A running replica gets pulled out from under an open `exec` session

**Symptom:** `az containerapp exec --name backend ... --command /bin/sh`
connects successfully (`INFO: Successfully connected to container...`),
then dies mid-session with:
```
ERROR: {"Error":{"Code":"ClusterExecFailure","Message":"...websocket: close 1011 (internal server error)...code: 500."}}
```

**Root cause (confirmed via `az containerapp logs show --type system`):**
staging's `backend` runs with `backendMinReplicas: 0` (see
`infra-deploy.yml`'s replica-floor logic). `az containerapp exec` does
**not** count as activity that resets KEDA's idle timer — only real HTTP
traffic through ingress does — so if nothing hits `/api/*` while you're
in the shell, KEDA scales the replica to zero underneath you:
```
"Msg": "Deactivated apps/v1.Deployment k8se-apps/backend--0000015 from 1 to 0", "Reason": "KEDAScaleTargetDeactivated", "EventSource": "KEDA"
"Msg": "Container 'backend' was terminated with exit code '' and reason 'ManuallyStopped'"
```

**Fix — float the floor for the duration of the session:**
```bash
az containerapp update --name backend --resource-group rg-snipeit-lite-staging --min-replicas 1
# ...do your work...
az containerapp update --name backend --resource-group rg-snipeit-lite-staging --min-replicas 0
```
Always set it back to `0` afterward — staging is deliberately scale-to-
zero for cost, and this is the one thing that overrides it.

### 6.3 `ClusterExecFailure` / websocket `close 1011` on an interactive shell

**Symptom:** even with `min-replicas: 1` and a freshly-created, healthy
replica (`healthState: Healthy`, no restarts, clean application logs —
Celery worker connected to Redis, `beat: Acquired lock`, nothing
resembling an OOM kill or crash), `/bin/sh` still fails instantly with
the exact same `ClusterExecFailure` / `websocket: close 1011` error as
§6.2, on three separate revisions in a row.

**Root cause:** an Azure Container Apps platform-side quirk in how
`az containerapp exec` handles interactive TTY sessions specifically —
not an application or resource-pressure issue (ruled out: app logs were
completely clean, no memory/restart signal anywhere). Multiple
near-identical reports exist against `microsoft/azure-container-apps` on
GitHub with the same signature and no published root cause; this is a
known platform gap, not something fixable from the app side.

**Workaround that reliably works — skip the interactive shell, run a
single non-interactive command instead:**
```bash
az containerapp exec --name backend --resource-group rg-snipeit-lite-staging \
  --command "python scripts/audit_partition_status.py"
```
This succeeded cleanly every time it was tried, where `--command /bin/sh`
never did. Use this pattern (one full command per `exec` call) for any
one-off diagnostic/admin script instead of dropping into an interactive
shell on Azure. If you genuinely need an interactive session, try the
Azure Portal's Container App → Console tab first (different client path,
sometimes succeeds where the CLI's websocket implementation doesn't), or
update the CLI/extension (`az upgrade && az extension add --name
containerapp --upgrade`) before assuming it's something else.

**Shell-quoting note:** don't try to inline multi-line Python through
`--command "python -c \"...\""` — surviving MINGW64/bash → `az exec` →
container-shell → Python quoting all at once is not worth the fight (see
`scripts/dev_seed_fake_old_partition.py`'s existence, §6.4, which is a
direct result of exactly this pain). Write a real script and call it by
path instead.

### 6.4 Rehearsing the partition-drop runbook safely

Section 7's annual retirement runbook is deliberately never automated
against real data — which also means there was previously no safe way to
practice the actual `DROP TABLE` step without either waiting for a real
year to need retiring, or improvising raw SQL by hand through the
`exec`/shell-quoting minefield above. `scripts/dev_seed_fake_old_partition.py`
exists specifically to close that gap: it creates one obviously-fake old
year (`audit_logs_y2020` by default) with a handful of disposable,
clearly-tagged rows (`FAKE_SEED_DATA_FOR_PARTITION_DROP_TESTING`), and
hard-refuses to run at all if `ENVIRONMENT=production` — see that
script's own module docstring. Combine it with §6.3's non-interactive
`--command` pattern:
```bash
az containerapp exec --name backend --resource-group rg-snipeit-lite-staging \
  --command "python scripts/dev_seed_fake_old_partition.py"

az containerapp exec --name backend --resource-group rg-snipeit-lite-staging \
  --command "python scripts/audit_partition_status.py"
```
then follow Section 7's Step 2 (`psql "$DATABASE_URL"` →
`DROP TABLE audit_logs_y2020;`) exactly as you would for a real year, and
re-run `audit_partition_status.py` once more to confirm it's gone.

### 6.5 Using distributed traces to find where time actually went

§2's alerts and §1's SLOs both tell you *that* something is wrong (error
rate spiked, p95 latency drifted) — neither tells you *where* the time
actually went inside a single slow/failing request. That gap is what
distributed tracing (`backend/telemetry.py`, `OTEL_ENABLED`) closes; it's
off by default, so this section assumes you've turned it on for at least
one environment (README.md's **Distributed Tracing** section covers the
one-time setup, locally via Jaeger or in Azure via
`otelAzureMonitorEnabled`).

**Two gotchas that silently produce "tracing looks completely
non-functional" with no error anywhere, worth ruling out first if a
trace you expect to exist isn't showing up:**
- `OTEL_ENABLED` defaults to `false` in `.env.example` — copying the
  template alone does NOT turn tracing on, that line has to be flipped
  to `true` explicitly. Confirm it actually took by grepping
  `docker compose logs backend | grep "OpenTelemetry:"` — you should see
  `OTLP span exporter configured (...)`; if that line never appears,
  the exporter never even armed itself, and nothing below this point
  will find anything.
- Locally, `jaeger` only starts with `docker compose --profile tracing
  up` — plain `docker compose up` never starts it at all, and
  `backend`/`worker`/`beat` will still try (and silently fail) to export
  spans to a container that was never running. `docker-compose.yml`'s
  `jaeger` service now runs current-stable **Jaeger v2**
  (`jaegertracing/jaeger:2.19.0` — v1 hit end-of-life on 2025-12-31 and
  is no longer receiving updates); see that service's own comment for
  the full "why v2" reasoning if you're pinning a newer patch release
  later.

**Scenario: §2's backend error-rate alert fired, or checkout-path p95
drifted above 800ms.**

1. Get a concrete `request_id` to start from — either from the alert
   query's `Log_s` output directly (§2a already tells you to `| project
   TimeGenerated, Log_s`), or from a user/support ticket (every error
   response body includes the `request_id` that produced it, thanks to
   `middleware/error_handling.py`'s global safety net).
2. Every structured log line for that request also carries `otelTraceID`
   (see `telemetry.py`'s module docstring for exactly how logs and
   traces get tied together) — grep the same log window for that
   `request_id` once to pull the matching `otelTraceID` out alongside it.
3. Open that trace:
   - **Jaeger** (local): paste the trace ID into the search box at
     `http://localhost:16686`, or just click through from a recent trace
     list if you were already looking at one.
   - **Application Insights** (Azure): **Investigate → Transaction
     search**, paste the trace ID, or run the KQL from README.md's
     **Distributed Tracing** section (`union requests, dependencies |
     where operation_Id == "<trace id>"`).
4. Read the waterfall top-down. The HTTP request span is the outermost
   bar; everything nested under it — SQL query spans, an enqueued Celery
   task span if this request kicked one off — is a candidate for "this
   is where the time went." A single 600ms SQL span buried under an
   otherwise-fast request points at a missing index or an N+1 query
   loop in that specific service function; a request that finishes fast
   itself but whose *child* Celery task span (in `backend-worker`,
   visible as a continuation of the SAME trace — see `telemetry.py`'s
   "WHAT GETS INSTRUMENTED" section for why Celery propagates trace
   context across the Redis broker) runs long points you at
   `tasks/`/`services/` instead of `api/`.
5. If the slow span is a SQL query with no obvious index problem, cross-
   reference it against Postgres itself: Flexible Server's own **Query
   Performance Insight** (Azure Portal, on the `postgresServer`
   resource) shows the same query ranked by total time across ALL
   requests, not just this one trace — useful for telling "this one
   request hit a cold cache" apart from "this query is slow for
   everyone, every time."

**What this does NOT replace:** the structured JSON logs
(`LOG_FORMAT=json`) and §2's alerts are still the first thing to check —
they're always-on (well, logs are; alerts need `ALERT_EMAIL_ADDRESS` set)
and need no extra setup. Tracing is the *second* step once you already
know roughly which request/time-window to look at, not a replacement for
having logs/alerts in the first place.

---

### 6.6 Fast request-ID triage without opening Jaeger

The current repository does not ship the older `scripts/trace-request.sh` or
`scripts/tail-errors.sh` helpers. The same triage can be done directly with the
logging tools already used by the deployment platforms.

**Local / Docker Compose:**
```bash
docker compose logs -f backend worker beat
docker compose logs backend worker beat | grep "<request_id_or_trace_id>"
```

**Azure Container Apps:**
```bash
az containerapp logs show --name backend --resource-group <resource-group> --tail 500
```
Then filter the returned structured logs for the `request_id` or trace ID.

**Azure VM:**
```bash
docker compose -f docker-compose.vm.yml logs backend worker beat
docker compose -f docker-compose.vm.yml logs backend worker beat | grep "<request_id_or_trace_id>"
```

The backend's request-context and error-handling middleware put request context
on structured log records, so this remains useful even when OpenTelemetry is
off. If tracing is enabled, use the emitted trace identifiers to continue the
investigation in Jaeger or the configured OTLP/Application Insights backend.


## 7. Audit log partitioning & annual archive

**What changed:** `audit_logs` is an append-only ledger — nothing is
ever deleted from it by the running app — which made it the one table
guaranteed to grow forever. It's now a native Postgres table
**PARTITIONED BY RANGE on `timestamp`, one partition per calendar year**
(`alembic/versions/0010_partition_audit_logs.py`; the full "why" —
query pruning on every existing date-filtered query, plus an
instant, VACUUM-free way to retire an old year instead of a slow bulk
`DELETE` — is in that migration's module docstring, and in
`models.py`'s `AuditLog` docstring). A `audit_logs_default` catch-all
partition exists so a write can never hard-fail even if the automation
below has a gap.

**What's automated (and what deliberately isn't):** a Celery Beat job
(`tasks.ensure_audit_log_partitions`, `celery_app.py`'s `beat_schedule`,
default every 24h — see `AUDIT_PARTITION_CHECK_INTERVAL_HOURS`) keeps the
next `AUDIT_PARTITION_YEARS_AHEAD` (default 2) years' partitions
pre-created, so nothing ever falls through to the default partition in
normal operation. **Retiring an old year is never automated.** That's a
deliberate, once-a-year (or "whenever disk space actually requires it")
human decision, made with a real backup in hand — the runbook below.

### The annual retirement runbook

This repo actually ships two deployment models — a plain
docker-compose/VM setup (`docker-compose.yml`, what `DEPLOYMENT.md`'s main
body assumes) and the cost-optimized Azure Container Apps architecture
(`infra/main.bicep`, `DEPLOYMENT.md`'s
**Azure Container Apps Production Deployment** section). The retirement
*decision* and the SQL are identical either way — what differs is only how
you get a shell. Every step below gives both; run the one that matches
where `db` is actually running, and skip the other.

**Step 0 — see what exists.** From inside the `backend` container:
```bash
# docker compose (VM/server)
docker compose exec backend python scripts/audit_partition_status.py

# Azure Container Apps
az containerapp exec --name backend --resource-group rg-snipeit-lite-prod \
  --command "python scripts/audit_partition_status.py"
```
This prints every partition's row count, on-disk size, and actual
oldest/newest entry — read-only, changes nothing. Use it to decide which
year (if any) is actually worth retiring; a partition sitting at a few
hundred KB isn't costing you anything, so don't drop one just because it's
old — drop one because disk space actually requires it.

**Rehearsing this runbook without real data to retire yet:** see
[§6.4](#64-rehearsing-the-partition-drop-runbook-safely) for the full
context. In short, `scripts/dev_seed_fake_old_partition.py` creates one
obviously-fake old year (2020 by default) with a handful of disposable,
clearly-tagged rows, so you can practice Step 2's `DROP TABLE` against
something that isn't production data. It refuses to run at all if
`ENVIRONMENT=production` — same non-interactive command pattern as Step 0
above:
```bash
# docker compose (VM/server)
docker compose exec backend python scripts/dev_seed_fake_old_partition.py

# Azure Container Apps
az containerapp exec --name backend --resource-group rg-snipeit-lite-staging \
  --command "python scripts/dev_seed_fake_old_partition.py"
```
Then run Step 0 again to see it, and Step 2 below to drop it.

**Azure caveat:** `backend` is a scale-to-zero Container App by default
(`backendMinReplicas`, `infra/main.bicep`) — `az containerapp exec` needs a
*running* replica to attach to, and won't itself trigger a cold start.
If `az containerapp replica list --name backend --resource-group
rg-snipeit-lite-prod` comes back empty, hit any `/api/*` endpoint first
(e.g. `curl https://<frontend-fqdn>/api/health`) to wake one up, wait for
it to show `Running`, then retry the `exec`. Postgres itself
(`postgresServer`, Azure Database for PostgreSQL Flexible Server) has no
cold start either way — it's a managed service, always running, no
Container App replica to wake up (see `main.bicep`'s `postgresServer`
comment).

**Step 1 — confirm the backup for that year is genuinely restorable,
*before* touching production.** This step is identical for both
deployment models — it always happens on your own machine, against a
throwaway local Postgres, never against the live production database
(docker-compose's `db` container, or the Azure Flexible Server) directly.
Don't trust "the file exists in Drive" — prove it restores:
```bash
# On your own machine, NOT against production, regardless of how
# production itself is hosted:
# 1. Pull the relevant backup file down from Google Drive (BACKUP_GDRIVE_FOLDER_ID,
#    per services/backup_service.py) — whichever backup covers the year
#    you're about to retire. Same backup job, same Drive folder, for
#    either deployment model.
# 2. Stand up a throwaway docker compose Postgres locally (a plain
#    `docker compose up -d db` against an empty volume is enough — you
#    do not need the whole app running for this, and you do not need
#    Azure credentials for this step at all).
docker compose up -d db
# 3. Restore the downloaded dump into it and confirm it comes back clean
#    (no errors, and the audit_logs partition for that year has the row
#    count you expect from Step 0):
cat that_backup_file.sql | docker compose exec -T db psql -U "${POSTGRES_USER:-admin}" -d "${POSTGRES_DB:-asset_db}"
```
If this restore fails, is truncated, or the row counts don't line up —
**stop here.** Don't drop anything in production; that backup can't
currently do its one job. Go figure out why (a bad upload, a corrupted
file, a backup job that silently failed) before proceeding, and re-run
this step once you have a backup you trust.

**Step 2 — only once Step 1 has actually passed, drop the partition in
production:**
```bash
# docker compose (VM/server) -- get a psql session against the live db
# (defaults shown -- match POSTGRES_USER/POSTGRES_DB to your real .env):
docker compose exec db psql -U "${POSTGRES_USER:-admin}" -d "${POSTGRES_DB:-asset_db}"

# Azure Container Apps -- postgresServer (Azure Database for PostgreSQL
# Flexible Server) is a standalone managed resource, not a Container App,
# so there's no `az containerapp exec --name db` to fall back on and no
# internal-only DNS name for it either -- it's reached over its own public
# FQDN with a firewall gating who can connect (see main.bicep's
# `postgresServer` comment). Simplest path: exec into `backend` (which
# already has psql installed -- see backend/Dockerfile -- and already has
# `DATABASE_URL` fully assembled, including host/user/db/sslmode, as its
# own env var) and connect from there, reusing that connection string
# as-is rather than reconstructing it by hand:
az containerapp exec --name backend --resource-group rg-snipeit-lite-prod --command /bin/sh
# then, inside that session:
psql "$DATABASE_URL"
# Alternative: connect directly from your own machine with `psql` or any
# GUI client, IF you've added your IP via main.bicep's
# `postgresAdminClientIp` parameter (redeploy after setting it) -- the
# "Allow Azure services" firewall rule alone does not permit arbitrary
# internet clients, only Azure's own backbone.
```
```sql
DROP TABLE audit_logs_y2021;
```
This is instant and reclaims the disk immediately — it's a separate
physical file, not a bulk `DELETE`, so there's no `VACUUM` to wait on and
no lock contention with the live table. Re-run Step 0's
`scripts/audit_partition_status.py` (again via `docker compose exec` or
`az containerapp exec`, whichever applies) afterward to confirm it's gone
and the disk space came back.

**Step 3 — if an old, already-retired year is ever needed again** (an
audit request, a legal hold, "what happened in 2021"): you already have
exactly what you need from Step 1 — pull that year's backup from Drive,
restore it into the same kind of scratch local docker compose setup
(never production, and this part is the same regardless of which
deployment model production actually runs), and export whatever's
actually being asked for (the existing CSV/PDF audit-ledger export in the
app, run against that scratch restore) before tearing the scratch setup
back down. The retired partition being gone from production doesn't mean
the data is gone — it means production doesn't have to carry the weight
of it day-to-day anymore.

---

## 8. What to deliberately *not* do

Worth saying explicitly, since over-building ops tooling is its own
failure mode for an app this size:
- Don't stand up a full Prometheus/Grafana stack — Log Analytics +
  scheduled query alerts covers this app's actual traffic volume for a
  fraction of the cost and maintenance.
- Don't re-enable Application Insights "just in case" — the bicep
  comments are explicit that this was a deliberate cost trade, and
  scheduled query alerts get you the paging without the ingestion bill.
- Don't scale `db`/`redis` containers horizontally — `main.bicep`
  already calls this out; if you need real HA there, that's a managed
  Postgres/Redis migration, not a scaling knob.

---

**Next concrete step, if you want it:** I can add the `dependabot.yml`
file above directly, or draft the `main.bicep` additions for the three
alert rules in §2. Either is a small, self-contained change against what's
already here.
