# Zero-Downtime Deployments: ACA vs VM

This app ships to two independent targets — **Azure Container Apps (ACA)**
and a **single Azure VM** — and both get a real zero-downtime blue-green
rollout, but the mechanics are necessarily different because ACA gives you
native multi-revision traffic splitting and the VM doesn't. Both paths now
use the same FIXED naming: **blue is always the incoming deploy being
validated, green is always the active/production slot** once every gate has
passed — never the other way around, and the roles never swap from one
deploy to the next. This doc is the short version: how to push, how to
watch it happen, how slots switch, and exactly how production traffic
moves in each case.

For the full rationale behind each design decision, see `DEPLOYMENT.md`'s
"Zero-downtime rollout mechanics" section (ACA) and `DEPLOYMENT_VM.md`'s
"Zero-Downtime Blue-Green Deployments" section (VM) — this file only
summarizes.

## Table of Contents

- [The two paths at a glance](#the-two-paths-at-a-glance)
- [Path 1: Azure Container Apps](#path-1-azure-container-apps)
  - [How traffic passes](#how-traffic-passes)
  - [How to push](#how-to-push)
  - [What happens per app, in order](#what-happens-per-app-in-order)
  - [Switching slots / rollback](#switching-slots--rollback)
  - [Monitoring](#monitoring)
- [Path 2: the VM](#path-2-the-vm)
  - [How traffic passes](#how-traffic-passes-1)
  - [How to push](#how-to-push-1)
  - [What `blue-green-deploy.sh` does, in order](#what-blue-green-deploysh-does-in-order)
  - [Switching slots / rollback](#switching-slots--rollback-1)
  - [Monitoring](#monitoring-1)
- [Quick reference](#quick-reference)

---

---

## The two paths at a glance

| | **ACA** | **VM** |
|---|---|---|
| Workflow | `.github/workflows/deploy-azure-aca.yml` | `.github/workflows/deploy-azure-vm.yml` |
| What splits traffic | Container Apps' native revision traffic-weight API | Caddy `reverse_proxy` weighted load balancing |
| Slot mechanism | Two **revisions** of the same Container App (`activeRevisionsMode: Multiple`) — blue's revision suffix is discarded once finalized, so there's no permanent "blue container"; it's simply green (active) from then on | Two **fixed, permanently-named containers** per service — `backend-blue`/`backend-green`, `frontend-blue`/`frontend-green` — blue is always redeployed into and always spun back down to idle; green is always what's promoted onto the new image and left running |
| Rollout driver | `.github/scripts/aca-blue-green.sh` (runs on the GitHub-hosted runner, talks to Azure via `az`) | `scripts/blue-green-deploy.sh` (runs **on the VM itself**, invoked over SSH) |
| Traffic steps | 10% → 25% → 50% → 75% → 100% (production) / straight to 100% (staging, scale-to-zero) | 10% → 25% → 50% → 75% → 100%, then a same-image handoff back to green (see below) |
| Rollback | Instant traffic-weight flip back to the still-active green revision | Instant Caddy reweight back to the still-active green slot |
| Live status (deployment's own view) | `bash .github/scripts/aca-blue-green.sh status <app> <rg> --watch`, or `https://<domain>/_deploy/` (dashboard shell baked into `frontend`'s image, data proxied live from Blob Storage) | `https://<domain>/_deploy/` (dashboard, served by Caddy from local disk) |
| Zero-downtime evidence (client's-eye view) | `scripts/poll-live-endpoint.sh` against the public frontend FQDN, backgrounded on the runner for the whole rollout | `scripts/poll-live-endpoint.sh` against `https://<domain>/`, backgrounded on the runner for the whole rollout |

---

## Path 1: Azure Container Apps

### How traffic passes

`backend` and `frontend` both run in **Multiple** active-revisions mode
(`infra/main.bicep`), not Single. That's what makes this possible at all:
the active revision ("green") and the incoming one being validated
("blue") run side by side as two fully independent, individually
addressable revisions, and the split between them is a traffic **weight**
the pipeline controls explicitly — decoupled from "which one is newest."
`frontend`'s external ingress means every revision also gets its own
per-revision FQDN, which is what lets the rollout smoke-test the incoming
revision directly before it takes any real traffic.

Unlike the VM path, ACA revisions don't have a fixed, reusable name to
promote onto — a revision's own suffix is whatever `aca-blue-green.sh`
generated for that one deploy. "Blue" and "green" here are ROLES, not
container names: once a rollout finalizes, the revision that was blue
simply IS green (the active role) from that point on — there's no second
handoff step the way there is on the VM (see below), just a spun-down old
revision and a live new one.

### How to push

`deploy-azure-aca.yml` only ever runs manually — open the workflow in the
Actions tab, pick `staging` or `production`, hit **Run workflow**. Leave
`image_tag` blank to build fresh from the branch/ref you're running
against (runs `ci.yml` + `build-push-images.yml` first), or supply an
existing Docker Hub tag (e.g. a version like `vX.Y.Z` published by
`git tag vX.Y.Z && git push origin vX.Y.Z` -- see `release.yml`) to
redeploy/roll forward to something already built. A version-tag push by
itself only builds, Trivy-scans, and publishes the images -- it never
calls this workflow.

### What happens per app, in order

Driven by `.github/scripts/aca-blue-green.sh rollout` for `backend`, then
again for `frontend` (backend always finishes first, so frontend's proxy
target is already correct by the time frontend itself rolls out):

1. **Create the incoming ("blue") revision at 0% traffic.** Nothing routes
   to it yet.
2. **Health-check the replica that actually received the push** —
   Container Apps' own readiness probe (`/readyz` on backend, `/` on
   frontend) polls the incoming revision directly.
3. **Direct smoke test** — `frontend` only (it's the one app with a public
   per-revision FQDN): real HTTP against `/` and `/api/auth/me`, still at
   zero production traffic.
4. **Ramp traffic.** Production walks the incoming revision 10% → 25% →
   50% → 75% → 100%, re-checking health at each step — the same five-step
   ramp the VM path uses (see below). Staging (min replicas 0, no standing
   traffic to protect) jumps straight to 100% once step 2 passes.
5. **Spin down the active ("green") revision** — only after **both**
   backend and frontend have reached 100% traffic **and** the workflow's
   own end-to-end smoke test (hitting the live app, not a single revision)
   has passed. Until then the active revision stays fully intact at 0%
   traffic — a rollback away, not a redeploy away. Once spun down, the
   revision that was blue for this rollout is simply green (the active
   role) going forward.

### Switching slots / rollback

- **Mid-rollout failure** (a revision fails its own health check or direct
  smoke test): `aca-blue-green.sh` rolls that one back itself, inline —
  traffic never left the active revision in the first place.
- **Post-cutover failure** (both apps reached 100% but the final
  end-to-end smoke test then fails): the workflow calls
  `aca-blue-green.sh rollback`, which flips traffic back to the active
  revision at 100% and deactivates the bad incoming one. This is a
  **traffic-weight change, not a redeploy** — no image pull, no cold
  start, the fastest possible recovery.
- **Manual rollback well after a deploy finished** (the old active
  revision already spun down, so there's nothing left to flip back to):
  re-run `deploy-azure-aca.yml` via `workflow_dispatch` with the previous
  `image_tag`. This is a genuine forward deploy of an old image, gated by
  the same rollout above.

### Monitoring

Two live views, same as the VM path now offers:

```bash
bash .github/scripts/aca-blue-green.sh status backend rg-snipeit-lite-prod --watch
```

Shows every revision's health, replica count, and live traffic weight,
refreshed every 5s — works from a laptop with just `az login`, no GitHub
Actions access needed.

```
https://<domain>/_deploy/
```

A small dashboard (HTTP Basic Auth-gated, not linked from the app). Its
shell (`scripts/deploy-status-aca/index.html`) ships baked into the
`frontend` image; `status.json`/`checks.log` are proxied **live**,
per-request, straight through to the `deploy-status` Blob container
(`infra/main.bicep`'s `deployStatusContainer`) — no mount, no cache
anywhere in the path (see `nginx/default.conf.template`'s own `/_deploy/`
comment for why this isn't an Azure Files mount: ACA rejects the mount
option that approach needed, and doesn't support mounting Blob Storage as
a volume at all). It polls `/_deploy/status.json` (both apps'
active/incoming revision names and live traffic split) and
`/_deploy/checks.log` (every individual gate `aca-blue-green.sh` ran, pass
or fail), both rewritten by `.github/scripts/aca-deploy-status.sh` at
every phase transition and gate — the same `write`/`check` shape
`scripts/blue-green-deploy.sh` uses locally on the VM, just uploaded to
Blob Storage (`az storage blob upload`, using the run's own OIDC login —
no extra secret for that part) instead of written to local disk.

Credentials come from two GitHub Environment secrets you need to set once
per environment (`staging`/`production`), consumed by the `deploy` job's
"Write ACA deploy status - init" step:

- `DEPLOY_STATUS_USER` — the Basic Auth username.
- `DEPLOY_STATUS_PASSWORD_APR1_HASH` — an **`$apr1$`-format** hash, NOT
  bcrypt (nginx's `auth_basic_user_file` only understands
  `{PLAIN}`/`{SSHA}`/`$apr1$`, unlike Caddy on the VM path, which supports
  bcrypt natively — see `nginx/default.conf.template`'s own comment).
  Generate one with:
  ```bash
  openssl passwd -apr1 'your-password-here'
  ```

If either secret is unset, the dashboard's `.htpasswd` is simply never
uploaded, so `nginx/docker-entrypoint.d/25-fetch-deploy-status-htpasswd.sh`
fetches nothing at container boot and `/_deploy/` 401s on every request
rather than serving unauthenticated (see `aca-deploy-status.sh`'s own
warning for this case) — the deploy itself is completely unaffected either
way; this page is purely observational.

This is the **deployment's own view** — proof each revision's readiness
probe passed, and proof of the one end-to-end smoke test once both apps
hit 100%. It is deliberately not proof that a real request stream against
the live domain was uninterrupted **while** traffic was actually moving.
That's a separate, independent check:

```bash
scripts/poll-live-endpoint.sh --url https://<frontend-fqdn>/
```

`deploy-azure-aca.yml`'s `deploy` job now runs this automatically —
backgrounded on the runner, started before either app's rollout begins and
stopped only after both slots are spun down — so every deploy produces a
timestamped CSV of every request the live domain answered (or didn't)
for the full rollout window, uploaded as the `zero-downtime-poll-evidence-*`
workflow artifact. A `FAIL`/`ERROR` row anywhere in that CSV fails the job
even if every internal health gate passed — see `scripts/poll-live-endpoint.sh`'s
own header comment for why that's a meaningfully different check than the
platform's own readiness probes.

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

Unlike ACA's revisions, these container names are FIXED and permanent —
`backend-blue`/`frontend-blue` are always the incoming candidate, and
`backend-green`/`frontend-green` are always the active/production slot.
`ACTIVE_SLOT`/`COMPOSE_PROFILES` in the VM's `.env` are correspondingly
just constants now, both always `green`, set once by
`infra-vm/cloud-init.yaml` on first boot and never touched again by any
later deploy — a reboot or a plain `docker compose up -d` always comes
back up on green with no rollout needing to run first.

### How to push

`deploy-azure-vm.yml` triggers the same two ways as the ACA workflow:

- **Manual** — `workflow_dispatch`, choose `vm-staging` or `prod` (kept
  deliberately distinct from ACA's `staging`/`production` environment
  names so the two paths never share secrets). Leave `image_tag` blank to
  build fresh; `skip_migrate` skips only the `alembic upgrade head` step
  (safe only if no migration changed since that image was built) — the
  replica/health-check/ramp/promotion mechanics always run in full either
  way.
- **Tag release** — same `vX.Y.Z` tag push as ACA, always targets `prod`.

The workflow SSHes in, syncs `docker-compose.vm.yml`, `Caddyfile`, and
`scripts/*` to the VM (deliberately **not** `caddy/weights.conf` — that
file is live state on the VM, owned by the deploy script, not the repo),
pulls the new image, then hands off to `scripts/blue-green-deploy.sh`
running on the VM itself over SSH.

### What `blue-green-deploy.sh` does, in order

1. **Migrate** — `alembic upgrade head` against the incoming image, run
   through blue, on the one shared Postgres database. Must stay
   backward-compatible with the still-running green slot for the rest of
   the rollout.
2. **Start the incoming slot** — only `backend-blue`/`frontend-blue`
   (plus refreshing `worker`/`beat` in place, since they aren't behind
   Caddy). Caddy is still sending 100% of traffic to green, so blue gets
   zero production traffic here.
3. **Health-check blue directly** (`scripts/health-check.sh --mode
   internal`), never through Caddy. Any failure aborts the whole rollout —
   green untouched, blue stopped.
4. **Ramp traffic** 10% → 25% → 50% → 75% → 100% onto blue, rewriting
   `caddy/weights.conf` + `caddy reload` at each step, re-running the
   health check against each partial-traffic step (catches a regression
   that only shows up under real load, not just an idle check).
5. **Promote** — now that blue has proven itself under 100% real traffic,
   bring green up on the exact same image (both slots share one
   `${IMAGE_TAG}` reference — see `docker-compose.vm.yml`'s own comment),
   health-check green directly, then flip Caddy's weight straight back to
   100% green / 0% blue — a same-code swap, not a second canary, since
   both slots are now running the identical, already-proven image.
6. **Spin blue back down** — stop and remove blue's containers so it's
   idle again, ready for the next incoming image. `.env` needs no update
   at all now — `ACTIVE_SLOT`/`COMPOSE_PROFILES` were already `green`
   before this rollout started and still are.

### Switching slots / rollback

Any failure during migration, blue's own health check, or a ramp-step
health check (steps 1–4 above) trips the failure trap in the script: it
immediately restores 100% of Caddy's weight to green and stops blue,
before exiting non-zero — green is never touched until blue has already
proven itself, so a bad deploy never causes an outage.

A failure **during promotion itself** (step 5 — green's own health check
after coming up on the new image) is handled differently, on purpose:
blue already proved itself at 100% traffic by this point, so it's left
serving rather than reverted to a potentially-stale green. The trap
reports this loudly (`status.json`'s phase becomes `promotion_failed`, a
distinct state from the normal `failed`) rather than silently retrying —
re-running the deploy retries promotion from scratch. See
`scripts/blue-green-deploy.sh`'s own top-of-file comment for the full
reasoning.

For a rollback well after a deploy finished (blue's containers already
removed, green fully caught up): run `deploy-azure-vm.yml` via
`workflow_dispatch` with the previous `image_tag` — a genuine forward
deploy of the older image through the same blue-green rollout above, into
blue as always.

### Monitoring

```
https://<domain>/_deploy/
```

A small dashboard (HTTP Basic Auth-gated, not linked from the app) that
polls `scripts/deploy-status/status.json`, which
`blue-green-deploy.sh` rewrites at every phase transition (`starting` →
`migrating` → `starting_replica` → `health_checking` → `ramping` →
`promoting` → `spinning_down_incoming` → `done`, or `failed`/
`promotion_failed`). Every individual health check is also appended to
`checks.log` alongside it. On a failed GitHub Actions run, the workflow
additionally dumps both files plus `caddy/weights.conf`'s current contents
into the run's own logs.

Credentials come from the `DEPLOY_STATUS_USER`/`DEPLOY_STATUS_PASSWORD_HASH`
GitHub Environment variable/secret (a **bcrypt** hash here — Caddy
supports it natively, unlike ACA's nginx-served dashboard, which needs an
`$apr1$` hash instead; see the ACA section above) — set once and both a
fresh VM's first boot (`infra-deploy-vm.yml`) and any later rotation
(`sync-secrets-vm.yml`) pick them up automatically, no SSH required. See
`DEPLOYMENT_VM.md`'s "Monitoring a rollout" section.

Like the ACA path, this dashboard is the **deployment's own view** — the
rollout script's phase and each health check it ran against the slot
directly. It's not proof a real client-facing request was uninterrupted
while `blue-green-deploy.sh` was actually ramping Caddy's weights. That's
what `scripts/poll-live-endpoint.sh` is for:

```bash
scripts/poll-live-endpoint.sh --url https://<domain>/
```

`deploy-azure-vm.yml` now runs this automatically too — backgrounded on
the runner (not the VM, not over SSH: it has to observe the exact same
path a real browser tab does, through Cloudflare and Caddy, not a
shortcut) from just before the rollout starts through the smoke test
after cutover. Every deploy produces a timestamped CSV of the live
domain's request-by-request behavior for the full rollout window,
uploaded as the `zero-downtime-poll-evidence-vm-*` workflow artifact, and
a `FAIL`/`ERROR` row anywhere in it fails the job — independent of
whether `blue-green-deploy.sh`'s own internal health checks all passed.

---

## Quick reference

| I want to... | ACA | VM |
|---|---|---|
| Deploy the latest code | `workflow_dispatch` on `deploy-azure-aca.yml`, blank `image_tag` | `workflow_dispatch` on `deploy-azure-vm.yml`, blank `image_tag` |
| Redeploy/roll back to a specific build | Same workflow, set `image_tag` to that tag/SHA | Same workflow, set `image_tag` to that tag/SHA |
| Watch a rollout live | `aca-blue-green.sh status <app> <rg> --watch`, or `https://<domain>/_deploy/` | `https://<domain>/_deploy/` |
| See which slot is live | Green, always (the fixed active role) — the `status` command's `active`/`traffic` columns or the dashboard show which revision that currently is | Green, always (the fixed active role, `backend-green`/`frontend-green`) — confirm with the dashboard or `docker compose -f docker-compose.vm.yml ps backend-green` |
| Force an instant rollback mid-flight | Automatic on health-check/smoke-test failure | Automatic on health-check failure (script's own trap) — a failure during promotion itself is handled differently, see above |
