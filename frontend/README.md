# Legacy Frontend (`frontend/`) — Frozen

This is the original vanilla HTML/JS + Tailwind (compiled ahead of time)
frontend: `index.html`/`admin.html`/`manager.html`/`staff.html`/
`customer.html`, `js/`, `css/`. **It is going out of phase and is not
receiving further maintenance.** `frontend-app/` (React + TypeScript +
Vite) is the actively developed frontend and the default everywhere —
see [`frontend-app/README.md`](../frontend-app/README.md).

## What "frozen" means here

- No new pages, components, or features.
- No refactors, dependency bumps, or stylistic clean-up for their own sake.
- No new automated test coverage beyond what already exists in
  `frontend/tests/`.
- A genuine security fix or a break that stops the app building/deploying
  is still in scope — this directory isn't abandoned, it's maintenance-mode.
- New browser-side observability work (ErrorBeacon coverage, tracing) should
  go into `frontend-app/`; this frontend keeps whatever bridge it already
  has so it doesn't silently go dark, not as a target for new instrumentation.

## Why it's still in the repo

Some existing deployments (Docker's `frontend-legacy-only` build target,
or `FRONTEND_BUILD_TARGET=legacy` on Render/ACA/VM) still run this site,
and switching frontends is an explicit, per-environment deploy decision —
see `docker-compose.yml`'s `frontend` service comment and
`frontend/Dockerfile`. Removing the code out from under those environments
isn't something to do casually. It stays buildable, testable in CI, and
deployable for as long as anyone runs it in production; it just isn't where
new work happens.

## If you're picking a frontend for something new

Use `frontend-app/`. If you're not sure whether a change belongs here or
there, it almost certainly belongs in `frontend-app/`.

## Reference

The full file-by-file function reference for this frontend (kept for
whoever still operates it) lives in
[`docs/README.md`](../docs/README.md)'s **Frontend — Core (`frontend/js/`)**
and **Frontend — Components** sections, both marked "legacy, frozen" there
too.
