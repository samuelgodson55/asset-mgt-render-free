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

Built and served as part of the main app, not standalone — see `frontend/Dockerfile`'s
`frontend-app-build` stage (builds this into `/usr/share/nginx/html/app/`) and
`nginx/default.conf.template`'s `location ^~ /app/` block (SPA fallback routing). It
sits alongside the existing `admin.html`/`manager.html`/etc. static site rather than
replacing it — visit `/app/` for this UI, or `/` for the legacy one.

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
