# Ledger — Asset Management Frontend

A new React + TypeScript frontend for the asset-mgt backend (FastAPI), built with Vite,
Tailwind CSS v4, Framer Motion, and Recharts.

## Design direction

The visual identity is built around the domain itself: physical inventory tags. Asset
cards render as die-cut "hang tags" with a punch hole, a monospaced pool ID (e.g.
`OPT-0114`), and a perforated edge — instead of a generic dashboard template. Palette
is a deep ink navy with a brass/amber accent (evoking a brass tag) plus moss (available),
rust (overdue/out of stock), and sky (in-transit) status colors. Type pairing is Space
Grotesk (display) + IBM Plex Sans (body) + IBM Plex Mono (tags, IDs, timestamps).

## Pages

- Overview — stats, 14-day checkout/return activity chart, "needs attention" list, fleet-by-category breakdown
- Inventory — searchable/filterable grid of asset-tag cards, opens a slide-over detail drawer
- Checkouts — active/overdue table + pending extension-request approvals
- Notifications — unread indicator, categorized activity feed
- Login — animated entry screen

## Running it

    npm install
    npm run dev

`vite.config.ts` proxies `/api/*` to `http://localhost:8000` by default, so `npm run
dev` talks to a locally running `uvicorn main:app` (or `docker compose up backend`)
with zero config. Point it elsewhere with `VITE_DEV_API_PROXY_TARGET`, or bypass the
proxy entirely with `VITE_API_BASE_URL` in `.env.local` (see `.env.local.example`).

## Connecting to the real backend

This now talks to the real FastAPI backend (`backend/api/*.py`), not demo data, once
you're signed in:

- `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout` — real,
  cookie-session-based auth (`src/lib/auth.tsx`). Two-factor (`super_admin`) accounts
  aren't fully wired up past the initial challenge yet.
- `GET /api/assets` → Inventory grid + dashboard stats
- `GET /api/checkouts/overdue` + `GET /api/checkouts/due-soon` → Checkouts table (the
  backend has no single "list every active checkout" route — custody is tracked
  per-person via `GET /users/{id}/items` instead — so these two alert feeds, the same
  ones the legacy dashboard's own banners use, are the closest real equivalent)
- `GET /api/checkouts/extension-requests` → pending extension requests
- Notifications and the dashboard's activity trend chart are synthesized client-side
  (see the comments in `src/lib/api.ts`) since the backend doesn't expose an in-app
  notification feed or a historical checkout/return time series today — only a
  digest-email recipient list (`api/notifications_api.py`) and the alert feeds above.

Every read still falls back to demo data on failure (`src/lib/api.ts`'s `tryLoad`), so
a signed-in-but-unreachable-backend state degrades gracefully instead of breaking. The
sidebar's status dot shows which mode you're actually in.

Types for all of these live in `src/lib/types.ts`.

## Deployment

Built and served as its own standalone image, not bundled with the legacy site — see
`frontend/Dockerfile`'s `frontend-app-build` stage (builds this into `dist/`) and its
`frontend-react-only` final stage (ships `dist/` at the image's site root,
`/usr/share/nginx/html/`), paired with `nginx/default.react.conf.template` (SPA
fallback routing at `/`). This app and the legacy `admin.html`/`manager.html`/etc.
static site are mutually exclusive: a single running container is always exactly one
of them, both served at `/` — never both side-by-side under one origin the way they
used to be under a shared `/app/` sub-path. Which one a given deploy ships is a build
choice (`--target frontend-react-only` vs. `--target frontend-legacy-only`), covered
below.

## Choosing which frontend to ship

`frontend/Dockerfile` produces two separate, mutually exclusive images from the same
source tree, selected with `--target` (there's no default target anymore — every
build has to say which one it wants):

    # This app (Ledger):
    docker build -f frontend/Dockerfile --target frontend-react-only \
        -t snipeit-lite-frontend-react .

    # The legacy vanilla-JS site instead:
    docker build -f frontend/Dockerfile --target frontend-legacy-only \
        -t snipeit-lite-frontend-legacy .

or, via Compose, set in `.env`:

    FRONTEND_BUILD_TARGET=frontend-react-only

then `docker compose up -d --build frontend`. (Leaving `FRONTEND_BUILD_TARGET` unset
defaults to `frontend-legacy-only` — the smaller, longer-proven surface — so an
unconfigured environment doesn't silently start shipping this app.)

What choosing `frontend-react-only` skips (and, symmetrically, what
`frontend-legacy-only` skips):

- **Exactly one of the two build stages ever runs.** Docker BuildKit only builds the
  stages a chosen `--target` depends on: `frontend-react-only` never references
  `frontend-build` (the legacy site's minify/obfuscate stage), and `frontend-legacy-only`
  never references `frontend-app-build` (this app's Vite/Node toolchain) — so whichever
  one you didn't choose isn't "built and discarded," it's not built at all. No Node/Vite
  run, no `frontend-app/` `npm ci`, nothing from this directory touches the
  `frontend-legacy-only` image or its build cache, and vice versa for the legacy site.
- **Only one nginx config template ships, matched to the content.** `frontend-react-only`
  ships `nginx/default.react.conf.template` (real SPA fallback: an unmatched route like
  `/checkouts` resolves to `index.html` so this app's own router can take over) and
  `frontend-legacy-only` ships `nginx/default.conf.template` (legacy multi-page "clean
  URL" serving, with a genuine 404 for a truly missing page) — see
  `frontend/Dockerfile`'s own top-of-file comment for why these are two separate files
  rather than one shared template with conditionals.
- **The legacy site, backend, and CI for `frontend/` are all untouched** by choosing
  `frontend-react-only`, and this app's own source is entirely untouched by choosing
  `frontend-legacy-only`. The one backend endpoint this app uses that the legacy site
  doesn't (`get_activity`, feeding the Dashboard's checkout-activity chart) stays live
  either way — nothing legacy calls it, so it's just inert unused API surface when this
  app isn't deployed, not a dependency in the other direction.
- **Render deployments (`Dockerfile.render`) are already React-free** — that build
  path never had a `frontend-app-build`-equivalent stage to begin with, so this only
  matters for the Compose/Azure images built from `frontend/Dockerfile`.

If you're maintaining a long-lived deployment of one flavor (not just a one-off
build), consider pinning `FRONTEND_BUILD_TARGET=frontend-react-only` (or
`frontend-legacy-only`) in that environment's own `.env` / CI variables rather than
remembering the `--target` flag every time.

### CI/CD (Azure VM and Container Apps)

The VM and ACA deploy pipelines (`deploy-azure-vm.yml`, `deploy-azure-aca.yml`,
`release.yml`) don't build locally -- they build via the shared
`build-push-images.yml` reusable workflow and push to Docker Hub, then the target
platform pulls that pre-built image. The `--target` choice above is wired all the way
through, with two ways to set it depending on whether the choice is a one-off or a
standing default:

- **Per-run, from the "Run workflow" form (no Settings page needed).**
  `deploy-azure-vm.yml` and `deploy-azure-aca.yml` both expose a **`frontend_type`**
  dropdown right next to `environment`/`image_tag` when you open the workflow in the
  Actions tab: `(environment default)`, `react`, or `legacy`. Picking one of the
  latter two overrides everything below for that single run only -- nothing is saved,
  so the next run goes back to whatever the standing default is. This is the quickest
  way to answer "which frontend do I want *this deploy* to ship" without leaving the
  Actions tab, and it's ignored (with a note in the run summary) whenever `image_tag`
  reuses an already-built image, since that image's flavor was fixed at the time it
  was built. Whichever flavor is chosen, `image_tag` still works the same way: leave
  it blank to build that flavor fresh from this branch, or supply an existing
  version/tag to pull that flavor's already-published image instead of rebuilding.
- **Standing per-environment default, via an Actions Variable.** Set a
  **`FRONTEND_BUILD_TARGET` Actions Variable** (not secret) to `react`
  on the relevant GitHub Environment (`prod`, `vm-staging`, or `staging`/`production`
  for ACA) -- same per-Environment scoping as the existing `ENVIRONMENT` variable.
  Leave unset for the default (`legacy`). This is what
  `frontend_type`'s dropdown falls back to when left on `(environment default)`, so
  it's the right place to configure "this client's prod always ships the React SPA"
  without anyone needing to remember a dropdown on every run.
- `deploy-azure-vm.yml`/`deploy-azure-aca.yml`'s `resolve-target` job reads
  whichever of the two above applies (run input takes priority) and passes the result
  to `build-push-images.yml`'s `build_matrix` as that run's one frontend entry, whose
  `target` becomes `frontend/Dockerfile`'s `--target` flag (`frontend-react-only` or
  `frontend-legacy-only`) and whose `image_name` becomes the matching Docker Hub repo
  (`snipeit-lite-frontend-react` or `snipeit-lite-frontend-legacy`, plus this
  environment's own `-staging` suffix if any) -- the exact same mechanism as this
  file's local `docker build --target` example above, just resolved automatically
  instead of typed by hand.
- `release.yml` (tag-triggered releases) has no `workflow_dispatch` form at all (it
  only reacts to `git push --tags`), so a version tag always builds and publishes
  **both** flavors together -- `snipeit-lite-frontend-react:vX.Y.Z` AND
  `snipeit-lite-frontend-legacy:vX.Y.Z` -- as a complete, immutable release artifact
  set. Which one an actual deploy ships is still decided later, per-environment, by the
  two mechanisms above.
- For the VM path specifically, Terraform's `infra-vm/variables.tf` also has a
  `frontend_build_target` variable (`"react"` or `"legacy"`), set from the same
  `FRONTEND_BUILD_TARGET` Environment variable by `infra-deploy-vm.yml` (the standing
  default only -- this one isn't wired to the per-run dropdown, since it documents
  infra state rather than building anything). It doesn't change what gets built or
  pulled (the VM only ever pulls a pre-built image, never builds one) -- it's written
  into `/opt/snipeit/.env` as `EXPECTED_FRONTEND_BUILD_TARGET` purely so an operator
  SSHed into the VM can see which kind of image that environment is supposed to be
  running, without cross-referencing GitHub Environment settings. Keep
  `dockerhub_frontend_image` in that same `terraform.tfvars` pointed at the matching
  flavor's own Docker Hub repo -- there are two separate repos now, not one repo with
  two build modes.
- Both deploy workflows' run summaries print a **Frontend type** row -- including
  which of the two mechanisms above actually decided it for that run -- so you can
  confirm what shipped after the fact.

See `DEPLOYMENT_VM.md`'s "Frontend type" callout and `DEPLOYMENT.md`'s
`FRONTEND_BUILD_TARGET` table row for the full per-platform setup steps.

## Why React

Chose React over Vue/Svelte for this project because it's an internal admin tool with
data-heavy views (charts, tables, filters) where React's ecosystem (Recharts, mature
routing) and easy hand-off to other engineers matter more than Svelte's smaller bundle
size — both are commonly recommended for internal dashboards, but React wins on
ecosystem depth for this shape of app.

## Structure

    src/
      components/   Layout, AssetTag (signature tag card), AssetDrawer, StatCard, StatusPill
      pages/        Dashboard, Assets, Checkouts, Notifications, Login
      lib/          api.ts (fetch + fallback), mock.ts (demo data), types.ts
      index.css     design tokens (Tailwind v4 @theme) + fonts
