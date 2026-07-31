# Zero-Downtime Deployments: ACA vs VM

This app ships to two independent targets — **Azure Container Apps (ACA)**
and a **single Azure VM** — and both get a real zero-downtime blue-green
rollout, but the mechanics are necessarily different because ACA gives you
native multi-revision traffic splitting and the VM doesn't. This doc is the
short version: how to push, how to watch it happen, how slots switch, and
exactly how production traffic moves in each case.

For the full rationale behind each design decision, see `DEPLOYMENT.md`'s
"Zero-downtime rollout mechanics" section (ACA) and `DEPLOYMENT_VM.md`'s
"Zero-Downtime Blue-Green Deployments" section (VM) — this file only
summarizes.

---

## The two paths at a glance

| | **ACA** | **VM** |
|---|---|---|
| Workflow | `.github/workflows/deploy-azure-aca.yml` | `.github/workflows/deploy-azure-vm.yml` |
| What splits traffic | Container Apps' native revision traffic-weight API | Caddy `reverse_proxy` weighted load balancing |
| Slot mechanism | Two **revisions** of the same Container App (`activeRevisionsMode: Multiple`) | Two **containers** per service — `backend-blue`/`backend-green`, `frontend-blue`/`frontend-green` |
| Rollout driver | `.github/scripts/aca-blue-green.sh` (runs on the GitHub-hosted runner, talks to Azure via `az`) | `scripts/blue-green-deploy.sh` (runs **on the VM itself**, invoked over SSH) |
| Traffic steps | 10% → 25% → 50% → 75% → 100% (production) / straight to 100% (staging, scale-to-zero) | 10% → 25% → 50% → 75% → 100% |
| Rollback | Instant traffic-weight flip back to the still-running old revision | Instant Caddy reweight back to the still-running old slot |
| Live status | `bash .github/scripts/aca-blue-green.sh status <app> <rg> --watch` | `https://<domain>/_deploy/` dashboard |

---

## Path 1: Azure Container Apps

### How traffic passes

`backend` and `frontend` both run in **Multiple** active-revisions mode
(`infra/main.bicep`), not Single. That's what makes this possible at all:
the old revision ("blue") and a new one ("green") run side by side as two
fully independent, individually addressable revisions, and the split
between them is a traffic **weight** the pipeline controls explicitly —
decoupled from "which one is newest." `frontend`'s external ingress means
every revision also gets its own per-revision FQDN, which is what lets the
rollout smoke-test the new revision directly before it takes any real
traffic.

### How to push

Two ways to trigger `deploy-azure-aca.yml`:

- **Manual** — open the workflow in the Actions tab, pick `staging` or
  `production`, hit **Run workflow**. Leave `image_tag` blank to build
  fresh from the branch/ref you're running against (runs `ci.yml` +
  `build-push-images.yml` first), or supply an existing Docker Hub tag to
  redeploy/roll forward to something already built.
- **Tag release** — `git tag vX.Y.Z && git push origin vX.Y.Z` runs
  `release.yml` (build + Trivy scan + push both images tagged `vX.Y.Z`),
  which then calls this workflow for **production** automatically.

### What happens per app, in order

Driven by `.github/scripts/aca-blue-green.sh rollout` for `backend`, then
again for `frontend` (backend always finishes first, so frontend's proxy
target is already correct by the time frontend itself rolls out):

1. **Create the new revision at 0% traffic.** Nothing routes to it yet.
2. **Health-check the replica that actually received the push** —
   Container Apps' own readiness probe (`/readyz` on backend, `/` on
   frontend) polls the new revision directly.
3. **Direct smoke test** — `frontend` only (it's the one app with a public
   per-revision FQDN): real HTTP against `/` and `/api/auth/me`, still at
   zero production traffic.
4. **Ramp traffic.** Production walks the new revision 10% → 25% → 50% →
   75% → 100%, re-checking health at each step — the same five-step ramp
   the VM path uses (see below). Staging (min replicas 0, no standing
   traffic to protect) jumps straight to 100% once step 2 passes.
5. **Spin down the old revision** — only after **both** backend and
   frontend have reached 100% traffic **and** the workflow's own
   end-to-end smoke test (hitting the live app, not a single revision)
   has passed. Until then the old revision stays fully intact at 0%
   traffic — a rollback away, not a redeploy away.

### Switching slots / rollback

- **Mid-rollout failure** (a revision fails its own health check or direct
  smoke test): `aca-blue-green.sh` rolls that one back itself, inline —
  traffic never left the old revision in the first place.
- **Post-cutover failure** (both apps reached 100% but the final
  end-to-end smoke test then fails): the workflow calls
  `aca-blue-green.sh rollback`, which flips traffic back to the old
  revision at 100% and deactivates the bad new one. This is a
  **traffic-weight change, not a redeploy** — no image pull, no cold
  start, the fastest possible recovery.
- **Manual rollback well after a deploy finished** (old revision already
  spun down, so there's nothing left to flip back to): re-run
  `deploy-azure-aca.yml` via `workflow_dispatch` with the previous
  `image_tag`. This is a genuine forward deploy of an old image, gated by
  the same rollout above.

### Monitoring

```bash
bash .github/scripts/aca-blue-green.sh status backend rg-snipeit-lite-prod --watch
```

Shows every revision's health, replica count, and live traffic weight,
refreshed every 5s — works from a laptop with just `az login`, no GitHub
Actions access needed. The workflow also writes old/new revision names and
final traffic state into the run's own `GITHUB_STEP_SUMMARY`.

---

## Path 2: the VM

### How traffic passes

There's no native revision system on a single VM, so the split is built
out of two full sets of containers and a reverse proxy in front of them:
`backend-blue`/`backend-green` and `frontend-blue`/`frontend-green`
(`docker-compose.vm.yml`), with **Caddy** as the single entry point
(everything comes in through the Cloudflare Tunnel to Caddy — no other
port is ever published). Caddy's `reverse_proxy` splits traffic across
`frontend-blue:80` and `frontend-green:80` by **weight**, imported from
one file, `caddy/weights.conf` (a single line: `lb_policy
weighted_round_robin <blue-weight> <green-weight>`). Each frontend
proxies its own `/api/*` to its own slot's backend, so a given request is
always served entirely by one slot — never a mix of old and new code.
`scripts/blue-green-deploy.sh` rewrites `weights.conf` and runs `caddy
reload` (graceful — in-flight requests finish, no dropped connections) at
each ramp step. Caddy also does passive health checking on top of this:
if a slot starts failing requests outright it's automatically pulled from
rotation for a few seconds, independent of the script's own checks.

Which slot is "active" persists in `.env` on the VM as `ACTIVE_SLOT` +
`COMPOSE_PROFILES` (blue or green), so a reboot or a plain `docker compose
up -d` always comes back up on the correct slot without needing a rollout
to run first.

### How to push

`deploy-azure-vm.yml` triggers the same two ways as the ACA workflow:

- **Manual** — `workflow_dispatch`, choose `vm-staging` or `prod` (kept
  deliberately distinct from ACA's `staging`/`production` environment
  names so the two paths never share secrets). Leave `image_tag` blank to
  build fresh; `skip_migrate` skips only the `alembic upgrade head` step
  (safe only if no migration changed since that image was built) — the
  replica/health-check/ramp mechanics always run in full either way.
- **Tag release** — same `vX.Y.Z` tag push as ACA, always targets `prod`.

The workflow SSHes in, syncs `docker-compose.vm.yml`, `Caddyfile`, and
`scripts/*` to the VM (deliberately **not** `caddy/weights.conf` — that
file is live state on the VM, owned by the deploy script, not the repo),
pulls the new image, then hands off to `scripts/blue-green-deploy.sh`
running on the VM itself over SSH.

### What `blue-green-deploy.sh` does, in order

1. **Read `ACTIVE_SLOT`** from `.env`, compute the other slot as the
   target (`NEW_SLOT`).
2. **Migrate** — `alembic upgrade head` against the incoming image, on
   the one shared Postgres database. Must stay backward-compatible with
   the still-running old slot for the rest of the rollout.
3. **Start the replica** — only `backend-$NEW_SLOT` / `frontend-$NEW_SLOT`
   (plus refreshing `worker`/`beat` in place, since they aren't behind
   Caddy). Caddy is still sending 100% of traffic to the old slot, so the
   new slot gets zero production traffic here.
4. **Health-check the new slot directly** (`scripts/health-check.sh
   --mode internal`), never through Caddy. Any failure aborts the whole
   rollout — old slot untouched, new slot stopped.
5. **Ramp traffic** 10% → 25% → 50% → 75% → 100%, rewriting
   `caddy/weights.conf` + `caddy reload` at each step, re-running the
   health check against each partial-traffic step (catches a regression
   that only shows up under real load, not just an idle check).
6. **Cut over** — flip `ACTIVE_SLOT`/`COMPOSE_PROFILES` in `.env` to the
   new slot, then stop and remove the old slot's containers.

### Switching slots / rollback

Any failure at any phase (migration, replica health check, or a ramp-step
health check) trips a failure trap in the script: it immediately restores
100% of Caddy's weight to the still-running old slot and stops the new
one, before exiting non-zero. Same guarantee as the ACA path — a rollback
during rollout is a traffic-weight change, not a redeploy, and the old
slot is never touched until the new one has proven itself.

For a rollback well after a deploy finished (old slot's containers already
removed): run `deploy-azure-vm.yml` via `workflow_dispatch` with the
previous `image_tag` — a genuine forward deploy of the older image through
the same blue-green rollout above, into whichever slot is currently idle.

### Monitoring

```
https://<domain>/_deploy/
```

A small dashboard (HTTP Basic Auth-gated, not linked from the app) that
polls `scripts/deploy-status/status.json`, which
`blue-green-deploy.sh` rewrites at every phase transition (`starting` →
`migrating` → `starting_replica` → `health_checking` → `ramping` →
`cutover_complete` → `spinning_down_old` → `done`, or `failed`). Every
individual health check is also appended to `checks.log` alongside it.
On a failed GitHub Actions run, the workflow additionally dumps both files
plus `caddy/weights.conf`'s current contents into the run's own logs.

---

## Quick reference

| I want to... | ACA | VM |
|---|---|---|
| Deploy the latest code | `workflow_dispatch` on `deploy-azure-aca.yml`, blank `image_tag` | `workflow_dispatch` on `deploy-azure-vm.yml`, blank `image_tag` |
| Redeploy/roll back to a specific build | Same workflow, set `image_tag` to that tag/SHA | Same workflow, set `image_tag` to that tag/SHA |
| Watch a rollout live | `aca-blue-green.sh status <app> <rg> --watch` | `https://<domain>/_deploy/` |
| See which slot is live | Same `status` command (`active`/`traffic` columns) | `ACTIVE_SLOT` in the VM's `.env`, or the dashboard |
| Force an instant rollback mid-flight | Automatic on health-check/smoke-test failure | Automatic on health-check failure (script's own trap) |
