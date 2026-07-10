# Snipe-IT Lite

A small, self-hosted **IT asset registry / equipment checkout system** —
think "who currently has the MacBook Pool unit #12, and when is it due
back?" Built with a **FastAPI + PostgreSQL** backend and a **plain
HTML/JS (no framework, no build step)** frontend, all wired together with
Docker Compose.

This README is a **complete guide for a beginner developer**. It assumes
you can read code but may not have deployed a full-stack app before. It
covers: what the app does, how to run it (with Docker and without), what
every file is for, how the pieces fit together, how to safely make changes
or add features, how to test what you build, and how to move this toward a
real production deployment.

> **New here? Read in this order:** [Quick Start](#quick-start-docker) to get it
> running → [Feature Tour](#feature-tour) to see what it does →
> [How A Request Flows](#how-a-request-flows-through-the-app) for the
> mental model → [Making Changes Safely](#making-changes-safely-a-guide-for-beginners)
> when you're ready to modify something.

---

## Table of Contents

1. [What This App Does](#what-this-app-does)
2. [Feature Tour](#feature-tour)
3. [Tech Stack](#tech-stack)
4. [Project Structure](#project-structure)
5. [How A Request Flows Through The App](#how-a-request-flows-through-the-app)
6. [Quick Start (Docker)](#quick-start-docker)
7. [Deploying on Render's Free Plan](#deploying-on-renders-free-plan)
8. [Running Without Docker (Local Dev)](#running-without-docker-local-dev)
9. [Environment Variables Reference](#environment-variables-reference)
10. [Roles & Permissions Model](#roles--permissions-model)
11. [Database & Migrations (Alembic)](#database--migrations-alembic)
12. [Full API Reference](#full-api-reference)
13. [File & Function Reference](#file--function-reference)
14. [Making Changes Safely (A Guide For Beginners)](#making-changes-safely-a-guide-for-beginners)
15. [Testing Your Changes](#testing-your-changes)
16. [Continuous Integration](#continuous-integration)
17. [Security Model](#security-model)
18. [Running In Production](#running-in-production)
19. [Suggested Future Features](#suggested-future-features)
20. [Troubleshooting](#troubleshooting)

---

## What This App Does

Five kinds of people use the system:

| Role | Can do |
|------|--------|
| `super_admin` | Everything `admin` can do (see below). This is a single **hardcoded** identity, not a `users` table row — configured via `SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_PASSWORD` in your environment (see [Environment Variables Reference](#environment-variables-reference)). There is always exactly one, it can never be created/edited/deleted through the app, and it never appears in the User Directory or any other listing. |
| `admin` | Everything: create/delete asset pools, adjust capacity, flag maintenance exceptions, provision any account, view/export all audit logs, export properties for anyone. A normal, database-backed, editable/deletable account — functionally identical in privilege to `super_admin`, just not the one hardcoded root identity. |
| `manager` | View inventory, dispatch/check-in items to Staff, Linked Customers, or Ad-Hoc Individuals; manage custody for ANY user (no department-scoping); provision new Staff/Customer logins (never Manager/Admin) with any department; export properties/audit data system-wide, same as Admin. |
| `staff` | Self-service dashboard showing their own checked-out items, with the ability to export their own list; can view/edit their own profile and change their own password. |
| `customer` | Same as staff, but for an external contact who has a login (as opposed to an "Ad-Hoc Individual", who doesn't — see below). |

Wherever the rest of this README says "Super Admin", it applies equally to
both `super_admin` and `admin` — they're permission-equivalent everywhere
in the app. The distinction only matters for account *lifecycle*: one is a
hardcoded singleton you configure via environment variables, the other is
an ordinary account you create/manage through the UI like anyone else. See
[Roles & Permissions Model](#roles--permissions-model) for the full
rationale.

The core object is an **Asset Pool** (`AssetType`, e.g. "MacBook Pro 14"
M3 Pool") with a `total_quantity`. Units of that pool can be:

- **Checked out** to a User (Staff/Customer) or an ad-hoc **Outsider**
  (`AssetCheckout`),
- **Isolated** for repair/loss (`AssetException`), or
- **Available** (everything else).

```
Available = Total − Outbound (active checkouts) − Isolated
```

This formula is recalculated by `backend/services/stock.py` after every
mutating action, so it's never allowed to drift out of sync. Every
meaningful action in the system (checkouts, returns, pool changes, account
changes, etc.) is written to an append-only `AuditLog` that can never be
edited or deleted through the app.

## Feature Tour

This section walks through **every feature** the app has, organized by
who uses it.

### Asset Inventory (Super Admin)

- **Create an asset pool** — name + starting quantity + optional custom
  fields (JSON blob for anything pool-specific you want to track).
- **Adjust capacity** — change `total_quantity` up or down at any time;
  the Available count recalculates automatically.
- **Flag a unit as unavailable** ("Exception") — mark one serial number as
  "Under Repair", "Stolen", "Missing", etc. It's pulled out of the
  Available pool until someone "Recalls" it back into service.
- **Bulk import via CSV** — upload a spreadsheet to create many pools at
  once. Files over 5 MiB are rejected before parsing (denial-of-service
  protection). Malformed rows are reported back to you individually
  instead of failing the whole import.
- **Dispatch (checkout)** — assign N units of a pool to a Staff member, a
  Customer, or an ad-hoc Outsider, with a due date. A due date is
  **required** for anyone without a login (ad-hoc Outsiders), since
  there's no dashboard reminding them.
- **Delete an asset pool** — permanently remove a pool from active
  inventory (soft-delete: the row and its full checkout/exception history
  stay intact for the audit trail, it just disappears from listings).
  Blocked while the pool still has outstanding checkouts or isolated
  (under-repair/stolen) units, so inventory can never silently "disappear"
  out from under an active custody or maintenance record. Reachable two
  ways: a "Delete" button directly on each row of the Asset Inventory
  table, or a "Delete Asset Pool" button inside that pool's own Properties
  Hub modal.

### Custody & Returns (Super Admin / Manager)

- **Custody Ledger** — open any user's or ad-hoc individual's page to see
  everything currently checked out to them.
- **Process a return** — partial returns are supported (e.g. someone
  returns 3 of the 5 units they were issued; the other 2 stay outstanding
  on the same checkout record).
- **Bulk return** — select multiple line items in the Custody Ledger and
  return them all in one action.
- **Extend a due date directly** — an "Extend" button sits next to
  "Process Return" on every line item in the Custody Ledger drawer (User
  Directory AND Ad-Hoc Directory both share this same drawer). Lets a
  Manager/Admin/Super Admin grant more time on the spot — no separate
  request/approval round trip — for a case where they're the one
  initiating it (e.g. on a phone call with the holder). See [Due-Date
  Extensions & Notifications](#due-date-extensions--notifications) below
  for how this differs from the self-service request flow.
- **Overdue alerts** — a banner on the Admin/Manager dashboard lists every
  active checkout whose due date has passed, most-overdue-first.

### Due-Date Extensions & Notifications

Everyone gets a piece of this, scoped by role. Three related pieces, all
backed by `backend/services/extension_service.py`:

- **Request an extension (Staff / Customer, self-service)** — a "Request
  Extension" button sits next to every row in "My Items"
  (`staff.html`/`customer.html`). Pick a new due date and an optional
  reason; it's submitted as a **pending request**, not applied
  immediately — someone with a Manager/Admin/Super Admin role still has to
  approve it. A Manager/Admin/Super Admin can also log a request **on
  behalf of** an Ad-Hoc Individual (Outsider), who has no login/dashboard
  of their own to do this themselves — e.g. after a phone call.
- **Review requests (Manager / Admin / Super Admin)** — an "Extension
  Requests" panel on the Admin/Manager dashboard lists every pending
  request system-wide (Managers have no department-scoping — they see
  exactly what an Admin/Super Admin sees) with one-click
  **Approve**/**Deny** buttons. Approving is what actually moves the
  checkout's real due date; denying leaves it untouched. Both write an
  audit log entry and email the requester back (if they're a logged-in
  User with an email address).
- **Grant one directly (Manager / Admin / Super Admin)** — see the
  **Extend** button described in [Custody & Returns](#custody--returns-super-admin--manager)
  above. Same unrestricted permission as approving a request, just
  without the request existing first.

**Email notifications** (`backend/services/notification_service.py` +
`backend/tasks/notification_tasks.py`) — plain SMTP, no vendor SDK, off by
default (`NOTIFICATIONS_ENABLED=false` — see [Environment Variables
Reference](#environment-variables-reference)). Two kinds go out once
enabled and configured:
1. **Extension-request lifecycle** — every Manager/Admin (system-wide) is
   emailed the moment a new request comes in; the checkout's holder is
   emailed back once their request is approved or denied, or once a
   Manager/Admin/Super Admin grants an extension directly (no request
   needed for that email to go out).
2. **Daily overdue digest** — an in-process scheduler thread
   (`send_overdue_notifications`, see [`backend/scheduler.py`](backend/scheduler.py),
   every `OVERDUE_NOTIFICATION_INTERVAL_HOURS` — 24 by default) emails each
   overdue checkout's own holder a reminder, plus one combined system-wide
   summary digest to every Manager and Admin/Super Admin (Managers have no
   department-scoping, so they get the exact same full list Admins do).

Every one of these emails runs on a **background thread**
(`tasks.send_email_task`, submitted via [`backend/jobs.py`](backend/jobs.py))
rather than sent inline in the request/response cycle — a slow or
unreachable SMTP server can add several seconds of latency
(`smtplib.SMTP(..., timeout=10)`), and doing that inline used to make the
"Request Extension" modal and the "Extension Requests" panel both feel
like they hung before clearing. The API now commits the database change
and returns immediately; the actual email goes out a moment later,
out-of-band — the same in-process background-thread pattern already used
for audit-ledger exports (see [Tech Stack](#tech-stack)). If
`NOTIFICATIONS_ENABLED=false` (the default), every notification is simply
logged at `DEBUG` level instead of sent — nothing here requires a mail
server to develop or demo the app locally.

### Directories (Super Admin / Manager)

- **User Directory** — every Staff/Customer/Manager/Admin account. Both a
  Manager and an Admin/Super Admin see the entire directory — Managers have
  no department-scoping anywhere in this app. The hardcoded `super_admin`
  identity itself never appears here (or anywhere else the directory is
  listed/exported) — it isn't a `users` table row, so there's nothing for
  these queries to return.
- **Ad-Hoc (Unlinked) Directory** — external people who receive equipment
  but never get a login (contractors, vendors, guests, etc.) — created
  automatically the first time you check something out to a new name.
- **Provision a new account** — Super Admins can create any role; Managers
  can only create Staff/Customer accounts, but (like a Super Admin) can set
  any department they like on a new Staff account.
- **Soft-delete an account/pool** — nothing is ever hard-deleted. A
  "deleted" row just gets hidden from listings and can no longer log in
  (for a User); its full history stays intact for the audit trail.
- **Restore a deleted account (Super Admin / Admin only)** — the User
  Directory page has a "Restore Deleted Users" panel below the Provision
  form, listing every soft-deleted account with its own search + paging.
  One click on "Restore" flips it back to active — login is re-enabled and
  it reappears in the main directory immediately, with the exact same
  email/username/role/department it had before deletion (nothing needs to
  be re-typed).
- **Reset a user's password (Super Admin / Admin only)** — the "forgot
  password" recovery path. Click "Reset Password" next to any account in
  the User Directory, type a new password (it must meet the same
  complexity policy as any other password), and it takes effect
  immediately — no need to know, or ask the user for, their old password.
- **Search + pagination, entirely server-side** — the Asset Inventory,
  User Directory, and Ad-Hoc Directory tables (like the Audit Ledger)
  never download an entire table into the browser. Every keystroke in a
  search box (debounced ~300ms so it doesn't fire on every keypress),
  page turn, or "rows per page" change re-fetches just that slice from
  the API via `?search=&limit=&offset=`, so these directories stay fast
  and responsive no matter how large they grow. See [How A Request Flows
  Through The App](#how-a-request-flows-through-the-app) and
  `services/search_utils.py` for how the search matching works.

### Exporting Data (all roles, scoped by permission)

Everyone can export the data they're allowed to see, as **CSV** (for
spreadsheets) or **PDF** (for printing/sharing):

| Who | What they can export |
|---|---|
| Staff / Customer | Their own "Properties Assigned To Me" list. |
| Manager | One specific user's or ad-hoc individual's custody ledger; a bulk export of every active checkout system-wide, or across all ad-hoc individuals; the full audit ledger — all unrestricted, same as Super Admin. |
| Super Admin | Any/all of the above, unrestricted. |

All exports share one formatting/security layer
(`backend/services/export_service.py`) — see
[Full API Reference](#full-api-reference) for the exact endpoints.

### "My Profile" (everyone)

Click your name in the navbar on any dashboard to:
- View your name, email, username, role, and department (fetched fresh
  from the server every time you open it — never a stale cached value).
- Change your own password (you must correctly enter your *current*
  password first — this prevents someone who steals an unattended, still
  logged-in session from locking you out of your own account). If you've
  forgotten your current password entirely, a Super Admin or Admin can
  reset it for you instead — see [Directories](#directories-super-admin--manager).

### Audit Trail (Super Admin / Manager)

- A permanent, paginated, append-only log of every meaningful action:
  pool created/deleted, capacity changed, unit flagged/recalled, checkout,
  return, account created/deleted, password changed, etc.
- Every entry records **who did it** (`operator`), **what** (`action`),
  **which record it affected** (`target_type`/`target_id`), and a
  human-readable **details** string. A return entry specifically also
  names **who the equipment was returned from** — a linked User (name +
  email) or an unlinked ad-hoc Outsider (name, and company if known).
- Exportable as CSV or PDF. Export generation runs on a background thread
  (see [Tech Stack](#tech-stack)) rather than inline in the request —
  clicking "Export" submits a job and polls it to completion before
  downloading, so a wide date range never risks tying up the API or
  timing out the browser. Both a Manager and a Super Admin see/export
  the entire ledger — Managers have no department-scoping anywhere in
  this app.

### Account Security (built-in, mostly invisible until you need it)

- Passwords are hashed with **Argon2id**, never stored or logged in plain
  text, and must meet a minimum complexity policy.
- Login is rate-limited **per IP address** (a burst of guesses from one
  source gets slowed down) **and** **per account** (after too many wrong
  passwords against the *same* account, that account is locked for a
  cooldown period no matter which IP the attempts come from). A Super
  Admin/Admin resetting the account's password also clears this lockout
  state early, same as the account holder finally remembering their own
  password.
- **Locked out or forgot your password?** A Super Admin or Admin can reset
  it for you directly from the User Directory — see
  [Directories](#directories-super-admin--manager). This is a separate,
  admin-only recovery path from the self-service "My Profile" password
  change, and never requires knowing (or being told) the old password.
- Sessions use signed JWTs; deactivating or deleting an account takes
  effect **immediately** on their next request, rather than waiting for
  their token to naturally expire.
- An idle dashboard automatically logs you out after a period of
  inactivity.

## Tech Stack

- **Backend:** Python 3.11, FastAPI, SQLAlchemy 2.0, PostgreSQL 16,
  Alembic (migrations), PyJWT (session tokens), `pwdlib`/Argon2id
  (password hashing), Pydantic Settings v2 (typed config), `reportlab`
  (PDF generation for exports). Audit-ledger exports and the
  overdue-checkout email digest run as **in-process background
  threads** (see [`backend/jobs.py`](backend/jobs.py) and
  [`backend/scheduler.py`](backend/scheduler.py)) rather than a separate
  Celery + Redis worker — see [Deploying on Render's Free
  Plan](#deploying-on-renders-free-plan) for why.
- **Email:** plain SMTP (`smtplib`, no vendor SDK) via
  `backend/services/notification_service.py` — works unmodified against a
  self-hosted mail server or any hosted provider's SMTP endpoint
  (SendGrid, Mailgun, AWS SES, etc.). Off by default
  (`NOTIFICATIONS_ENABLED=false`); see [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications) and [Environment
  Variables Reference](#environment-variables-reference).
- **Frontend:** Plain HTML + vanilla JS ES Modules — **no React/Vue, no
  app build step** — styled with Tailwind CSS, compiled locally ahead of
  time to a single static `frontend/css/tailwind.css` (see
  [`build-tailwind/README.md`](build-tailwind/README.md)) instead of being
  pulled from a CDN and recompiled in every visitor's browser at runtime.
  Served directly by the SAME FastAPI process as the API, via Starlette's
  `StaticFiles` (see [`backend/main.py`](backend/main.py)) — no separate
  nginx container.
- **Infra:** ONE Docker image (built from the root
  [`Dockerfile`](Dockerfile)), containing both the API and the static
  frontend, run as ONE process. Locally, `docker-compose.yml` runs just
  two services: `db` (Postgres) and `app` (that one combined image). See
  [Deploying on Render's Free Plan](#deploying-on-renders-free-plan) for
  why this is a single service rather than the split
  backend/worker/frontend/Redis shape you might expect from a "real"
  production app — short version: that split doesn't fit any platform's
  free tier, and this app is explicitly sized to run on one for free.

Because `frontend/css/tailwind.css` is a plain committed file (not
generated inside the Docker build), **editing an `.html` or `.js` file
and refreshing your browser is still the entire "deploy" cycle** while
developing locally — nothing to compile, and `docker compose up` needs no
`npm install` of its own. The only time you touch `build-tailwind/` is
if you add/remove Tailwind utility classes and need to regenerate that
one CSS file — see that folder's README for the one-line command.

## Project Structure

```
snipe-it-lite/
├── Dockerfile                  # Single image: backend + frontend, one process
├── .dockerignore                # Keeps .env (and other junk) out of the build context
├── docker-compose.yml           # 2 services: db, app (local dev)
├── render.yaml                   # Render Blueprint -- ONE free web service +
│                                    # ONE free Postgres, see "Deploying on
│                                    # Render's Free Plan" section above
├── .env.example                 # Copy this to .env and fill in real secrets
├── .gitignore                    # Keeps .env (and other junk) out of git
│
├── .github/
│   └── workflows/
│       └── ci.yml                 # Lint (ruff), pytest smoke tests against a
│                                     # real Postgres, Docker build, and
│                                     # render.yaml/docker-compose.yml validation
│                                     # -- runs on every push/PR
│
├── backend/
│   ├── main.py                    # FastAPI app: middleware, startup, routers,
│   │                                 # AND the static-frontend mount (StaticFiles)
│   ├── config.py                   # Pydantic Settings -- all env vars, one place
│   ├── database.py                  # SQLAlchemy engine/session + init_db()/seed_db()
│   ├── models.py                     # SQLAlchemy ORM table definitions
│   ├── security.py                    # Password hashing, password policy, JWT
│   ├── deps.py                         # get_current_user / role-gate dependencies
│   ├── logging_config.py                # Structured (JSON) logging setup
│   ├── jobs.py                            # In-process background thread pool --
│   │                                         # replaces Celery+Redis for async
│   │                                         # audit exports + fire-and-forget email
│   ├── scheduler.py                        # In-process daily overdue-digest timer
│   │                                          # thread -- replaces Celery Beat
│   ├── requirements.txt                    # Python dependencies (production image)
│   ├── requirements-dev.txt                 # + pytest/httpx/ruff, CI-only
│   ├── pyproject.toml                        # ruff lint config
│   │
│   ├── tests/                     # pytest smoke tests -- run by .github/workflows/ci.yml
│   │   └── test_smoke.py
│   │
│   ├── tasks/                     # Plain functions, run via jobs.py's thread pool
│   │   ├── export_tasks.py          # generate_audit_export(): builds the CSV/PDF
│   │   │                              # off the request/response cycle
│   │   └── notification_tasks.py     # send_email_task() (generic async email --
│   │                                    # used by extension_service.py too) +
│   │                                    # send_overdue_notifications() (the daily
│   │                                    # digest -- see scheduler.py)
│   │
│   ├── middleware/                # ASGI middleware, one concern per file
│   │   ├── request_context.py       # Request Correlation ID (X-Request-ID)
│   │   ├── rate_limit.py             # Per-IP login rate limiting
│   │   └── security_headers.py       # Standard defensive response headers + CSP
│   │
│   ├── api/                       # Thin FastAPI routers (HTTP layer only),
│   │   │                            # all mounted under /api in main.py
│   │   ├── auth.py, assets.py, users.py, outsiders.py, checkouts.py, audit.py
│   │   └── system.py                 # GET /system/health (uptime pinger target)
│   │                                    # + POST /system/notifications/run
│   │                                    # (manual/external-scheduler trigger)
│   │
│   ├── schemas/                   # Pydantic request/response models
│   │   ├── auth.py, assets.py, users.py, checkouts.py
│   │
│   ├── services/                  # All business logic / DB queries live here
│   │   ├── auth_service.py           # Login, password changes, account lockout
│   │   ├── asset_service.py           # Asset pool CRUD, checkout, CSV import
│   │   ├── user_service.py             # User directory, self/bulk exports
│   │   ├── outsider_service.py          # Ad-hoc directory, exports
│   │   ├── checkout_service.py           # Returns, overdue-checkout feed
│   │   ├── extension_service.py           # Due-date extension requests +
│   │   │                                    # decisions + direct grants
│   │   ├── notification_service.py         # The one place that calls smtplib --
│   │   │                                     # see services/notification_service.py
│   │   ├── audit_service.py               # Audit ledger + CSV/PDF export
│   │   ├── export_service.py               # Shared CSV/PDF builders
│   │   ├── search_utils.py                  # Shared ILIKE search-filter helper
│   │   │                                       # (GET /assets, /users, /outsiders)
│   │   └── stock.py                         # The Available-quantity formula
│   │
│   └── alembic/                    # Database migration scripts
│       ├── env.py
│       └── versions/
│           └── 0001_baseline_schema.py   # the ONLY migration -- see "Database &
│                                           # Migrations" below for why there's no
│                                           # 0002/0003/... chain
│
├── build-tailwind/              # Build tooling ONLY -- never shipped/run in
│   │                              # Docker. Compiles frontend/css/tailwind.css.
│   │                              # See build-tailwind/README.md.
│   ├── tailwind.config.js         # Theme colors/fonts, single source of truth
│   ├── input.css                   # @tailwind base/components/utilities
│   └── package.json                 # `npm run build` / `npm run watch`
│
└── frontend/                     # Served directly by backend/main.py -- no
    │                                # separate container/service
    ├── index.html            # Login page
    ├── admin.html            # Super Admin dashboard
    ├── manager.html          # Manager dashboard
    ├── staff.html            # Staff self-service dashboard
    ├── customer.html         # Customer self-service dashboard
    ├── css/
    │   └── tailwind.css       # Compiled by build-tailwind/ -- committed as a
    │                            # plain static file, not generated by Docker
    └── js/
        ├── main.js            # Wires up every DOM event (event delegation)
        ├── api.js             # The one place that calls fetch()
        ├── auth.js            # Login/session/JWT-decode/role guard
        ├── ui.js               # Shared table/pagination/modal helpers
        ├── dashboard.js         # refreshDashboard() orchestrates all loads
        └── components/           # One file per feature area
            ├── assets.js           # Inventory table, dispatch, exceptions, CSV import
            ├── audit.js             # Audit ledger table + CSV/PDF export
            ├── custody.js            # Custody Ledger modal + returns + direct Extend
            ├── exports.js             # Properties-assigned CSV/PDF downloads
            ├── extensions.js           # Request/Approve/Deny + direct-extend modals
            ├── myitems.js               # Staff/Customer "what do I have?" view
            ├── outsiders.js             # Ad-Hoc directory table
            ├── overdue.js                # Overdue-checkouts alert banner
            ├── profile.js                 # "My Profile" modal + change password
            └── users.js                    # User directory table, provisioning,
                                              #   admin password reset, restore
```

## How A Request Flows Through The App

This is the most important mental model in the whole project. Once you
understand this, every file's purpose becomes obvious.

```
Browser (frontend/js)
   │  fetch('/api/...') — see js/api.js (relative path, no hardcoded host)
   ▼
main.py's /api routers    <-- SAME FastAPI process that also serves the
                              static frontend files (see main.py's
                              `app.include_router(..., prefix="/api")`
                              calls and its StaticFiles mount) -- no
                              separate reverse-proxy hop anymore.
   │
   ▼
api/*.py            <-- HTTP layer only: parses the request body, checks
                        "is this person allowed to call this?", then hands
                        off to a services/*.py function. Contains almost
                        no actual logic.
   │
   ▼
services/*.py       <-- ALL business logic lives here: validation beyond
                        what Pydantic already checked, database queries,
                        the Available-quantity formula, audit log writes.
                        This is where you look for "how does X actually
                        work?"
   │  SQLAlchemy ORM (never raw SQL)
   ▼
models.py           <-- Table definitions. The shape of the data.
   │
   ▼
PostgreSQL
```

Two more pieces sit alongside this flow:

- **`schemas/*.py`** define what a valid request/response *looks like* —
  Pydantic validates this automatically before your route code even runs
  (e.g. "is `quantity` a positive integer?", "does this password meet the
  complexity policy?"). If validation fails, FastAPI returns a `422` error
  before your function body executes at all.
- **`deps.py`** defines *who's allowed* to call a route (e.g.
  `require_super_admin`, `require_privileged_role`) — these run as FastAPI
  dependencies, one line above your route function, keeping permission
  checks consistent instead of hand-rolled inside every function.

**Nothing outside `database.py` talks to SQLAlchemy's engine directly** —
every other file gets a `Session` handed to it via the `get_db()`
dependency.

On the frontend, the equivalent flow is:

```
User clicks something (data-action="..." attribute)
   │
   ▼
js/main.js's event delegation      <-- one click listener on the whole
                                        page, dispatches based on the
                                        data-action attribute
   │
   ▼
js/components/*.js                 <-- one file per feature area; calls
                                        apiRequest() (js/api.js) to talk
                                        to the backend, then re-renders
                                        its own table/modal
   │
   ▼
js/ui.js's shared helpers          <-- escapeHtml(), pagination, modals
```

## Quick Start (Docker)

This is the fastest way to get the whole app running — no Python or
Node.js installation needed on your machine at all, just Docker.

```bash
# 1. Copy the environment template and fill in real secrets
cp .env.example .env

#    - Generate a real JWT secret and paste it into .env as JWT_SECRET_KEY:
python3 -c "import secrets; print(secrets.token_hex(32))"

#    - Pick a real POSTGRES_PASSWORD and update DATABASE_URL in .env to match
#      (the placeholder password appears in BOTH places -- keep them in sync).

# 2. Build and start everything (Postgres + the one combined app container)
docker compose up --build

# 3. Open the app -- everything (frontend + API) is served from ONE origin,
#    ONE container, straight from FastAPI (see backend/main.py):
#    App (login page):  http://localhost:8000
#    API docs:           http://localhost:8000/docs
```

Leave the terminal running to see live logs from both containers. Press
`Ctrl+C` to stop everything, or run `docker compose up -d --build` to
start it in the background instead.

**To stop and remove the containers (keeping your database data):**
```bash
docker compose down
```

**To wipe the database completely and start fresh** (useful if you break
something while experimenting):
```bash
docker compose down -v   # -v also removes the named Postgres volume
docker compose up --build
```

### Demo Login Credentials

The very first time the app starts against an **empty** database, it
seeds a few demo accounts and some sample inventory/checkouts (see
`backend/database.py -> seed_db()`) so you have something to look at
immediately. This only happens if `AUTO_SEED_DEMO_DATA=true` (the default
for local dev — see [Environment Variables Reference](#environment-variables-reference)).

| Role | Email | Username | Password |
|------|-------|----------|----------|
| Admin | `r.adeyemi@corp.io` | `r.adeyemi` | `Admin123!` |
| Manager | `s.chen@corp.io` | `s.chen` | `Manager123!` |
| Staff | `t.okafor@corp.io` | `t.okafor` | `Staff123!` |
| Customer | `d.martins@customer.io` | `d.martins` | `Customer123!` |

Login accepts **either** the email or the username in the same field.

**Super Admin** isn't seeded here — it's the hardcoded root identity
described in [Roles & Permissions Model](#roles--permissions-model). For
local Docker Compose, `.env.example`'s defaults let you log in with
username `superadmin` / password `change-this-super-admin-password`; set
your own `SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_PASSWORD` before deploying
anywhere real.

## Deploying on Render's Free Plan

This app is deliberately sized to deploy **entirely within Render's free
plan — $0/month, no credit card required for the free resources
themselves.** That took some real re-shaping (see the note below if
you're curious why), and it comes with a few honest trade-offs — both
covered here.

### Why this looks different from a "typical" production deployment

A more conventional version of this app would split into a private
FastAPI backend, a separate Celery worker + Redis for background jobs,
and a public nginx frontend/reverse-proxy in front of it all — five
resources total. Render's (and most platforms') **free** tier doesn't
support that shape at all:

- Free instance types are limited to **Web Services, Postgres, and Key
  Value (Redis-compatible) instances** — private services and background
  workers always require a **paid** plan. (See
  [render.com/docs/free](https://render.com/docs/free).)
- Free web services **can't receive private network traffic** from other
  services either, so even a free nginx-in-front-of-a-free-backend split
  wouldn't work.

So this app now runs as **ONE Docker image** (built from the root
[`Dockerfile`](Dockerfile)) that serves both the JSON API (`/api/*`) and
the static frontend from a single FastAPI process — see
[`backend/main.py`](backend/main.py)'s module docstring for the full
explanation. The audit-export and overdue-notification background jobs
that used to run on a separate Celery worker now run as **in-process
background threads** inside that same process instead — see
[`backend/jobs.py`](backend/jobs.py) and
[`backend/scheduler.py`](backend/scheduler.py).

### Deploy it

The easiest path is the [`render.yaml`](render.yaml) **Blueprint** at the
repo root, which provisions both resources (the free Postgres database
and the free web service) in one shot and wires them together
automatically:

- [ ] Push `render.yaml` (already at the repo root) to your Git provider.
- [ ] In the Render Dashboard: **New** → **Blueprint** → connect this repo.
      Render reads `render.yaml`, shows you the two resources it's about
      to create (`snipeit-lite-db`, `snipeit-lite`), and provisions them
      on **Deploy Blueprint**.
- [ ] That's it — `render.yaml` uses Blueprint's `fromDatabase` reference
      to fill in `DATABASE_URL` from the Postgres instance automatically.
      There's no "copy the connection string from the dashboard" step to
      do by hand.
- [ ] `JWT_SECRET_KEY` and `SUPER_ADMIN_PASSWORD` are both auto-generated
      by Render (`generateValue: true`) — you never need to invent or
      store either one yourself. Find the generated
      `SUPER_ADMIN_PASSWORD` in the dashboard's **Environment** tab if you
      need to log in as the hardcoded Super Admin (see
      [Roles & Permissions Model](#roles--permissions-model)).
- [ ] `ENABLE_API_DOCS` is already set to `false` in `render.yaml` —
      `/docs`, `/redoc`, and `/openapi.json` are disabled by default on
      this Blueprint, not just left at their locally-convenient default.
      See the [Security Model](#security-model) section if you ever want
      to turn them back on temporarily.
- [ ] Verify: load the service's public Render URL, log in, and confirm
      everything works (open your browser's Network tab — you should see
      `200`s from `/api/auth/login` etc.).

Prefer to click through the dashboard manually instead of using the
Blueprint? Create one **Web Service**, Free plan, `runtime: docker`,
`dockerfilePath: ./Dockerfile`, and one **PostgreSQL** database, Free
plan — then copy the `envVars` list from `render.yaml` into that Web
Service's Environment tab by hand.

### Known free-tier trade-offs (read this before you rely on it)

- **The service spins down after 15 minutes of no inbound traffic**, and
  takes roughly 30–60 seconds to spin back up on the next request. Fine
  for a demo, personal project, or internal tool with light traffic; not
  what you want for an always-on production tool — upgrade this one
  service off the Free instance type if that matters to you (everything
  else about this Blueprint stays exactly the same).
- **The free Postgres instance expires after 30 days** and isn't
  automatically backed up or renewed — Render emails you before it
  expires. Upgrade it to a paid plan before then if you need this data to
  persist long-term.
- **The daily overdue-checkout email digest only fires while the service
  is awake** (see [`backend/scheduler.py`](backend/scheduler.py)'s
  docstring) — the spin-down above pauses it along with everything else.
  Two ways to make it reliable despite that, in increasing order of
  effort:
  1. Point a free external uptime pinger (e.g.
     [UptimeRobot](https://uptimerobot.com),
     [cron-job.org](https://cron-job.org)) at
     `GET https://<your-app>.onrender.com/api/system/health` every few
     minutes — keeps the service (and the scheduler thread inside it)
     from ever spinning down.
  2. Point an external scheduler directly at
     `POST https://<your-app>.onrender.com/api/system/notifications/run`
     (with an `X-Task-Token` header matching your `SYSTEM_TASK_TOKEN`) —
     a free GitHub Actions **scheduled workflow** (`on: schedule:`) works
     well for this and costs nothing on a public repo. See
     [`backend/api/system.py`](backend/api/system.py) and
     `SYSTEM_TASK_TOKEN` in [Environment Variables
     Reference](#environment-variables-reference).
- **Only ever runs as ONE instance.** Free instances can't horizontally
  scale, which is actually why the in-process job system in
  [`backend/jobs.py`](backend/jobs.py) is safe to use here at all — see
  that file's docstring for what would need to change if you later
  upgrade to a paid plan *and* turn on multiple instances.

### Other platforms (Cloud/AWS/GCP/Azure/self-hosted)

Nothing about this app is Render-specific — it's just one Docker image
(the root `Dockerfile`) plus a Postgres database. Any platform that can
run a Docker container and give it a `DATABASE_URL` works the same way:
build the root `Dockerfile`, set the environment variables from
`.env.example`, point `DATABASE_URL` at your Postgres instance, and make
sure the container's `$PORT` env var (or your platform's equivalent) is
honored — the Dockerfile's `CMD` already reads `$PORT` at startup. If
your platform *does* give you real background workers/cron jobs for
free/cheap, you could reintroduce a separate worker for the notification
digest instead of relying on `backend/scheduler.py` — that file's
docstring covers the trade-off.

## Running Without Docker (Local Dev)

If you'd rather run the backend directly with Python (e.g. to use a
debugger, or because Docker isn't available), here's how. You'll still
need a PostgreSQL server running somewhere reachable (Docker is still the
easiest way to get *just* Postgres — see the snippet below).

```bash
# 1. Start ONLY a Postgres container (skip the "app" container)
docker compose up db

# 2. In a separate terminal, set up a Python virtual environment
cd backend
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Point the app at that Postgres container. Easiest way: export the
#    same DATABASE_URL your .env file has, or just `export $(cat ../.env
#    | grep -v '^#' | xargs)` to load the whole .env file into your shell.
export DATABASE_URL="postgresql://admin:change-this-to-a-long-random-password@localhost:5432/asset_db"
export JWT_SECRET_KEY="any-random-string-for-local-dev"

# 4. Run the backend with live-reload -- this ALSO serves the frontend
#    (see backend/main.py's StaticFiles mount), so there's nothing extra
#    to run for the frontend. Just open the URL below.
uvicorn main:app --reload --host 0.0.0.0 --port 8000
# now open http://localhost:8000
```

That's it — no separate frontend server, no reverse proxy to configure.
`frontend/js/api.js`'s `API_URL` is a relative path (`/api`), which
resolves correctly here because the SAME `uvicorn` process is serving
both the API and the static frontend files (see [Tech
Stack](#tech-stack) above).

## Environment Variables Reference

All of these live in `.env` (copied from `.env.example`, never committed —
see `.gitignore`) and are read by `backend/config.py` into a single typed
`settings` object that every other backend module imports.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENVIRONMENT` | `development` | `production` enables the startup JWT-secret strength check and adds an HSTS response header. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | see `.env.example` | Postgres credentials, shared by the `db` and `app` services. |
| `DATABASE_URL` | built from the above | Full SQLAlchemy connection string. |
| `EXPORT_JOB_TTL_SECONDS` | `3600` | How long a finished export job's file bytes stay cached in memory (see [`backend/jobs.py`](backend/jobs.py)) before expiring. |
| `JWT_SECRET_KEY` | *(required, no insecure default allowed)* | Signs/verifies session tokens. **Must** be a long random string in production. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `JWT_EXPIRY_HOURS` | `12` | How long a login session stays valid. |
| `CORS_ORIGINS` | *(empty)* | Comma-separated list of origins allowed to call the API cross-origin. Almost never needed — the frontend and API are served from the same origin now (see [Tech Stack](#tech-stack)). |
| `AUTO_INIT_DB` | `true` | If true, runs `create_all()` on startup (creates missing tables). Set `false` in production and use Alembic instead. |
| `AUTO_SEED_DEMO_DATA` | `true` | If true, seeds demo accounts/data on an empty DB at startup. Set `false` in production. |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. |
| `LOG_FORMAT` | `json` | `json` (production/log aggregators) or `text` (readable local dev). |
| `LOGIN_RATE_LIMIT_MAX` | `5` | Max `/api/auth/login` attempts per IP per window before HTTP 429. |
| `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | `60` | The window (in seconds) the above limit applies over. |
| `ENABLE_API_DOCS` | `true` | Whether `/docs`, `/redoc`, `/openapi.json` exist at all. **Set `false` in Render/cloud** — see the Security Model section below. |
| `SUPER_ADMIN_USERNAME` | `superadmin` | Login identifier for the hardcoded Super Admin (root) account — see [Roles & Permissions Model](#roles--permissions-model). |
| `SUPER_ADMIN_NAME` | `Super Admin` | Display name for that account (shown in the navbar/profile, same as any other user's `name`). |
| `SUPER_ADMIN_PASSWORD` | *(placeholder, must be changed in production)* | Password for the hardcoded Super Admin. Leaving it empty fully disables that login path. **Must** be a real, unique value in production — the backend refuses to start otherwise (same idea as `JWT_SECRET_KEY`). |
| `NOTIFICATIONS_ENABLED` | `false` | Master switch for all email (see [Due-Date Extensions & Notifications](#due-date-extensions--notifications)). Leave `false` for local dev with no mail server — every send is logged at `DEBUG` instead. |
| `SMTP_HOST` | *(empty)* | Mail server hostname. Required if `NOTIFICATIONS_ENABLED=true`. |
| `SMTP_PORT` | `587` | Mail server port (587 = STARTTLS, the standard). |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | *(empty)* | SMTP auth credentials, if your provider requires them. |
| `SMTP_USE_TLS` | `true` | STARTTLS vs. a plain/unencrypted connection (only appropriate for a local/private relay). |
| `SMTP_FROM_EMAIL` | *(empty)* | The `From:` address. Required if `NOTIFICATIONS_ENABLED=true` — most providers reject sends where this doesn't match a verified domain/sender. |
| `ADMIN_NOTIFICATION_EMAILS` | *(empty)* | Comma-separated extra recipients who get every overdue digest and every new-extension-request alert, in addition to Admins/Managers/the Super Admin. |
| `OVERDUE_NOTIFICATION_INTERVAL_HOURS` | `24` | How often the in-process scheduler (see [`backend/scheduler.py`](backend/scheduler.py)) checks for overdue checkouts and sends the digest. Lower it (e.g. to a few minutes) while testing locally if you want to see it fire sooner. On Render's free plan this pauses whenever the instance spins down — see [Deploying on Render's Free Plan](#deploying-on-renders-free-plan). |
| `SYSTEM_TASK_TOKEN` | *(empty, disabled)* | Optional shared secret that lets an external scheduler trigger `POST /api/system/notifications/run` without a real login session — see [`backend/api/system.py`](backend/api/system.py) and [Deploying on Render's Free Plan](#deploying-on-renders-free-plan). |
| `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` | `5` | Wrong-password attempts against **the same account** before it's locked, regardless of which IP they came from. |
| `ACCOUNT_LOCKOUT_DURATION_MINUTES` | `15` | How long that per-account lock lasts once triggered. |

## Roles & Permissions Model

Enforced **on the backend** (`deps.py`'s `require_super_admin` /
`require_privileged_role`, plus extra checks inside individual services) —
**never trust the frontend alone**, since anyone can call the API directly
with a tool like `curl` or Postman, bypassing the UI entirely.

- A JWT is issued at login and must be sent as `Authorization: Bearer
  <token>` on every other request.
- `deps.get_current_user` decodes the JWT **and** re-queries the database
  on every single request, so deactivating/deleting a user takes effect
  immediately instead of waiting for their token to expire naturally.
- A Manager can see/manage every user and audit entry system-wide, exactly
  like an Admin/Super Admin — there is no department-scoping for Managers
  anywhere in this app. Their JWT still carries a `department` claim (it's
  informational, e.g. shown on their own profile), but no backend query
  filters by it any more.
- Resetting another user's password (`POST /users/{id}/reset-password`),
  listing soft-deleted accounts (`GET /users/deleted`), and restoring one
  (`POST /users/{id}/restore`) are gated by `require_super_admin` — despite
  the name, that dependency allows **both** `super_admin` and `admin`, same
  as deleting a user. A Manager cannot do any of these three, even by
  calling the API directly.
- Ad-hoc Outsiders never have a department (they're not tied to one), so
  the Ad-Hoc Directory has always shown the same list to every Manager and
  Admin alike (see `services/outsider_service.py`'s comments if you're
  curious why) — now consistent with every other directory in the app.

### Extension-request permissions

A short summary of who can do what — see
[services/extension_service.py](backend/services/extension_service.py)
for the full rule and its docstrings:

| Action | Who |
|---|---|
| Request more time on **your own** active checkout | Any logged-in User (Staff/Customer/Manager/Admin). |
| Request more time **on behalf of** an Ad-Hoc Individual (Outsider) | Manager / Admin / Super Admin only — Outsiders have no login to do this themselves. |
| Approve/deny a pending request | Manager / Admin / Super Admin. No department-scoping — a Manager can decide any request, same as Admin/Super Admin. |
| Grant an extension **directly**, no request first | Manager / Admin / Super Admin, same unrestricted access as approving. See the "Extend" button in the Custody Ledger drawer. |

### The hardcoded Super Admin

`super_admin` is treated completely differently from every other role. It
is **not** a row in the `users` table — it's a single fixed identity built
entirely from the `SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_PASSWORD`/
`SUPER_ADMIN_NAME` settings (see [Environment Variables
Reference](#environment-variables-reference) and `backend/config.py`'s
docstring). That design is what makes the following true **structurally**,
not just by convention:

- **Exactly one exists, always.** There's no table row to duplicate, and
  `POST /users` explicitly rejects `role: "super_admin"` as a reserved
  value no matter who's provisioning the account (see
  `services/user_service.py`'s `RESERVED_ROLES`) — anyone who needs the
  same privileges gets the `admin` role instead (see below).
- **It can never be deleted.** `DELETE /users/{id}` only ever operates on
  real `users` rows (`services/user_service.py`'s `delete_user()`); there's
  no row for the Super Admin to be deleted from.
- **It never appears anywhere in the UI.** The User Directory, exports,
  and every other listing all come from `SELECT ... FROM users` — a query
  that structurally can never return this identity.
- **Its password lives only in the environment.** `POST
  /auth/update-password` **and** `POST /users/{id}/reset-password` both
  explicitly reject any attempt to target it (see
  `services/auth_service.py`'s `update_password()` and
  `services/user_service.py`'s `reset_user_password()`) — change it by
  editing `SUPER_ADMIN_PASSWORD` and restarting the backend, not through
  the app.
- **Login is checked first, before the database.** `services/
  auth_service.py`'s `login()` compares the submitted identifier/password
  against `SUPER_ADMIN_USERNAME`/a pre-hashed `SUPER_ADMIN_PASSWORD`
  *before* ever querying `users` — see that function's comments for the
  exact flow, including why an empty `SUPER_ADMIN_PASSWORD` fully disables
  this path rather than accepting a blank password.
- **Its JWT is recognized, not re-validated against the database.**
  `deps.get_current_user` normally re-queries `users` on every request so
  deactivating/deleting an account revokes access immediately (see above)
  — there's no row to re-query for the Super Admin, so its token is simply
  trusted until it expires. To revoke Super Admin access immediately,
  rotate `JWT_SECRET_KEY` and/or unset `SUPER_ADMIN_PASSWORD`, then restart
  the backend.

`admin` exists precisely so you're not stuck using this one hardcoded
identity for everyday work: it's a normal, database-backed account with
**every privilege `super_admin` has** (`deps.py`'s `_FULL_ADMIN_ROLES`
groups them together in every permission check), but it can be created,
renamed, given a new password through the app, and soft-deleted like any
other user — see the seeded demo `admin` account in [Demo Login
Credentials](#demo-login-credentials).

## Database & Migrations (Alembic)

`backend/models.py` is the source of truth for table *shape*; Alembic
tracks how to get an existing database from one shape to the next without
losing data.

**Step-by-step workflow** (run these from inside the `backend/` folder, with
your virtualenv/deps installed, and `DATABASE_URL` pointing at a reachable
Postgres instance — e.g. `docker compose up db` first if you're running
Alembic from your host machine rather than inside the container):

```bash
cd backend

# 1. Apply all migrations to create/update every table:
alembic upgrade head
NOTE: if the above refuses to run, prefix with docker compose exec
docker compose exec backend alembic upgrade head

# 2. Whenever you change models.py in the future, generate a new migration
#    by diffing your models against the live database:
alembic revision --autogenerate -m "describe your change here"

# 3. Review the generated file in alembic/versions/ (autogenerate is smart
#    but not perfect -- always read it), then apply it:
alembic upgrade head

# To roll back the most recent migration:
alembic downgrade -1
```

`backend/alembic/env.py` is already wired up: it imports `Base.metadata`
from `models.py` (so autogenerate can see your tables) and pulls the real
`DATABASE_URL` from `backend/config.py`'s `settings` object instead of a
hardcoded connection string.

`init_db()` (`Base.metadata.create_all()`) is still safe to leave enabled
for local development — it only creates tables that don't exist yet and
never alters existing ones, so it won't fight with Alembic. In production,
disable it (`AUTO_INIT_DB=false`) and let `alembic upgrade head` be the
only thing that ever changes your schema.

**Current migrations:**
> **If your database was already migrated with an old 0001–0005 chain**
> (check with `alembic current` — if it shows anything other than
> `0001_baseline_schema`), do **not** just run `alembic upgrade head`;
> Alembic will look for migration files that no longer exist and error
> out. Since your tables already match the new baseline's schema exactly,
> just re-point Alembic's bookkeeping at it instead, without touching any
> table:
> ```bash
> alembic stamp 0001_baseline_schema
> ```
> Fresh installs (empty/nonexistent database) don't need this — just run
> `alembic upgrade head` as normal.

**Going forward, every schema change should be its own NEW migration**
(via `alembic revision --autogenerate -m "description"`) layered on top of
`0001_baseline_schema.py` — don't keep hand-editing the baseline itself
once any real data exists anywhere.

**When you add a new column to `models.py`, always write a migration for
it** (either by hand or via `alembic revision --autogenerate`) — don't
rely on `create_all()` alone, since it will never alter an *existing*
table that's missing a new column.

## Full API Reference

Full interactive docs (auto-generated from the code, with a "Try it out"
button for every endpoint) are always available at `/docs` (Swagger UI)
once the backend is running. This table is the high-level map:

| Method & Path | Who | Purpose |
|---|---|---|
| `POST /auth/login` | anyone | Exchange email/username + password for a JWT. Rate-limited by IP; also enforces per-account lockout after repeated failures. |
| `GET /auth/me` | logged in | "Who am I?" — fresh profile data for the "My Profile" window. |
| `POST /auth/update-password` | self or Super Admin/Admin | Change a password (self-service requires the current password; a Super Admin resetting someone else's does not). |
| `GET /assets` | logged in | List asset pools. TRUE server-side pagination + search — `?limit=&offset=&search=` (searches pool name). |
| `POST /assets` | Super Admin / Admin | Create a new pool. |
| `GET /assets/{id}/details` | logged in | Full pool detail: stock breakdown, active checkouts, isolated units. |
| `PUT /assets/{id}/quantity` | Super Admin / Admin | Adjust total capacity. |
| `DELETE /assets/{id}` | Super Admin / Admin | Soft-delete a pool. |
| `POST /assets/{id}/exception` | Super Admin / Admin | Flag a serial as under repair/stolen. |
| `POST /assets/{id}/exception/{eid}/recall` | Super Admin / Admin | Return an isolated unit to service. |
| `POST /assets/{id}/checkin` | Super Admin / Admin | Reconcile newly-found stock. |
| `POST /assets/{id}/checkout_advanced` | Super Admin / Admin / Manager | Dispatch units to a Staff member, a linked Customer account, or an ad-hoc Outsider. |
| `POST /assets/import` | Super Admin / Admin | Bulk-create pools from a CSV (max 5 MiB). |
| `GET /users` | Super Admin / Admin / Manager | Directory listing (both Managers and Admins see the entire directory — no department-scoping). TRUE server-side pagination + search — `?limit=&offset=&search=` (searches name, email, role, department, department_role). |
| `POST /users` | Super Admin / Admin / Manager | Provision a new login. |
| `GET /users/me/items` | logged in | Self-service: my own checked-out items. |
| `GET /users/me/items/export` | logged in | Self-service download of the above as `?format=csv` or `?format=pdf`. |
| `GET /users/{id}/items` | Super Admin / Admin / Manager | Someone else's custody ledger (unrestricted for Managers too). |
| `GET /users/{id}/items/export` | Super Admin / Admin / Manager | Download one specific user's custody ledger (CSV/PDF). |
| `GET /users/export` | Super Admin / Admin / Manager | Bulk download of every active checkout across every user, system-wide, for both roles (CSV/PDF). |
| `DELETE /users/{id}` | Super Admin / Admin | Soft-delete an account. |
| `POST /users/{id}/reset-password` | Super Admin / Admin | "Forgot password" recovery: set a brand-new password for another user's account, no current password required. |
| `GET /users/deleted` | Super Admin / Admin | List soft-deleted accounts. TRUE server-side pagination + search — `?limit=&offset=&search=`, same fields as `GET /users`. |
| `POST /users/{id}/restore` | Super Admin / Admin | Undo a soft-delete: re-enables login and returns the account to the User Directory. |
| `GET /outsiders` | Super Admin / Admin / Manager | Ad-Hoc directory listing. TRUE server-side pagination + search — `?limit=&offset=&search=` (searches name, contact details, company). |
| `GET /outsiders/{id}/items` | Super Admin / Admin / Manager | An outsider's custody ledger. |
| `GET /outsiders/{id}/items/export` | Super Admin / Admin / Manager | Download one specific outsider's custody ledger (CSV/PDF). |
| `GET /outsiders/export` | Super Admin / Admin / Manager | Bulk download of every active checkout across every ad-hoc individual (CSV/PDF). |
| `POST /checkouts/{id}/return` | Super Admin / Admin / Manager | Process a (partial or full) return. |
| `GET /checkouts/overdue` | Super Admin / Admin / Manager | Dashboard alert feed of overdue checkouts, system-wide for both roles (no department-scoping). |
| `POST /checkouts/{id}/extension-requests` | logged in | Request more time on your own active checkout (or, if Manager/Admin/Super Admin, on behalf of an Ad-Hoc Individual). Creates a **pending** request — does not change the due date by itself. |
| `GET /checkouts/extension-requests` | Super Admin / Admin / Manager | List extension requests — `?status=pending\|approved\|denied&limit=&offset=` (Managers see every request, same as Admin/Super Admin). |
| `POST /checkouts/extension-requests/{id}/decision` | Super Admin / Admin / Manager | Approve or deny a pending request — `{approve, override_due_date?, note?}`. Approving is what actually updates the checkout's due date. |
| `POST /checkouts/{id}/extend` | Super Admin / Admin / Manager | Grant more time **directly** — `{new_due_date, reason?}` — no request/decision round trip. Used by the Custody Ledger drawer's "Extend" button. |
| `GET /audit-logs` | Super Admin / Admin / Manager | TRUE server-side paginated audit ledger — `?limit=&offset=` (no search param; see [Feature Tour](#feature-tour)). |
| `POST /audit-logs/export` | Super Admin / Admin / Manager | Enqueue a background export job — `?format=csv` (default) or `?format=pdf`, plus optional `?start_date=&end_date=`. Returns `{task_id, status}` immediately; does not return the file. |
| `GET /audit-logs/export/{task_id}/status` | Super Admin / Admin / Manager | Poll a job's progress — `{state, ready, error?}`. |
| `GET /audit-logs/export/{task_id}/download` | Super Admin / Admin / Manager | Download the finished file once `status` reports `SUCCESS` (409 if not ready yet, 404 if the task_id is unknown/expired). |
| `GET /health` | anyone | Trivial liveness check for Docker/orchestrators. |

**Every export endpoint** accepts `?format=csv` or `?format=pdf` and
responds with a real file download (`Content-Disposition: attachment`) —
you can test any of them straight from `/docs`, or with `curl -O -J` and
a bearer token.

## File & Function Reference

Route handlers in `api/*.py` are intentionally thin — they just parse the
request and call the matching function in `services/*.py`, which is where
the actual logic lives. Use this section as a map when you need to find
"where does X happen?" without grepping the whole repo.

### Backend — App Core

#### `backend/main.py`
- `on_startup()` — FastAPI startup hook; runs `init_db()`/`seed_db()` if
  their `AUTO_*` settings flags are enabled.
- `custom_openapi()` — customizes the generated OpenAPI schema (used by
  `/docs`).
- `health_check()` — `GET /health`.
- Also where the middleware stack (`RateLimitMiddleware`,
  `RequestContextMiddleware`, `CORSMiddleware`, `SecurityHeadersMiddleware`)
  and all API routers are registered — if you add a new `api/*.py` file,
  you register its router here.

#### `backend/config.py`
- `class Settings` — the single source of truth for every environment
  variable, loaded once into a shared `settings` object every other module
  imports (`from config import settings`).
- `Settings.cors_origin_list` — splits the comma-separated `CORS_ORIGINS`
  string into a Python list.
- `Settings.is_production` — `True` when `ENVIRONMENT=production`.
- `Settings._enforce_prod_jwt_secret()` — validator that **refuses to
  start** if running in production with a placeholder/weak
  `JWT_SECRET_KEY`.
- `Settings._enforce_prod_super_admin_password()` — same idea, for
  `SUPER_ADMIN_PASSWORD` (the hardcoded Super Admin's password — see
  Roles & Permissions Model).

#### `backend/database.py`
- `init_db()` — `Base.metadata.create_all()`; creates any tables that
  don't exist yet.
- `get_db()` — FastAPI dependency that yields a SQLAlchemy `Session` and
  always closes it afterwards.
- `seed_db()` — inserts demo accounts/asset pools **only if the database
  is empty**. Note there's no `super_admin` row seeded here — that
  identity is hardcoded via environment variables (see Roles &
  Permissions Model); the seeded top-privilege demo account is `admin`.

#### `backend/models.py`
- `utc_now()` — the one place "the current time" is generated app-wide,
  always timezone-aware UTC (never plain `datetime.now()`).
- `class AssetType` — an inventory pool (name, total_quantity,
  custom_fields).
- `class AssetException` — a single serial number pulled out of
  circulation (repair/lost/stolen).
- `class AuditLog` — an append-only record of every meaningful action.
- `class User` — a login account (Admin/Manager/Staff/Customer — never
  `super_admin`, which is reserved for the hardcoded root identity and is
  never a database row), including `failed_login_attempts`/`locked_until`
  for brute-force lockout.
- `class Outsider` — an ad-hoc external person with no login, who can
  still have custody of assets.
- `class AssetCheckout` — one dispatch of N units to a User or Outsider,
  with a due date and return tracking.
- `class ExtensionRequest` — a request to push out an `AssetCheckout`'s
  due date (`pending` → `approved`/`denied`), recording who asked, why,
  who decided it, and what was actually granted. See [Due-Date Extensions
  & Notifications](#due-date-extensions--notifications).

#### `backend/security.py`
- `hash_password(plain_password)` — Argon2id hash of a plaintext password.
- `verify_password(plain_password, hashed_password)` — constant-time
  comparison against a stored hash.
- `validate_password_strength(password)` — enforces the password
  complexity policy; raises with a specific reason on failure.
- `create_access_token(user)` — issues a signed JWT for a logged-in user.
- `decode_access_token(token)` — verifies signature + expiry and returns
  the token's claims.
- `SUPER_ADMIN_ID` / `SUPER_ADMIN_ROLE` — constants identifying the
  hardcoded Super Admin's JWT `sub`/`role` claims.
- `super_admin_password_hash()` / `SUPER_ADMIN_PASSWORD_HASH` — hashes
  `settings.SUPER_ADMIN_PASSWORD` once at startup (`None` if unset).
- `super_admin_principal()` — a `User`-shaped stand-in for the Super Admin
  so `create_access_token()` can issue it a token like any other account.

#### `backend/deps.py`
- `get_current_user(...)` — FastAPI dependency: decodes the bearer token
  AND re-queries the DB to confirm the account is still active/not
  deleted.
- `require_super_admin(user)` — dependency that 403s unless `role` is
  `super_admin` or `admin` (see `_FULL_ADMIN_ROLES`).
- `require_privileged_role(user)` — dependency that 403s unless `role` is
  `super_admin`, `admin`, or `manager`.
- Also where the hardcoded Super Admin's JWT is recognized and exempted
  from the database re-query every other account gets (see Roles &
  Permissions Model).

#### `backend/logging_config.py`
- `class RequestIdLogFilter` — attaches the current request's correlation
  ID to every log record.
- `class JsonFormatter` — renders each log record as one JSON line.
- `class TextFormatter` — renders each log record as one human-readable
  line (for local dev).
- `configure_logging(settings)` — wires the root logger up with the
  above, called once at startup.

### Backend — Middleware (`backend/middleware/`)

- **`request_context.py`** — `class RequestContextMiddleware`
  assigns/reuses an `X-Request-ID` per request, stores it for the logger,
  echoes it back on the response.
- **`rate_limit.py`** — `class RateLimitMiddleware`, an in-memory sliding-
  window limiter applied only to `POST /auth/login`.
- **`security_headers.py`** — `class SecurityHeadersMiddleware` stamps
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and a
  restrictive `Permissions-Policy` onto every response.

### Backend — API Routes (`backend/api/`)

- **`api/auth.py`** — `login`, `get_my_profile`, `update_password`.
- **`api/assets.py`** — `create_asset_type`, `list_assets`,
  `get_asset_details`, `update_asset_quantity`, `delete_asset_type`,
  `flag_asset_exception`, `recall_asset_exception`, `checkin_asset`,
  `checkout_advanced`, `import_assets_from_csv`.
- **`api/users.py`** — `create_user`, `get_users`, `get_deleted_users`,
  `get_my_assigned_items`, `export_my_assigned_items`,
  `export_all_users`, `get_user_assigned_items`,
  `export_user_assigned_items`, `delete_user`, `reset_user_password`,
  `restore_user`.
- **`api/outsiders.py`** — `get_outsiders`, `export_all_outsiders`,
  `get_outsider_assigned_items`, `export_outsider_assigned_items`.
- **`api/checkouts.py`** — `return_checkout`, `get_overdue_checkouts`,
  `request_extension`, `get_extension_requests`,
  `decide_extension_request`, `extend_checkout`.
- **`api/audit.py`** — `get_audit_logs`, `export_audit_logs`.

### Backend — Services (`backend/services/`) — the business logic layer

- **`services/asset_service.py`** — `create_asset_type`, `list_assets`
  (now accepts a `search` param, narrowing to pool name — see
  `services/search_utils.py`), `get_asset_details`, `update_asset_quantity`,
  `delete_asset_type`, `flag_asset_exception`, `recall_asset_exception`,
  `checkin_asset`, `checkout_advanced`, `import_assets_from_csv`.
  `MAX_CSV_UPLOAD_BYTES` caps upload size.
- **`services/auth_service.py`** — `login(db, req)` checks the hardcoded
  Super Admin identifier/password FIRST, before ever querying `users`;
  otherwise verifies credentials against the database, enforces
  per-account lockout, and issues a JWT. `_DUMMY_PASSWORD_HASH` keeps the
  "no such account" response timing-consistent with "wrong password";
  `get_profile(db, current_user)` backs `GET /auth/me` (rehydrated
  straight from JWT claims for the Super Admin, since it has no row to
  query); `update_password` changes a password and clears any lockout as a
  side effect, and rejects any attempt to target the Super Admin (whose
  password only lives in `SUPER_ADMIN_PASSWORD`).
- **`services/checkout_service.py`** — `return_checkout` processes a
  partial/full return (and records who the equipment came from in the
  audit entry); `list_overdue_checkouts` backs the overdue alert feed.
- **`services/extension_service.py`** — `create_extension_request`
  (self-service, or Manager/Admin logging one on behalf of an Outsider);
  `list_extension_requests` (system-wide for both Managers and Admins — no
  department-scoping);
  `decide_extension_request` (approve/deny — the only path that changes
  `AssetCheckout.due_date` via the request workflow); `extend_checkout_directly`
  (Manager/Admin/Super Admin grants time immediately, no request first —
  backs the Custody Ledger drawer's "Extend" button). `_notify()` enqueues
  every email this file sends via `tasks.send_email_task` instead of
  calling `notification_service.send_email()` inline, so a slow/
  unreachable SMTP server can never hold an API request open — see [Due-
  Date Extensions & Notifications](#due-date-extensions--notifications).
- **`services/notification_service.py`** — `send_email(to, subject,
  body)`, the ONE place `smtplib` is touched anywhere in this codebase.
  Fail-soft by design (never raises — logs a warning and returns `False`
  instead) and a no-op that just logs at `DEBUG` when
  `NOTIFICATIONS_ENABLED=false`.
- **`services/user_service.py`** — `_derive_username` turns an email's
  local-part into a unique username, steering clear of the reserved Super
  Admin username too; `RESERVED_ROLES` blocks `role: "super_admin"` from
  ever being assigned to a database-backed account; `create_user`,
  `list_users` (now accepts a `search` param, narrowing across name/
  email/role/department/department_role), `get_my_assigned_items`,
  `get_user_assigned_items`, `delete_user` (which also rejects the Super
  Admin's sentinel id); the export trio `export_my_assigned_items`,
  `export_user_assigned_items`, `export_all_users_items`;
  `reset_user_password` (Super Admin/Admin sets a locked-out user's new
  password directly — no current password needed — and clears lockout
  state, same as a self-service change); `list_deleted_users` (mirrors
  `list_users` but scoped to `is_deleted == True`, for the Restore panel);
  `restore_user` (reverses `delete_user`, re-enabling login — no email/
  username collision handling needed since `create_user`/
  `_derive_username` already check across soft-deleted rows too).
- **`services/outsider_service.py`** — `list_outsiders` (now accepts a
  `search` param, narrowing across name/contact_details/company),
  `get_outsider_assigned_items`; the export pair
  `export_outsider_assigned_items`, `export_all_outsiders_items`.
- **`services/audit_service.py`** — `get_audit_logs` (TRUE server-side
  paginated listing); `_filtered_audit_logs_query` (shared filter logic);
  `export_audit_logs_csv` (streamed); `export_audit_logs_pdf`.
- **`services/search_utils.py`** — `apply_search_filter(query, search,
  columns)`, the shared helper behind the `search` param on `GET /assets`,
  `GET /users`, and `GET /outsiders`: an OR-across-columns, case-
  insensitive `ILIKE`, with `_escape_like()` neutralizing literal `%`/`_`
  characters in what someone typed so they aren't misread as SQL wildcards.
- **`services/export_service.py`** — `csv_safe_cell` (neutralizes
  formula-injection payloads), `build_csv_bytes`, `build_pdf_bytes` — the
  shared CSV/PDF machinery every exporter in the app uses.
- **`services/stock.py`** — `recalculate_asset_stock(db, asset)`, the
  single shared formula for `Available = Total − Outbound − Isolated`,
  called after every mutation that could change it.

### Backend — In-Process Background Jobs (`backend/jobs.py`, `backend/scheduler.py`, `backend/tasks/`)

Everything below runs as a background **thread**, inside the SAME process
serving HTTP requests — no separate worker container, no broker. See
`jobs.py`'s module docstring for the full reasoning (short version: a
separate Celery+Redis worker doesn't fit Render's, or most platforms',
free tier — see [Deploying on Render's Free Plan](#deploying-on-renders-free-plan)).

- **`jobs.py`** — `submit(fn, ...)` runs a function on a shared
  `ThreadPoolExecutor` and returns a `job_id` immediately; `get_status`/
  `get_result` poll it, mirroring just enough of Celery's `AsyncResult`
  API that `api/audit.py` barely had to change. `run_async(fn, ...)` is
  the fire-and-forget variant (no `job_id`, used for emails). All state
  lives in a plain in-memory dict — see the module docstring for what
  that does and doesn't cost you.
- **`scheduler.py`** — a single daemon thread, started from
  `main.py`'s startup event, that calls `send_overdue_notifications()`
  every `OVERDUE_NOTIFICATION_INTERVAL_HOURS`. Replaces Celery Beat.
- **`tasks/export_tasks.py`** — `generate_audit_export(...)`, builds one
  audit-ledger CSV/PDF export file and returns it as a small dict
  (base64-encoded file bytes) that `jobs.py` holds in memory until
  `GET /api/audit-logs/export/{task_id}/download` reads it back out.
- **`tasks/notification_tasks.py`** — `send_email_task(to, subject,
  body)`, a thin, generic wrapper around
  `notification_service.send_email()` that runs on the background thread
  pool instead of inline in an API request — this is what
  `services/extension_service.py`'s `_notify()` submits via
  `jobs.run_async()`, and the reason the "Request Extension"/"Extension
  Requests" UI no longer hangs waiting on a slow SMTP server (see
  [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications)).
  `send_overdue_notifications()` is what `scheduler.py` calls on a timer
  (and what `POST /api/system/notifications/run` — see `api/system.py` —
  triggers manually): reminds each overdue checkout's own holder, plus
  one combined system-wide digest for every Manager and Admin/Super
  Admin + `ADMIN_NOTIFICATION_EMAILS` (Managers have no
  department-scoping, so they get the exact same full list Admins do).

### Backend — Schemas (`backend/schemas/`)

Pure Pydantic request/response models, no logic:
- **`schemas/auth.py`** — `LoginRequest`, `PasswordUpdateRequest`
  (enforces password strength via a `field_validator`).
- **`schemas/assets.py`** — asset/checkout request bodies, including the
  server-side due-date min/max check.
- **`schemas/users.py`** — `UserCreateRequest` (also enforces password
  strength); `UserPasswordResetRequest` (the admin-reset body — just
  `new_password`, same strength `field_validator`, no `current_password`
  since the whole point is not needing the old one).
- **`schemas/checkouts.py`** — `ReturnRequest`; `ExtensionRequestCreate`
  (self-service or on-behalf-of-Outsider request body);
  `ExtensionDecisionRequest` (approve/deny, with an optional
  `override_due_date`/`note`); `DirectExtensionRequest` (the "Extend"
  button's request body — same shape as `ExtensionRequestCreate`, but
  skips the request/approval workflow entirely).

### Frontend — Core (`frontend/js/`)

- **`js/api.js`** — `apiRequest(path, options)`, the ONE function that
  calls `fetch()`; attaches the JWT header, parses JSON, throws a
  normalized `Error` on failure. `formatErrorDetail(detail)` turns
  FastAPI's `detail` field (string OR validation-error array) into one
  readable message.
- **`js/auth.js`** — `parseJwt`, `getSession`, `logout`, `login`,
  `currentPageName`, `checkAccess`, `redirectByUserRole`,
  `startIdleWatchdog` (auto-logout after inactivity).
- **`js/dashboard.js`** — `refreshDashboard()` calls every component's
  `load*()` function together.
- **`js/main.js`** — `wireDelegatedEvents()` (one `click` listener on
  `document`, dispatched by `data-action` attributes),
  `wireTableControls()`, `wireCsvDragAndDrop(fileInput, form)`.
- **`js/ui.js`** — shared, framework-free UI helpers:
  `openModal`/`closeModal` (toggle `hidden`/`flex` together — see the
  in-code comment if you're ever tempted to "simplify" this), `escapeHtml`
  (the ONE function standing between server data and DOM injection —
  used everywhere a server value is rendered), `switchTab`, `toggleRoute`,
  `toggleCapacityEdit`, `statusBadge`. Two small pagination toolkits live
  here side by side: the CLIENT-side one (`tableState`, `registerRenderer`,
  `filterAndPaginate`, `renderPaginationBar`, `setSearch`, `setPerPage`,
  `changePage`) — now used only by My Items, which filters/pages an
  already-downloaded array in memory — and the SERVER-side one
  (`debounce`, `renderServerPaginationBar`) shared by the Asset/User/
  Outsider directories and the Audit Ledger, each of which keeps its own
  small `{ page, perPage, search, total }` state object in its own
  component file and re-fetches from the API on every change instead.

### Frontend — Components (`frontend/js/components/`) — one file per feature area

- **`assets.js`** — `loadAssets` (TRUE server-side search + pagination —
  `assetsState` + `setAssetsSearch`/`setAssetsPerPage`/`changeAssetsPage`,
  the same pattern as `audit.js`'s `auditState`, extended here), due-date
  bounds helpers, `openDispatchModal`, `submitDispatchForm` (the "Assign
  To > Linked Customer Account" route sends `assignee_type: "user"` +
  the selected `role="customer"` user's real `user_id` — same shape as
  the "Staff Member" route — rather than fabricating a new `Outsider`
  row, so a dispatch to a customer actually links to their account),
  `openPropsModal`, `recallException`, `saveCapacity`,
  `submitExceptionForm`, `submitCreatePoolForm`, `submitCsvImportForm`,
  `deleteAssetPool` (Super Admin only — wired both to a row-level "Delete"
  button on the Asset Inventory table and to a "Delete Asset Pool" button
  in the Properties Hub modal itself; the backend endpoint already
  existed, this just added the missing UI to reach it).
- **`audit.js`** — `loadAuditLogs` (TRUE server-side pagination —
  `auditState` + `changeAuditPage`/`setAuditPerPage`, sharing
  `js/ui.js`'s `renderServerPaginationBar()` with `assets.js`/`users.js`/
  `outsiders.js` below), `exportAuditLogs(format)` (CSV or PDF).
- **`custody.js`** — `getCurrentCustodyEntity()` (lets other modules know
  which user/outsider's ledger is open), `openCustodyModal`,
  `processReturn`, selection/bulk-return helpers. Each item row's "Extend"
  button is wired to `extensions.js` (below) via `main.js`'s
  `data-action="open-direct-extend"`.
- **`exports.js`** — `exportMyItems`, `exportCustodyItems`,
  `exportAllUsers`, `exportAllOutsiders` — all built on one shared
  `downloadExport()` helper that reads the real filename off the
  response's `Content-Disposition` header.
- **`extensions.js`** — three parts of the due-date extension feature:
  1) `openExtensionRequestModal`/`submitExtensionRequestForm` — the
  self-service "Request Extension" modal (`staff.html`/`customer.html`);
  2) `loadExtensionRequests`/`decideExtensionRequest` — the Admin/Manager
  review panel (Approve/Deny); 3) `openDirectExtendModal`/
  `submitDirectExtendForm` — the Custody Ledger drawer's "Extend" button
  (Admin/Manager only, no request/approval step). See [Due-Date
  Extensions & Notifications](#due-date-extensions--notifications).
- **`myitems.js`** — `loadMyItems`/`renderMyItemsTable`, the
  Staff/Customer self-service view. Each row's "Request Extension" button
  opens `extensions.js`'s self-service modal.
- **`outsiders.js`** — `loadOutsiders` (TRUE server-side search +
  pagination — `outsidersState` + `setOutsidersSearch`/
  `setOutsidersPerPage`/`changeOutsidersPage`).
- **`overdue.js`** — `loadOverdueAlerts`.
- **`profile.js`** — `openProfileModal`, `submitChangePasswordForm`,
  `setProfileFormMessage`.
- **`users.js`** — `loadUsers` (TRUE server-side search + pagination —
  `usersState` + `setUsersSearch`/`setUsersPerPage`/`changeUsersPage`;
  also separately re-fetches an unfiltered roster to populate both the
  Dispatch drawer's "Assign To > Staff Member" dropdown (`#staffSelect`,
  every user) AND its "Linked Customer Account" dropdown (`#customerSelect`,
  narrowed to `role="customer"` users only) — that list must never be
  narrowed by whatever's currently typed into the User Directory's search
  box), `deleteProfile`, `submitCreateUserForm`; `openResetPasswordModal`/
  `submitResetPasswordForm` (Super Admin/Admin-only "forgot password"
  recovery — remembers which account via `pendingResetPasswordUserId`,
  same pattern as `extensions.js`'s direct-extend modal); `loadDeletedUsers`
  (own `deletedUsersState` + `setDeletedUsersSearch`/
  `setDeletedUsersPerPage`/`changeDeletedUsersPage`, mirroring `usersState`
  but against `GET /users/deleted`), `restoreUser`.

## Making Changes Safely (A Guide For Beginners)

This section is a walkthrough of the **safe pattern** for common changes,
so you don't have to reverse-engineer it from scratch. Follow the same
shape every time and you'll rarely break something unrelated.

### The golden rule: work outward from the database, one layer at a time

```
1. models.py         (does the DATA need to change shape?)
2. alembic migration  (if #1 changed: write the migration)
3. schemas/*.py        (does the REQUEST/RESPONSE shape need to change?)
4. services/*.py        (where the actual logic goes)
5. api/*.py               (the thin route that calls #4)
6. frontend/js/*.js         (call the new/changed endpoint)
7. frontend/*.html           (add any new buttons/fields/markup)
```

You don't always need all 7 steps — a pure UI tweak might only touch #7,
a backend-only bugfix might only touch #4. But when you DO need several of
them, do them **in this order**, and test after each layer if you can
(see [Testing Your Changes](#testing-your-changes)).

### Example: "Add a `notes` field to Asset Pools"

1. **`models.py`** — add `notes = Column(String, nullable=True)` to
   `AssetType`.
2. **Migration** — `alembic revision --autogenerate -m "add notes to asset_types"`,
   then check the generated file actually looks right before running
   `alembic upgrade head`.
3. **`schemas/assets.py`** — add `notes: Optional[str] = None` to whichever
   request model creates/updates a pool.
4. **`services/asset_service.py`** — read `payload.notes` and set it on
   the `AssetType` row in `create_asset_type()`/`update_asset_quantity()`
   (or wherever makes sense).
5. **`api/assets.py`** — usually needs NO change at all, since routes just
   pass the whole validated `payload` object through to the service.
6. **`frontend/js/components/assets.js`** — include `notes` in whatever
   object `submitCreatePoolForm()` sends, and display it in
   `renderAssetsTable()`/`openPropsModal()`.
7. **`frontend/admin.html`** — add an `<input>` for it in the "Register
   New Inventory Pool" form.

### Example: "Add a brand-new endpoint" (e.g. `GET /assets/{id}/history`)

1. Decide which `services/*.py` file it belongs in (asset-related →
   `asset_service.py`) and write the actual query/logic function there
   first, in isolation — you can test it directly in a Python shell before
   wiring up any HTTP plumbing at all (see
   [Testing Your Changes](#testing-your-changes)).
2. Add a thin route in the matching `api/*.py` file that calls it — copy
   the shape of a neighboring route in the same file (same
   `Depends(get_db)`, same `Depends(require_...)` pattern).
3. If it needs a new Pydantic model for its request body, add it to the
   matching `schemas/*.py` file.
4. Register nothing extra in `main.py` — routers are already wired up
   there; a new function on an existing router's file is picked up
   automatically the next time the app restarts.
5. Add the frontend call + UI last, once you've confirmed the endpoint
   works from `/docs` directly.

### Example: "Add a new page/dashboard tab"

The four dashboard HTML files (`admin.html`, `manager.html`, `staff.html`,
`customer.html`) are the pattern to copy from — they all share the same
navbar/modal/tab structure. To add a new tab:
1. Copy an existing `<section>` block that's structured like a tab panel,
   give it a new `id`, and add a matching nav button with
   `data-action="switch-tab"` (see `ui.js`'s `switchTab()`).
2. Create a new file under `frontend/js/components/` for its logic,
   following the `load*()`/`render*Table()` naming pattern every other
   component uses.
3. Wire its `load*()` function into `js/dashboard.js`'s
   `refreshDashboard()` so it populates automatically when the dashboard
   loads.
4. Wire up any buttons via `data-action` attributes and a matching entry
   in `main.js`'s `CLICK_ACTIONS` map — **don't** add individual
   `addEventListener()` calls scattered around; the whole app uses one
   central dispatch table on purpose, so anyone can find every click
   handler in one place.

### Things to be careful about

- **Never hard-delete a `User`, `AssetType`, or anything else with
  history attached.** Always soft-delete (`is_deleted = True`,
  `deleted_at = utc_now()`) — a hard delete either crashes on a foreign
  key or silently destroys the audit trail for anything that referenced
  it.
- **Never call `datetime.datetime.utcnow()` or `datetime.datetime.now()`
  directly.** Always `from models import utc_now` and call that instead —
  see `models.py`'s big comment block on timezone handling for why this
  matters.
- **Never build a raw SQL string with an f-string/`%`/`.format()`.** This
  codebase is 100% SQLAlchemy ORM queries on purpose — that's what makes
  SQL injection "not applicable" as a risk here. Keep it that way.
- **Never insert a server-supplied string into the DOM without
  `escapeHtml()`** (`js/ui.js`). Every existing `render*Table()` function
  already does this — copy that pattern for any new one.
- **Never log a password**, hashed or plain, anywhere — not even at
  `DEBUG` level.
- **Recalculate stock after any mutation that could change it.** If you
  add a new way for units to enter/leave a pool (a new checkout type, a
  new exception type, etc.), call
  `services/stock.py -> recalculate_asset_stock(db, asset)` afterwards,
  same as every existing mutation does.
- **Write an audit log entry for anything a Super Admin/Manager does that
  changes state.** Copy the `db.add(models.AuditLog(...))` pattern from a
  neighboring function in the same service file.

## Testing Your Changes

A small pytest smoke suite lives in `backend/tests/` and runs
automatically in CI (`.github/workflows/ci.yml`) against a real Postgres
service container — see [Continuous Integration](#continuous-integration)
below. It's intentionally not a full test suite for every business rule;
here's how to verify a change works more thoroughly in the meantime.

### Fastest option: Swagger UI (`/docs`)

With the full stack running via `docker compose up`, open
`http://localhost:8000/docs` — the SAME origin serving the app now, no
separate proxy or port (see [Tech Stack](#tech-stack)). Every
endpoint is listed with a "Try it out" button, a place to paste your JWT
(click the padlock icon, or the green "Authorize" button at the top), and
a live response. This is the quickest way to confirm a backend change
works without touching the frontend at all.

### Manual functional testing with a throwaway SQLite database

If you want to test a chain of API calls end-to-end without touching your
real Postgres data, you can point the app at a temporary SQLite file
instead — no Docker, no Postgres needed:

```bash
cd backend
pip install httpx --break-system-packages   # only needed for this test client

DATABASE_URL="sqlite:////tmp/test.db" python3 << 'EOF'
import os
os.environ["DATABASE_URL"] = "sqlite:////tmp/test.db"
import database
database.init_db()
database.seed_db()

from fastapi.testclient import TestClient
from main import app
client = TestClient(app)

# Log in as the demo Admin account (see "Demo Login Credentials" above)
r = client.post("/api/auth/login", json={"identifier": "r.adeyemi@corp.io", "password": "Admin123!"})
token = r.json()["token"]
headers = {"Authorization": f"Bearer {token}"}

# Try whatever you just built, e.g.:
r = client.get("/api/assets", headers=headers)
print(r.status_code, r.json())
EOF

rm -f /tmp/test.db   # clean up when you're done
```

This is exactly the pattern used to verify the exports, audit trail, and
account-lockout features described in this README while they were built —
copy/adapt the snippet above for whatever endpoint you're changing. See
[`backend/tests/test_smoke.py`](backend/tests/test_smoke.py) for a real,
runnable version of this same pattern.

### Frontend

Since there's no build step, just refresh the page in your browser after
saving a `.js`/`.html` file. Open your browser's DevTools Console while
testing — `js/api.js` throws a real `Error` (with the backend's message)
on any failed request, which will show up there if something goes wrong
silently in the UI.

## Continuous Integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push
and pull request against `main`, with four jobs:

- **lint** — `ruff check .` against the backend.
- **test** — spins up a real, throwaway Postgres service container, runs
  `alembic upgrade head` against it, then runs the pytest smoke suite in
  [`backend/tests/`](backend/tests/) with the full app wired up against
  that database.
- **docker** — builds the production image from the root `Dockerfile`
  (the same one Render builds from `render.yaml`), to catch a broken
  `Dockerfile`/`COPY` path/missing dependency before a real deploy ever
  attempts it.
- **validate-deploy-configs** — lints `render.yaml` and
  `docker-compose.yml` as YAML, and runs `docker compose config` to
  confirm every `${VARIABLE}` reference in `docker-compose.yml` actually
  resolves against `.env.example`.

None of these jobs deploy anything — this workflow's only job is to catch
problems before they ever reach a real deploy. Wire up Render's own
GitHub integration (auto-deploy on push) or a separate deploy step if you
want pushes to `main` to also trigger a live deployment.

## Security Model

A quick reference of what's already handled, so you don't accidentally
"fix" something that's already correct, or skip something that matters
when adding a new feature.

- ✅ Passwords hashed with **Argon2id** (`pwdlib`), never stored/logged in
  plain text.
- ✅ Password complexity policy enforced server-side on every
  *set*-password path (never on login, which must always be allowed to
  fail generically).
- ✅ **JWT** sessions; the backend re-validates the user's
  `is_active`/`is_deleted` state on *every* request (instant revocation,
  not "wait for the token to expire").
- ✅ Startup **refuses to boot in production** with a placeholder/short
  JWT secret (`config.py`).
- ✅ Role-based access control enforced **server-side** for every
  privileged action (never just hidden in the UI).
- ✅ **Soft deletes** everywhere a row is referenced by audit/history, so
  the audit trail can never be silently destroyed by a delete.
- ✅ SQL injection: not applicable — 100% SQLAlchemy ORM queries, zero raw
  SQL string interpolation anywhere in the codebase.
- ✅ Stored XSS: the frontend consistently escapes every server-supplied
  string before inserting it into the DOM (`escapeHtml()` in `ui.js`).
- ✅ CSV/"formula injection" protection on every exported CSV
  (`services/export_service.py -> csv_safe_cell()`).
- ✅ Secrets (`JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, `SMTP_PASSWORD`) live
  only in a git-ignored `.env`, never hardcoded in source or
  `docker-compose.yml`.
- ✅ Email notifications never block a request or task on a slow/
  unreachable mail server — every send is fail-soft (logs a warning,
  returns `False`, never raises — `services/notification_service.py`) AND
  runs on a background thread rather than inline in the request/response
  cycle (`tasks.send_email_task`, submitted via `jobs.run_async()` — see
  [Due-Date Extensions & Notifications](#due-date-extensions--notifications)).
- ✅ Row-level locking (`with_for_update()`) on the checkout path prevents
  a race condition from overselling a pool under concurrent requests.
- ✅ Pagination limits (`limit`/`offset` + hard `MAX_LIMIT` caps) on every
  listing endpoint, preventing unbounded-response-size abuse.
- ✅ Login rate-limited **per IP** (`middleware/rate_limit.py`) **and**
  **per account** (`User.failed_login_attempts`/`locked_until`) — the two
  work together: IP limiting slows a single source hammering many
  accounts, account lockout stops one account being brute-forced from
  many sources.
- ✅ Login is timing-safe against username enumeration — a nonexistent
  identifier still runs a full password-hash comparison (against a
  precomputed dummy hash) so it can't be distinguished, by response time
  alone, from "wrong password for a real account" (`auth_service.py`'s
  `_DUMMY_PASSWORD_HASH`).
- ✅ Standard defensive response headers (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`).
- ✅ A real **`Content-Security-Policy`**, tuned against the frontend's
  actual CDN/script/font usage rather than a generic copy-pasted policy —
  see `backend/middleware/security_headers.py`'s `Content-Security-Policy`
  header and its accompanying comment for the reasoning behind each
  directive.
- ✅ Exactly one **Super Admin**, hardcoded via environment variables
  rather than a database row — it can never be created, edited, or deleted
  through the app, and never appears in the User Directory or any other
  listing. See [Roles & Permissions Model](#roles--permissions-model).
- ✅ App container runs as an unprivileged user, not root.
- ✅ Structured, correlated logging for every login attempt and password
  change (never the password itself).
- ✅ Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) can be fully
  disabled via `ENABLE_API_DOCS=false` — FastAPI never generates/serves
  the schema at all when disabled, not just a hidden UI. Defaults to
  `true` for local-dev convenience; **set `false`** in Render/cloud
  (`render.yaml` already does this). See `config.py`'s `ENABLE_API_DOCS`
  docstring and the Environment Variables Reference above.

**Known trade-off, left as-is:** the frontend stores the JWT in
`localStorage` (see `frontend/js/auth.js`'s comment) rather than an
`httpOnly` cookie. This is a common, reasonable choice for a same-origin
SPA without a token-refresh flow, but it does mean a successful XSS
elsewhere in the page could read the token. Moving to `httpOnly` cookies
would need CSRF-token handling added at the same time — a bigger
architectural change, listed in
[Suggested Future Features](#suggested-future-features) instead of
changed silently.

## Running In Production

A checklist before you deploy this anywhere real:

- [ ] `ENVIRONMENT=production` in your `.env` (this alone makes the
      backend **refuse to start** if `JWT_SECRET_KEY` is still a
      placeholder or too short — see `config.py`).
- [ ] Generate and set a real `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, and
      `SUPER_ADMIN_PASSWORD` (this alone makes the backend **refuse to
      start** if `SUPER_ADMIN_PASSWORD` is still empty/placeholder or too
      short — see `config.py`).
- [ ] `AUTO_INIT_DB=false` and `AUTO_SEED_DEMO_DATA=false` — run
      `alembic upgrade head` as its own explicit deploy step instead, and
      never create the public demo accounts against a real database.
- [ ] Set `CORS_ORIGINS` to your real frontend domain(s) only — or leave
      it empty if the frontend and API stay on this same combined service
      (the default; see [Tech Stack](#tech-stack)).
- [ ] Decide on email: leave `NOTIFICATIONS_ENABLED=false` if you don't
      want extension-request/overdue-checkout emails yet, or set it to
      `true` and fill in real `SMTP_HOST`/`SMTP_FROM_EMAIL`/etc. — see
      [Due-Date Extensions & Notifications](#due-date-extensions--notifications).
      If you're on Render's free plan, also read [Deploying on Render's
      Free Plan](#deploying-on-renders-free-plan)'s note on the daily
      digest pausing whenever the service spins down.
- [ ] Set `ENABLE_API_DOCS=false` (`render.yaml` already does this).
      Confirm it worked by requesting `/docs` and `/openapi.json` on your
      deployed URL — both should return a plain `404`, not a docs page or
      schema.
- [ ] Put TLS termination in front of this service — a managed platform's
      own edge/load balancer (Render does this automatically), or a cloud
      load balancer / cert-manager setup if you're self-hosting. Once
      `ENVIRONMENT=production` is set, `middleware/security_headers.py`
      automatically adds a `Strict-Transport-Security` header — safe to
      leave on even before TLS is in front of it, since browsers simply
      ignore that header over plain HTTP.
- [ ] Drop `--reload` from the `uvicorn` command if you've customized the
      Dockerfile's `CMD` (the root `Dockerfile` already doesn't use it —
      only `docker-compose.yml`'s local-dev override does). A free-tier
      instance has limited RAM/CPU and no horizontal scaling anyway, so a
      single `uvicorn` worker process is the right fit; only add
      `--workers N` if you've upgraded to a paid plan with more resources
      AND still run this as one instance (see `jobs.py`'s docstring for
      why multiple *instances* need a different job-queue design first).
- [ ] Consider swapping the in-memory login rate limiter (and
      `jobs.py`'s in-memory job store) for a shared, Redis-backed
      alternative if you ever run more than one instance of this service
      — see `middleware/rate_limit.py`'s and `jobs.py`'s docstrings for
      exactly what breaks (silently, per-instance) if you don't.
- [ ] Review and tighten `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` /
      `ACCOUNT_LOCKOUT_DURATION_MINUTES` and
      `LOGIN_RATE_LIMIT_MAX`/`LOGIN_RATE_LIMIT_WINDOW_SECONDS` for your
      actual expected traffic pattern.
- [ ] Set up a real backup schedule for the Postgres volume — this
      project doesn't include one, since backup strategy is very
      deployment-specific (managed Postgres providers usually handle this
      for you automatically; note Render's *free* Postgres specifically
      expires after 30 days with no automatic renewal — see [Deploying on
      Render's Free Plan](#deploying-on-renders-free-plan)).

## Suggested Future Features

Small, well-scoped follow-ups if you want to keep extending this project:

- **A deeper automated test suite** beyond the smoke tests in
  `backend/tests/` (see [Testing Your Changes](#testing-your-changes) and
  [Continuous Integration](#continuous-integration)) — real coverage of
  each role's permission boundaries, the checkout/return/extension state
  machine, and the CSV/PDF export formats.
- **Redis-backed rate limiting and job storage** (e.g. `slowapi`/
  `fastapi-limiter` for login attempts, a real queue for `jobs.py`) if
  this service is ever scaled to multiple instances, so all instances
  share one counter/job store instead of each enforcing its own
  independently — see `middleware/rate_limit.py`'s and `jobs.py`'s
  docstrings for why this only matters once you're past a single
  instance.
- **A `deleted_by` column** recording which admin performed a given
  soft-delete (good first Alembic migration exercise) — `restore_user()`
  itself (undoing a soft-delete) already shipped; see [Directories](#directories-super-admin--manager).
- **Case-insensitive login** (`func.lower()` comparison + a matching
  unique index) so `T.Okafor@corp.io` and `t.okafor@corp.io` are treated
  as the same account.
- **Per-user notification preferences** — email is currently all-or-nothing
  via `NOTIFICATIONS_ENABLED` (see [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications)); a `users` table
  column for "email me my own overdue reminders: yes/no" would be a small,
  well-scoped follow-up.
- **A reminder before something goes overdue**, not just after — the
  overdue digest (`tasks.send_overdue_notifications`) only fires once a
  due date has already passed; a "due in N days" heads-up would need a
  second scheduled query alongside it.
- **`httpOnly` cookie sessions + CSRF tokens**, replacing the current
  `localStorage`-based JWT storage (see the trade-off noted in
  [Security Model](#security-model)).
- **OpenTelemetry tracing** — the request-ID/structured-logging
  foundation (`middleware/request_context.py`, `logging_config.py`) is a
  natural stepping stone toward full distributed tracing if this app ever
  calls out to other services.

## Troubleshooting

- **The app takes 30–60 seconds to respond after being idle for a
  while (Render free plan only)** — expected. Render's free web
  services spin down after 15 minutes with no inbound traffic and take a
  moment to spin back up on the next request. See [Deploying on Render's
  Free Plan](#deploying-on-renders-free-plan) for two ways to keep it warm
  if that matters for your use case.
- **The overdue-checkout digest email never seems to fire** — first
  check `NOTIFICATIONS_ENABLED=true` and your `SMTP_*` settings are
  actually correct (see [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications)). If those are
  right, and you're on Render's free plan, the in-process scheduler
  thread (`backend/scheduler.py`) pauses whenever the service spins down
  from inactivity — see [Deploying on Render's Free
  Plan](#deploying-on-renders-free-plan) for the fix (an uptime pinger,
  or an external scheduler hitting `POST /api/system/notifications/run`).
- **"Refusing to start: ENVIRONMENT=production but JWT_SECRET_KEY is
  still a placeholder..."** — expected and intentional (see `config.py`).
  Generate a real secret: `python3 -c "import secrets;
  print(secrets.token_hex(32))"` and set it in `.env`.
- **Backend crash-loops on `docker compose up`** — check `db`'s
  healthcheck passed first (`docker compose logs db`); the backend waits
  for it (`depends_on: condition: service_healthy`) but a bad
  `POSTGRES_PASSWORD`/`DATABASE_URL` mismatch will still fail the
  connection.
- **Getting `429 Too Many Requests` while testing login repeatedly** —
  that's the IP-based rate limiter (`LOGIN_RATE_LIMIT_MAX` /
  `LOGIN_RATE_LIMIT_WINDOW_SECONDS` in `.env`); wait out the window or
  raise the limit locally while developing.
- **Getting `423 Locked` when logging in** — that's the per-account
  lockout (`ACCOUNT_LOCKOUT_MAX_ATTEMPTS` consecutive wrong passwords
  against that specific account). Wait out
  `ACCOUNT_LOCKOUT_DURATION_MINUTES`, or have a Super Admin reset that
  account's password (`POST /auth/update-password`), which clears the
  lockout immediately.
- **Logs look like dense JSON and are hard to read locally** — set
  `LOG_FORMAT=text` in your `.env` for a more human-friendly single-line
  format while developing.
- **"Current password is incorrect" when changing your own password** —
  the "My Profile" window's Change Password form always requires your
  actual current password. If you (as a Super Admin) instead need to
  reset a *different*, e.g. locked-out, user's password, that flow
  doesn't require it — see `services/auth_service.py -> update_password()`.
- **A modal isn't centered / looks misplaced** — every modal's wrapper
  toggles `hidden`/`flex` together in `js/ui.js`'s `openModal()`/
  `closeModal()`; if you're building a new modal, copy an existing one's
  markup (`fixed inset-0 ... hidden items-center justify-center`) exactly
  rather than writing it from scratch, so it inherits this behavior.
- **An export button downloads an empty/near-empty file** — both Managers
  and Super Admins export the full, unscoped dataset now, so an empty file
  almost always means there's genuinely nothing active to export yet
  (e.g. no active checkouts) rather than a permissions/scope issue.
- **"Request Extension" (or Approve/Deny, or "Extend") feels slow to
  close/clear** — every extension-related action submits an email to a
  background thread instead of sending it inline (see [Due-Date
  Extensions & Notifications](#due-date-extensions--notifications)), so
  the API itself should return almost instantly regardless of SMTP
  speed. If it's still slow, check the app's own logs for a
  `background_task_failed` entry (see `jobs.py -> run_async()`) — a
  submission that fails is caught and logged (never raised — see
  `services/extension_service.py -> _notify()`), but a genuine hang here
  would point to something else worth investigating separately.
- **Extension emails never arrive, even with `NOTIFICATIONS_ENABLED=true`**
  — check the app's logs for the actual `smtplib` error (search for
  `tasks.send_email_task` / `notification_service`). Confirm `SMTP_HOST`
  and `SMTP_FROM_EMAIL` are both set — `notification_service.send_email()`
  logs a `WARNING` (not `DEBUG`) and refuses to send if either is missing
  while `NOTIFICATIONS_ENABLED=true`.
- **`403 Forbidden` on "Extend" or on approving/denying an extension
  request** — Managers no longer have any department-scoping here (they
  can act on any checkout, same as Admin/Super Admin), so a 403 here means
  something else: double-check the request isn't already `approved`/
  `denied` (only `pending` requests can be decided), or that you're
  actually logged in as a Manager/Admin/Super Admin and not a Staff/
  Customer account. See [Extension-request permissions](#extension-request-permissions).
