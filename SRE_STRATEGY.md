# Snipe-IT Lite — Continuous SRE Strategy

This is the operating rhythm for keeping the Azure Container Apps
deployment healthy day-to-day, not a one-time setup task. It assumes
you've already read `DEPLOYMENT.md`'s **Health Checks & Monitoring** and
**Azure Container Apps Production Deployment** sections — this document
picks up where those leave off: what to actually *do*, on a schedule,
using what's already built.

**Starting point worth naming:** this app already has more reliability
plumbing than most projects this size — `/healthz` + `/readyz` split,
Redis-backed rate limiting that's replica-safe, RedBeat so `beat` never
double-fires across replicas, a migrate → deploy → smoke-test →
auto-rollback pipeline, structured JSON logs with a correlation ID, and
both local + off-box (Google Drive) backups. The strategy below is mostly
about **using that machinery on a schedule**. Both gaps this document
originally flagged are now closed at the code level: alerting on top of
the Log Analytics data you're already paying for is implemented in
`infra/main.bicep`, opt-in behind one secret (see §2), and automated
dependency updates are already running via `.github/dependabot.yml` (see
§4). What's left for both is cadence and turning the one secret on, not
missing tooling.

---

## 1. Service Level Objectives (keep these small and honest)

For a single-tenant internal tool, don't over-engineer error budgets.
Three numbers are enough to know if you're drifting:

| SLI | Target | How to measure |
|---|---|---|
| Availability | 99.5% monthly (~3.6 hrs/month) | `/healthz` probe success rate (already polled by Container Apps) |
| Readiness | 99% monthly | `/readyz` — catches "schema doesn't match code" and DB blips separately from liveness |
| Checkout-path latency | p95 < 800ms | Log Analytics on `backend`'s request logs (see queries below) |
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

**Done:** [`.github/dependabot.yml`](.github/dependabot.yml) now exists in
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
- **Need to roll back a bad release** → `workflow_dispatch` on
  `deploy-azure-production.yml` with the previous `image_tag`, per
  DEPLOYMENT.md's Rollback section. Don't hand-run `az containerapp
  update` — you'd bypass the migrate/smoke-test safety net.

For each real incident going forward, write a short postmortem: what
broke, what the user-facing impact was, what the fix was, and one
concrete follow-up action. Even three sentences beats nothing — the
value is almost entirely in the "did we do the follow-up action" part.

---

## 6. What to deliberately *not* do

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
