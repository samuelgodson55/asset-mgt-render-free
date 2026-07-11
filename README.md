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
7. [Deploying Across Environments (nginx Reverse Proxy)](#deploying-across-environments-nginx-reverse-proxy)
8. [Running Without Docker (Local Dev)](#running-without-docker-local-dev)
9. [Environment Variables Reference](#environment-variables-reference)
10. [Roles & Permissions Model](#roles--permissions-model)
11. [Database & Migrations (Alembic)](#database--migrations-alembic)
12. [Backups](#backups)
13. [Full API Reference](#full-api-reference)
14. [File & Function Reference](#file--function-reference)
15. [Making Changes Safely (A Guide For Beginners)](#making-changes-safely-a-guide-for-beginners)
16. [Testing Your Changes](#testing-your-changes)
17. [Security Model](#security-model)
18. [Running In Production](#running-in-production)
19. [Safely Updating An Existing Production Deployment (CI/CD)](#safely-updating-an-existing-production-deployment-cicd)
20. [Suggested Future Features](#suggested-future-features)
21. [Troubleshooting](#troubleshooting)

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
- **Due Soon alerts** — "a reminder before something goes overdue" — a
  second, amber banner right above it lists every active checkout due
  within `DUE_SOON_REMINDER_DAYS` that hasn't gone overdue yet,
  soonest-first. See [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications) below for the full
  picture (it also shows up on Staff/Customer's own My Items and in the
  Custody Ledger).

### Due-Date Extensions & Notifications

Everyone gets a piece of this, scoped by role. Four related pieces:

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
- **A reminder before something goes overdue (everyone)** — the same
  `DUE_SOON_REMINDER_DAYS` window (2 days by default — see [Environment
  Variables Reference](#environment-variables-reference)) drives three
  matching, proactive nudges, all surfaced BEFORE a checkout's due date
  actually passes:
  - **Admin / Manager** — an amber **"Due Soon"** dashboard banner (right
    above the red "Overdue" one) lists every active checkout system-wide
    that's due within the window, soonest first — see
    `backend/services/checkout_service.py`'s `list_due_soon_checkouts()`
    and `GET /checkouts/due-soon`.
  - **Staff / Customer** — a row in "My Items" that's due within the
    window shows an amber **"Due Soon"** badge instead of the usual blue
    "On Loan" one, right on their own self-service dashboard.
  - **Admin / Manager, inside the Custody Ledger** — the same due-soon
    rows are also highlighted amber (with a "· Due Soon" note next to the
    due date) when reviewing anyone's custody, whether a User or an
    Ad-Hoc Outsider.

  All three read the exact same `due_soon`/`days_until_due` computation
  (`models.is_due_soon()`, shared by every service module above) so the
  "what counts as due soon" definition can never drift between them.

**Email notifications** (`backend/services/notification_service.py` +
`backend/tasks/notification_tasks.py`) — plain SMTP, no vendor SDK, off by
default (`NOTIFICATIONS_ENABLED=false` — see [Environment Variables
Reference](#environment-variables-reference)). Three kinds go out once
enabled and configured:
1. **Extension-request lifecycle** — every Manager/Admin (system-wide) is
   emailed the moment a new request comes in; the checkout's holder is
   emailed back once their request is approved or denied, or once a
   Manager/Admin/Super Admin grants an extension directly (no request
   needed for that email to go out).
2. **Daily overdue digest** — a scheduled Celery Beat job
   (`send_overdue_notifications`, every `OVERDUE_NOTIFICATION_INTERVAL_HOURS`
   — 24 by default) emails each overdue checkout's own holder a reminder,
   plus one combined system-wide summary digest to every Manager and
   Admin/Super Admin (Managers have no department-scoping, so they get
   the exact same full list Admins do).
3. **Daily due-soon reminder digest** — the proactive counterpart to #2:
   a second scheduled Celery Beat job (`send_due_soon_reminders`, every
   `DUE_SOON_NOTIFICATION_INTERVAL_HOURS` — 24 by default) sends the
   *same shape* of individual-holder-reminder + Manager/Admin-digest
   email, just for checkouts due within `DUE_SOON_REMINDER_DAYS` instead
   of ones that have already passed their due date — "a reminder before
   something goes overdue," by email as well as on the dashboard.

Every one of these emails is **enqueued on the background `worker`
container** (`tasks.send_email_task`, `backend/celery_app.py`) rather than
sent inline in the request/response cycle — a slow or unreachable SMTP
server can add several seconds of latency (`smtplib.SMTP(...,
timeout=10)`), and doing that inline used to make the "Request Extension"
modal and the "Extension Requests" panel both feel like they hung before
clearing. The API now commits the database change and returns
immediately; the actual email goes out a moment later, out-of-band —
exactly the same producer/consumer split already used for audit-ledger
exports (see [Tech Stack](#tech-stack)). If `NOTIFICATIONS_ENABLED=false`
(the default), every notification is simply logged at `DEBUG` level
instead of sent — nothing here requires a mail server to develop or demo
the app locally.

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
- Exportable as CSV or PDF. Export generation runs on a background
  Celery worker (see [Tech Stack](#tech-stack)) rather than inline in the
  request — clicking "Export" enqueues a job and polls it to completion
  before downloading, so a wide date range never risks tying up the API
  or timing out the browser. Both a Manager and a Super Admin see/export
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
  (PDF generation for exports), Celery + Redis (background workers for
  audit-ledger exports, extension-request/overdue/due-soon-checkout email
  notifications, and two scheduled Celery Beat digest jobs — see below).
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
  Served by an nginx reverse proxy built from
  [`nginx/Dockerfile`](nginx/Dockerfile) (see
  [Deploying Across Environments](#deploying-across-environments-nginx-reverse-proxy)).
- **Infra:** Docker Compose, 5 services: `db` (Postgres), `redis`
  (Celery broker/result backend, not exposed to the host), `backend`
  (FastAPI/uvicorn, not exposed to the host), `worker` (Celery — builds
  audit-ledger CSV/PDF exports, sends every email notification, and runs
  the embedded Celery Beat scheduler for the daily overdue AND due-soon
  digests, all out-of-band; not exposed to the host), `frontend` (nginx —
  serves the static site AND reverse-proxies `/api/*` to `backend`, the
  only publicly-exposed service).

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
├── .github/
│   └── workflows/
│       ├── ci.yml                     # Lint/syntax sanity checks on every
│       │                                # push + PR (no full test suite yet --
│       │                                # see "Suggested Future Features")
│       ├── deploy-docker-compose.yml   # Push-to-deploy over SSH for a
│       │                                # self-hosted VPS -- see "Safely
│       │                                # Updating An Existing Production
│       │                                # Deployment" section above
│       └── deploy-render.yml            # Migrate-then-deploy for the Render
│                                          # Free-plan target (works around
│                                          # Free's lack of preDeployCommand)
│
├── docker-compose.yml        # 5 services: db, redis, backend, worker, frontend
├── render.yaml                # Render Blueprint (free-plan shape: db + redis +
│                                # one combined web service) -- see "Deploying
│                                # Across Environments" section above
├── Dockerfile.render          # Builds the single combined image (backend +
│                                # static frontend + optional embedded Celery
│                                # worker) that render.yaml's web service uses --
│                                # a Render-Free-plan-specific ALTERNATIVE to
│                                # backend/Dockerfile, not a replacement for it
├── render-start.sh             # CMD for Dockerfile.render -- optionally launches
│                                # the embedded Celery worker/beat, then execs uvicorn
├── .env.example               # Copy this to .env and fill in real secrets
├── .gitignore                  # Keeps .env (and other junk) out of git
├── .dockerignore               # Keeps .env (and other junk) out of the build context too
│
├── nginx/
│   ├── Dockerfile                  # Builds the frontend/reverse-proxy image
│   ├── default.conf.template        # nginx config template -- see "Deploying
│   │                                  # Across Environments" section above
│   └── docker-entrypoint.d/
│       └── 15-detect-resolver-ip.sh  # Auto-detects RESOLVER_IP from
│                                       # /etc/resolv.conf if it isn't set
│                                       # (must stay non-executable -- see
│                                       # its own header comment for why)
│
├── backend/
│   ├── main.py                    # FastAPI app: middleware, startup, routers
│   ├── config.py                   # Pydantic Settings -- all env vars, one place
│   ├── database.py                  # SQLAlchemy engine/session + init_db()/seed_db()
│   ├── models.py                     # SQLAlchemy ORM table definitions
│   ├── security.py                    # Password hashing, password policy, JWT
│   ├── deps.py                         # get_current_user / role-gate dependencies
│   ├── logging_config.py                # Structured (JSON) logging setup
│   ├── celery_app.py                      # Celery app: Redis broker/result backend
│   │                                        # for async exports -- shared by `backend`
│   │                                        # (producer) and `worker` (consumer)
│   ├── requirements.txt                  # Python dependencies
│   ├── Dockerfile                         # Backend container build (also used,
│   │                                        # unmodified, by the `worker` service --
│   │                                        # see docker-compose.yml)
│   │
│   ├── tasks/                     # Celery tasks -- run on the `worker` container
│   │   ├── export_tasks.py          # generate_audit_export(): builds the CSV/PDF
│   │   │                              # off the request/response cycle
│   │   └── notification_tasks.py     # send_email_task() (generic async email --
│   │                                    # used by extension_service.py too) +
│   │                                    # send_overdue_notifications() +
│   │                                    # send_due_soon_reminders() (Celery
│   │                                    # Beat digests -- see celery_app.py)
│   │
│   ├── middleware/                # ASGI middleware, one concern per file
│   │   ├── request_context.py       # Request Correlation ID (X-Request-ID)
│   │   ├── rate_limit.py             # Per-IP login rate limiting
│   │   └── security_headers.py       # Standard defensive response headers
│   │
│   ├── api/                       # Thin FastAPI routers (HTTP layer only)
│   │   ├── auth.py, assets.py, users.py, outsiders.py, checkouts.py, audit.py
│   │
│   ├── schemas/                   # Pydantic request/response models
│   │   ├── auth.py, assets.py, users.py, checkouts.py
│   │
│   ├── services/                  # All business logic / DB queries live here
│   │   ├── auth_service.py           # Login, password changes, account lockout
│   │   ├── asset_service.py           # Asset pool CRUD, checkout, CSV import
│   │   ├── user_service.py             # User directory, self/bulk exports
│   │   ├── outsider_service.py          # Ad-hoc directory, exports
│   │   ├── checkout_service.py           # Returns, overdue + due-soon feeds
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
└── frontend/
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
            ├── due-soon.js            # "Due Soon" alert banner (reminder before overdue)
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
nginx (frontend container)   <-- reverse proxy: strips the "/api" prefix and
                                  forwards to the backend container. See
                                  nginx/default.conf.template and the
                                  "Deploying Across Environments" section.
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

# 2. Build and start everything (Postgres, Redis, backend, worker, frontend)
docker compose up --build

# 3. Open the app -- everything is served from ONE origin now, via the
#    nginx reverse proxy (see the next section for how/why):
#    App (login page):  http://localhost:8080
#    API docs:           http://localhost:8080/docs
```

Leave the terminal running to see live logs from all three containers.
Press `Ctrl+C` to stop everything, or run `docker compose up -d --build`
to start it in the background instead.

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

## Deploying Across Environments (nginx Reverse Proxy)

This app is designed to run, **unmodified**, across three tiers: your local
Docker Compose setup, a Render staging environment, and a real cloud
environment (AWS/GCP/Azure/etc.). The piece that makes that possible is the
`frontend` service — it's no longer a bare static-file server, it's an
**nginx reverse proxy** built from [`nginx/Dockerfile`](nginx/Dockerfile).

**The core idea:** the browser never talks to the FastAPI backend directly
and never needs to know its hostname. `frontend/js/api.js` calls a single
constant, relative path — `/api` — for every request. nginx, sitting in
front of both the static site and the backend, quietly forwards
(“proxies”) anything under `/api/*` to wherever the real backend actually
lives in that environment, **unchanged** — `backend/main.py` mounts every
API router under an `/api` prefix itself (e.g. `/api/auth`, `/api/assets`),
so there's no path-rewriting for nginx to do anymore. See
[`nginx/default.conf.template`](nginx/default.conf.template) for the
fully-commented config that does this.

```
Browser  --GET /api/assets-->  nginx (frontend container)  --GET /api/assets-->  FastAPI (backend container)
Browser  <--------------------------- same response --------------------------------------
```

Because that config is a **template** (`envsubst`'d by nginx's own
entrypoint script every time the container starts), the exact same Docker
image works in all three tiers — only these environment variables change:

| Variable | What it controls | Local Docker Compose | Render / Cloud |
|---|---|---|---|
| `PORT` | Port nginx listens on inside its container | `80` | Whatever port that platform injects |
| `BACKEND_HOST` | Hostname nginx proxies `/api/*` to | `backend` (the Compose service name) | Your backend service's real hostname on that platform |
| `BACKEND_PORT` | Port on that host | `8000` | Whatever port your backend actually listens on there |
| `RESOLVER_IP` | Internal DNS server nginx uses to re-resolve `BACKEND_HOST` on every request (so a backend redeploy never leaves nginx pointed at a stale IP) | `127.0.0.11` (Docker's built-in DNS) | Auto-detected at boot from `/etc/resolv.conf` if left unset — see [`nginx/docker-entrypoint.d/15-detect-resolver-ip.sh`](nginx/docker-entrypoint.d/15-detect-resolver-ip.sh) |

`PORT`, `BACKEND_HOST`, and `BACKEND_PORT` all have sensible defaults baked
into `nginx/Dockerfile`. `RESOLVER_IP` deliberately does **not** — instead of
hardcoding a guess that could go stale on some future platform, it's
auto-detected at container boot (see the table above and
[`nginx/docker-entrypoint.d/15-detect-resolver-ip.sh`](nginx/docker-entrypoint.d/15-detect-resolver-ip.sh)
for why). All four are already wired up as `environment:` overrides on the
`frontend` service in `docker-compose.yml`, sourced from `.env` (see
`.env.example`) — Compose explicitly pins `RESOLVER_IP=127.0.0.11` there, so
nothing changes for local dev.

### Local Docker Compose
Nothing to configure — `docker compose up --build` already sets
`BACKEND_HOST=backend`/`BACKEND_PORT=8000`, matching the `backend` service
in the same compose file.

### Render

**This project is configured to run entirely on Render's free plan** — no
credit card, no paid services. See [`render.yaml`](render.yaml)'s
top-of-file comment for the full reasoning; the short version is: Render's
Free instance type only exists for Web Services, Postgres, and Key Value
(Redis) — Private Services and Background Workers aren't available on the
Free plan at any price, which ruled out the original private-backend +
private-worker + public-frontend split. Instead, [`Dockerfile.render`](Dockerfile.render)
builds ONE image containing the FastAPI backend, the static frontend
(served directly by FastAPI — see `backend/main.py`'s `SERVE_FRONTEND`
flag), and an **embedded** Celery worker/beat process (see
[`render-start.sh`](render-start.sh) and `RUN_EMBEDDED_WORKER`), so the
whole app fits on a single free Web Service.

- [ ] Push this repo (including `render.yaml` and `Dockerfile.render` at
      the repo root) to your Git provider.
- [ ] In the Render Dashboard: **New** → **Blueprint** → connect this repo.
      Render reads `render.yaml` and shows you the three resources it's
      about to create — `snipeit-lite-db` (free Postgres),
      `snipeit-lite-redis` (free Key Value), and `snipeit-lite-web` (free
      Web Service) — then provisions them on **Deploy Blueprint**.
- [ ] That's it — no manual hostname copy-pasting. `JWT_SECRET_KEY` and
      `SUPER_ADMIN_PASSWORD` are auto-generated (`generateValue: true`);
      find the generated Super Admin password in the Render dashboard's
      Environment tab if you need to log in as that account.
- [ ] Verify: load `snipeit-lite-web`'s public Render URL (expect a ~1
      minute cold start the first time, or after any 15-minute idle period
      — see the free-plan limitations below), log in, and confirm `/api/*`
      calls succeed in your browser's Network tab.

**Know the free-plan tradeoffs before you rely on this for anything real**
(all documented in more detail in `render.yaml`'s comments):
- The web service **spins down after 15 minutes idle** and takes about a
  minute to spin back up on the next request.
- **750 free instance-hours/month**, shared across every free web service
  in your Render *workspace* — fine for just this one service running
  continuously, but a second always-on free web service in the same
  workspace could push you over the limit.
- Only **one** free Postgres and **one** free Key Value instance are
  allowed per workspace.
- The free Postgres database **expires 30 days after creation** (14-day
  grace period to upgrade before Render deletes it) — there's no way
  around this on the Free plan.
- The free Key Value (Redis) instance is **in-memory only** — a restart
  (including every spin-down/spin-up cycle) wipes any queued-but-not-yet-
  processed export job. Worst case: a queued export is lost and someone
  re-clicks "export".
- The embedded Celery worker/beat process lives and dies with the web
  service's spin-down/redeploy cycle, so scheduled notification digests
  (see `celery_app.py`'s `beat_schedule`) may fire somewhat irregularly
  around a spin-down. Fine for personal/demo use; not a guarantee for
  anything time-sensitive.
- The embedded-worker approach does **not** scale past one instance — if
  you ever move off the Free plan and turn on horizontal scaling, every
  instance would start its own worker/beat and fire every scheduled task
  once per instance (duplicate emails). See the next section.

<details>
<summary>Need a paid, multi-service, horizontally-scalable deployment instead? Click to expand.</summary>

Once you're off the Free plan, you can split this back into the original
three-service shape (a private backend, a private Celery worker, and a
public nginx frontend) for proper horizontal scaling and no shared-process
tradeoffs:

- [ ] Create a **Web Service** (`plan: starter` or higher) built from
      `nginx/Dockerfile` (build context = repo root) for the frontend/
      proxy — the only piece that needs a public URL.
- [ ] Create a **Private Service** (`plan: starter` or higher) built from
      `backend/Dockerfile` for the FastAPI backend, with
      `SERVE_FRONTEND=false` (the default) and `ENVIRONMENT=production`.
      Bind to `0.0.0.0` on Render's injected `$PORT`.
- [ ] Create a **Background Worker** (`plan: starter` or higher), same
      image/build context as the backend, with `dockerCommand: celery -A
      celery_app worker -B --loglevel=info --concurrency=2` and
      `RUN_EMBEDDED_WORKER=false` (it's its own dedicated process now, not
      embedded — see `render-start.sh`'s caveats for why this matters once
      you scale the web service to more than one instance).
- [ ] Create a paid **Key Value** and **Postgres** instance (Free instances
      don't support persistence/backups/expiration-free storage — see
      `render.yaml`'s comments).
- [ ] Wire `DATABASE_URL`/`REDIS_URL`/`BACKEND_HOST`/`BACKEND_PORT` together
      via `fromDatabase`/`fromService` the same way the free-tier
      `render.yaml` does (or via the dashboard's **Connect → Internal**
      tab if you're not using a Blueprint) — see the "Deploying Across
      Environments" table above for what each variable controls.
- [ ] Confirm every service is in the **same Render region** — private
      networking only works within one region.

</details>


### Cloud (AWS/GCP/Azure/etc.)
Same pattern: deploy the `nginx/Dockerfile` image as your public-facing
service, deploy `backend/Dockerfile` as an internal-only service (e.g.
behind a private load balancer or in the same VPC/private subnet with no
public IP), and set `BACKEND_HOST`/`BACKEND_PORT` to match that
environment's actual internal DNS naming (e.g. an ECS Service Connect name,
a Kubernetes Service DNS name like `backend.default.svc.cluster.local`, or
an internal ALB/NLB hostname). Leave `RESOLVER_IP` unset unless you've
confirmed a specific value your platform needs — it's auto-detected from
`/etc/resolv.conf` at boot otherwise (see
[`nginx/docker-entrypoint.d/15-detect-resolver-ip.sh`](nginx/docker-entrypoint.d/15-detect-resolver-ip.sh)).

### Why the backend is no longer exposed directly
`docker-compose.yml`'s `backend` service no longer publishes port `8000` to
the host. nginx is now the **only** public entry point; it's the sole thing
that reaches the backend, over each environment's private/internal
network. This shrinks the app's public attack surface to one hardened,
well-understood front door, and is exactly the shape you want in Render/
cloud too — never give your database-talking API container a public IP if
a reverse proxy can front it instead.

## Running Without Docker (Local Dev)

If you'd rather run the backend directly with Python (e.g. to use a
debugger, or because Docker isn't available), here's how. You'll still
need a PostgreSQL server running somewhere reachable (Docker is still the
easiest way to get *just* Postgres — see the snippet below).

```bash
# 1. Start ONLY a Postgres container (skip backend/frontend containers)
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

# 4. Run the backend with live-reload
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

For the frontend, since there's no build step, you can serve the
`frontend/` folder with literally any static file server:

```bash
cd frontend
python3 -m http.server 8080
# now open http://localhost:8080
```

**One catch:** `frontend/js/api.js`'s `API_URL` is deliberately a *relative*
path (`/api`) — see [Deploying Across Environments](#deploying-across-environments-nginx-reverse-proxy)
above for why. That only resolves correctly when the frontend is served
*behind the nginx reverse proxy* (which is what `docker compose up`
gives you and does the `/api/*` → backend forwarding). A bare
`python3 -m http.server` has no such proxy, so `/api/*` calls will 404
against its own static file server. For this fully-Docker-free mode,
either:
- run `docker compose up frontend db` alongside the steps above so nginx
  still fronts things (simplest — just skip starting `backend` via
  Compose and run it with `uvicorn` instead, as shown), or
- temporarily hardcode `API_URL` back to `http://localhost:8000` in
  `frontend/js/api.js` while working this way, and revert it before
  committing.

## Environment Variables Reference

All of these live in `.env` (copied from `.env.example`, never committed —
see `.gitignore`) and are read by `backend/config.py` into a single typed
`settings` object that every other backend module imports.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENVIRONMENT` | `development` | `production` enables the startup JWT-secret strength check. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | see `.env.example` | Postgres credentials, shared by the `db` and `backend` services. |
| `DATABASE_URL` | built from the above | Full SQLAlchemy connection string. |
| `REDIS_URL` | `redis://redis:6379/0` | Celery broker **and** result backend, shared by `backend` (producer) and `worker` (consumer) — used for async audit-ledger exports and every email notification (see [Due-Date Extensions & Notifications](#due-date-extensions--notifications)). |
| `EXPORT_RESULT_TTL_SECONDS` | `3600` | How long a finished export job's file bytes stay cached in Redis before expiring. |
| `JWT_SECRET_KEY` | *(required, no insecure default allowed)* | Signs/verifies session tokens. **Must** be a long random string in production. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `JWT_EXPIRY_HOURS` | `12` | How long a login session stays valid. |
| `CORS_ORIGINS` | localhost variants | Comma-separated list of origins allowed to call the API. |
| `AUTO_INIT_DB` | `true` | If true, runs `create_all()` on startup (creates missing tables). Set `false` in production and use Alembic instead. |
| `AUTO_SEED_DEMO_DATA` | `true` | If true, seeds demo accounts/data on an empty DB at startup. Set `false` in production. |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. |
| `LOG_FORMAT` | `json` | `json` (production/log aggregators) or `text` (readable local dev). |
| `LOGIN_RATE_LIMIT_MAX` | `5` | Max `/auth/login` attempts per IP per window before HTTP 429. |
| `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | `60` | The window (in seconds) the above limit applies over. |
| `ENABLE_API_DOCS` | `true` | Whether `/docs`, `/redoc`, `/openapi.json` exist at all. **Set `false` in Render/cloud** — see the Security Model section below. Also read by the frontend/nginx service (see the table below) as a second, independent layer. |
| `SUPER_ADMIN_USERNAME` | `superadmin` | Login identifier for the hardcoded Super Admin (root) account — see [Roles & Permissions Model](#roles--permissions-model). |
| `SUPER_ADMIN_NAME` | `Super Admin` | Display name for that account (shown in the navbar/profile, same as any other user's `name`). |
| `SUPER_ADMIN_PASSWORD` | *(placeholder, must be changed in production)* | Password for the hardcoded Super Admin. Leaving it empty fully disables that login path. **Must** be a real, unique value in production — the backend refuses to start otherwise (same idea as `JWT_SECRET_KEY`). |
| `NOTIFICATIONS_ENABLED` | `false` | Master switch for all email (see [Due-Date Extensions & Notifications](#due-date-extensions--notifications)). Leave `false` for local dev with no mail server — every send is logged at `DEBUG` instead. |
| `SMTP_HOST` | *(empty)* | Mail server hostname. Required if `NOTIFICATIONS_ENABLED=true`. |
| `SMTP_PORT` | `587` | Mail server port (587 = STARTTLS, the standard). |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | *(empty)* | SMTP auth credentials, if your provider requires them. |
| `SMTP_USE_TLS` | `true` | STARTTLS vs. a plain/unencrypted connection (only appropriate for a local/private relay). |
| `SMTP_FROM_EMAIL` | *(empty)* | The `From:` address. Required if `NOTIFICATIONS_ENABLED=true` — most providers reject sends where this doesn't match a verified domain/sender. |
| `ADMIN_NOTIFICATION_EMAILS` | *(empty)* | Comma-separated extra recipients who get every overdue digest, every due-soon reminder digest, and every new-extension-request alert, in addition to Admins/Managers/the Super Admin. |
| `OVERDUE_NOTIFICATION_INTERVAL_HOURS` | `24` | How often the Celery Beat job checks for overdue checkouts and sends the digest. Lower it (e.g. to a few minutes) while testing locally if you want to see it fire sooner. |
| `DUE_SOON_REMINDER_DAYS` | `2` | "A reminder before something goes overdue" — how many days ahead of its `due_date` an active checkout counts as "due soon". Drives the "Due Soon" dashboard banner, the "Due Soon" badge on My Items, AND the due-soon reminder email below, all from this one setting. |
| `DUE_SOON_NOTIFICATION_INTERVAL_HOURS` | `24` | How often the Celery Beat job checks for checkouts about to go overdue and sends the due-soon reminder digest. Same "lower it for local testing" idea as `OVERDUE_NOTIFICATION_INTERVAL_HOURS` above. |
| `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` | `5` | Wrong-password attempts against **the same account** before it's locked, regardless of which IP they came from. |
| `ACCOUNT_LOCKOUT_DURATION_MINUTES` | `15` | How long that per-account lock lasts once triggered. |
| `ENABLE_AUTO_BACKUP` | `true` | Runs a `pg_dump` backup inside this same process (a plain daemon thread — no Celery/Redis dependency) at each hour in `BACKUP_HOURS_UTC`. See [Backups](#backups). |
| `BACKUP_HOURS_UTC` | `3` | Comma-separated hours of day (UTC, each 0–23) the backup runs at — `3` for once a day, `3,15,21` for three times a day. |
| `BACKUP_DIR` | `/app/backups` | Where local backup files + their `index.json` metadata live inside the container. |
| `BACKUP_RETENTION_COUNT` | `7` | How many local backup files to keep before deleting the oldest. Google Drive copies (if enabled) are unaffected. |
| `BACKUP_GDRIVE_ENABLED` | `false` | Uploads every backup to Google Drive right after it's written locally — the only thing that makes a backup survive a Render redeploy/spin-down. |
| `BACKUP_GDRIVE_OAUTH_CLIENT_ID` | *(empty)* | Mode 1 (personal Google account) — printed by `backend/scripts/gdrive_oauth_setup.py`. Takes priority over Mode 2 if both are set. See [Backups](#backups). |
| `BACKUP_GDRIVE_OAUTH_CLIENT_SECRET` | *(empty)* | Mode 1, paired with the above. |
| `BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN` | *(empty)* | Mode 1, paired with the above. |
| `BACKUP_GDRIVE_CREDENTIALS_JSON` | *(empty)* | Mode 2 (Google Workspace) — the raw contents of a service account's JSON key. Only works if `BACKUP_GDRIVE_FOLDER_ID` is a Shared Drive folder. See [Backups](#backups) for the 5-minute setup. |
| `BACKUP_GDRIVE_FOLDER_ID` | *(empty)* | The destination Drive folder's ID (from its URL). Mode 1: any folder in your own Drive. Mode 2: must be inside a Shared Drive, shared with the service account as an Editor. |

The four below are read by the **`frontend`** service (the nginx reverse
proxy), not the backend — see [Deploying Across Environments](#deploying-across-environments-nginx-reverse-proxy).

| Variable | Default | Purpose |
|----------|---------|---------|
| `FRONTEND_PORT` | `80` | Port nginx listens on inside its own container. |
| `BACKEND_HOST` | `backend` | Hostname nginx proxies `/api/*` requests to. |
| `BACKEND_PORT` | `8000` | Port on that host. |
| `RESOLVER_IP` | `127.0.0.11` | Internal DNS server nginx uses to re-resolve `BACKEND_HOST` on every request. |
| `ENABLE_API_DOCS` | `true` | nginx's own copy of the backend's identically-named flag (see the table above) — blocks `/docs`/`/redoc`/`/openapi.json` at the proxy itself, before the request ever reaches the backend, if either copy is `false`. **Set `false` in Render/cloud.** |

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

## Backups

Everything lives in `backend/services/backup_service.py` (the logic),
`backend/api/backup.py` (the `/api/backup/*` routes, Super Admin/Admin
only), and the **System Backups** panel at the bottom of `admin.html`.

**What happens automatically:** once a day at each hour listed in
`BACKUP_HOURS_UTC` (default `3`, i.e. 03:00 UTC — set e.g. `3,15,21` for
three times a day), this app runs `pg_dump` against its own database,
gzip-compresses the result, saves it to `BACKUP_DIR` (default
`/app/backups`), and — if `BACKUP_GDRIVE_ENABLED=true` — uploads that same
file to a Google Drive folder you control. This runs as a plain daemon
thread inside the same `uvicorn` process (see
`backup_service.start_backup_scheduler()`, called from `main.py`'s startup
event) — **not** Celery — so it works whether or not `RUN_EMBEDDED_WORKER`
is enabled, with no Redis dependency.

**Why Google Drive, not just local disk:** on Render's Free plan, this
service's own disk is **ephemeral** — wiped on every restart, every
spin-down/wake-up cycle, and every redeploy. A local backup file is a
convenient "undo the last few minutes" safety net (and is what the
Restore button's dropdown reads from), but it is NOT durable on its own.
Google Drive sync is what actually protects you against a wiped disk.

**Setting up Google Drive sync (~5 minutes, one-time):** there are two auth
modes — pick the one matching your Google account. If both get configured,
Mode 1 (OAuth) takes priority.

**Mode 1 — personal/consumer Google account (a regular Gmail), recommended
for most people running this project:**

A Google Cloud "service account" (Mode 2 below) has **zero Drive storage
quota of its own** — even a folder you personally share with it as
"Editor" doesn't help, since any file it creates there is still *owned by*
the service account and billed against its (nonexistent) quota. That's the
`storageQuotaExceeded` / "Service Accounts do not have storage quota..."
error you'll hit if you try Mode 2 without a Workspace Shared Drive. Mode 1
sidesteps this entirely by uploading as *you* instead, so backups count
against your own normal 15GB quota, same as dragging a file into Drive by
hand.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or reuse one), then enable the **Google Drive API** (**APIs &
   Services → Library**).
2. **APIs & Services → OAuth consent screen** → choose **External** → fill
   in an app name/support email → Save. This is fine to leave in "Testing"
   mode forever for personal use — no Google review/verification needed.
   Under "Test users", add your own Google account's email.
3. **APIs & Services → Credentials → Create Credentials → OAuth client
   ID** → Application type **Desktop app** → Create → **Download JSON**.
4. On your own machine (not inside Docker): `pip install google-auth-oauthlib`,
   then run:
   ```bash
   python backend/scripts/gdrive_oauth_setup.py /path/to/the_downloaded.json
   ```
   A browser opens — log in with the Google account whose Drive storage
   you want backups to live in, and click Allow. The script prints three
   values.
5. In Google Drive, create (or pick) a regular folder for backups (no
   sharing step needed this time) and copy its ID out of the URL:
   `https://drive.google.com/drive/folders/<THIS_PART>`.
6. Set these in `.env` (or Render's Environment tab):
   ```bash
   BACKUP_GDRIVE_ENABLED=true
   BACKUP_GDRIVE_OAUTH_CLIENT_ID=<printed by the script>
   BACKUP_GDRIVE_OAUTH_CLIENT_SECRET=<printed by the script>
   BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN=<printed by the script>
   BACKUP_GDRIVE_FOLDER_ID=<the folder ID from step 5>
   ```
   On Render, the three `BACKUP_GDRIVE_OAUTH_*` values are marked
   `sync: false` in `render.yaml` — paste them directly into the
   Environment tab; they're never committed to git.

**Mode 2 — Google Workspace account with a Shared Drive:**

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or reuse one), then **APIs & Services → Credentials → Create
   Credentials → Service Account**. Enable the **Google Drive API** first
   if it isn't already (**APIs & Services → Library**).
2. Open the new service account → **Keys → Add Key → Create new key →
   JSON**. This downloads a `.json` file — treat it like a password.
3. Create a folder **inside a Shared Drive** (not a regular "My Drive"
   folder — this only works inside a Shared Drive, since that's the one
   place Google bills storage to the drive itself rather than the file's
   creator), right-click → **Share**, and share it with the service
   account's own email address (looks like
   `something@your-project.iam.gserviceaccount.com`, found inside the JSON
   key) as an **Editor**.
4. Copy that folder's ID out of its URL:
   `https://drive.google.com/drive/folders/<THIS_PART>`.
5. Set three environment variables (`.env` locally, or Render's
   Environment tab):
   ```bash
   BACKUP_GDRIVE_ENABLED=true
   BACKUP_GDRIVE_CREDENTIALS_JSON='<paste the entire JSON key file contents as one line>'
   BACKUP_GDRIVE_FOLDER_ID=<the folder ID from step 4>
   ```
   On Render, `BACKUP_GDRIVE_CREDENTIALS_JSON` is marked `sync: false` in
   `render.yaml` — paste the JSON key's contents directly into the
   Environment tab; it's never committed to git.

**Manual backup:** click **Backup Now** in the System Backups panel — runs
`pg_dump` synchronously and returns pass/fail immediately (this app's data
is small/fast enough that a background job isn't needed for this).

**Downloading a backup:** click **Download** next to any local backup to
save its `.sql.gz` file yourself, independent of Google Drive.

**Restoring — two paths, both destructive (replace the whole database):**
- **Restore** next to a backup already listed in the panel (still on this
  container's local disk).
- **Restore from File…** — upload a `.sql.gz` (or plain `.sql`) file
  directly. This is the recovery path once local disk has been wiped
  (e.g. a Render redeploy happened since the last backup): download the
  last good file from your Google Drive backups folder, then upload it
  here.

Both paths require typing `RESTORE` into a confirmation box before
anything runs, and both automatically take one more "pre-restore safety"
backup of whatever's currently in the database immediately before
replacing it — so restoring the wrong file is itself undoable through the
exact same flow.

**Retention:** `BACKUP_RETENTION_COUNT` (default `7`) caps how many local
backup files are kept before the oldest is deleted — this only affects the
local copy; anything already uploaded to Google Drive is untouched by it.

**Required OS package:** `pg_dump`/`psql` are provided by the
`postgresql-client` apt package, not by the `psycopg2-binary` Python
dependency — both `backend/Dockerfile` and `Dockerfile.render` already
install it. If you're running the backend outside Docker (see "Running
Without Docker" above), install it yourself (e.g.
`sudo apt install postgresql-client` on Debian/Ubuntu, `brew install
postgresql` on macOS) or backups/restores will fail with a clear
"pg_dump is not installed" error.

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
| `POST /assets/import` | Super Admin / Admin | Bulk-create pools from a CSV (max 5 MiB, columns `name`, `total_quantity`, optional `department`). |
| `GET /assets/departments` | logged in | Distinct list of departments currently set on any active pool — powers the Asset Inventory Export button's per-department options. |
| `GET /assets/export` | logged in | Download the Asset Inventory table itself (one row per pool) as `?format=csv\|pdf`, optionally narrowed with `?department=` (omit, or pass `all`, for every pool). |
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
| `GET /checkouts/due-soon` | Super Admin / Admin / Manager | Dashboard alert feed of checkouts due within `DUE_SOON_REMINDER_DAYS` but not yet overdue — "a reminder before something goes overdue," system-wide for both roles. |
| `POST /checkouts/{id}/extension-requests` | logged in | Request more time on your own active checkout (or, if Manager/Admin/Super Admin, on behalf of an Ad-Hoc Individual). Creates a **pending** request — does not change the due date by itself. |
| `GET /checkouts/extension-requests` | Super Admin / Admin / Manager | List extension requests — `?status=pending\|approved\|denied&limit=&offset=` (Managers see every request, same as Admin/Super Admin). |
| `POST /checkouts/extension-requests/{id}/decision` | Super Admin / Admin / Manager | Approve or deny a pending request — `{approve, override_due_date?, note?}`. Approving is what actually updates the checkout's due date. |
| `POST /checkouts/{id}/extend` | Super Admin / Admin / Manager | Grant more time **directly** — `{new_due_date, reason?}` — no request/decision round trip. Used by the Custody Ledger drawer's "Extend" button. |
| `GET /audit-logs` | Super Admin / Admin / Manager | TRUE server-side paginated audit ledger — `?limit=&offset=` (no search param; see [Feature Tour](#feature-tour)). |
| `POST /audit-logs/export` | Super Admin / Admin / Manager | Enqueue a background export job — `?format=csv` (default) or `?format=pdf`, plus optional `?start_date=&end_date=`. Returns `{task_id, status}` immediately; does not return the file. |
| `GET /audit-logs/export/{task_id}/status` | Super Admin / Admin / Manager | Poll a job's progress — `{state, ready, error?}`. |
| `GET /audit-logs/export/{task_id}/download` | Super Admin / Admin / Manager | Download the finished file once `status` reports `SUCCESS` (409 if not ready yet, 404 if the task_id is unknown/expired). |
| `GET /backup/status` | Super Admin / Admin | Daily-schedule config, Google Drive on/off, and the most recent backup's metadata. |
| `GET /backup/list` | Super Admin / Admin | Newest-first list of local backup files. |
| `POST /backup/create` | Super Admin / Admin | Run a `pg_dump` backup right now ("Backup Now" button). |
| `GET /backup/download/{filename}` | Super Admin / Admin | Download a previously-created backup file. |
| `DELETE /backup/{filename}` | Super Admin / Admin | Delete a local backup file (does not touch any copy already on Google Drive). |
| `POST /backup/restore/{filename}` | Super Admin / Admin | **Destructive.** Replace the entire database with a backup already on local disk. Takes an automatic pre-restore safety backup first. |
| `POST /backup/restore-upload` | Super Admin / Admin | **Destructive.** Same as above, but from an uploaded `.sql`/`.sql.gz` file — the recovery path once local disk has been wiped. |
| `GET /healthz` | anyone | Trivial liveness check for Docker/orchestrators. |

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
- `health_check()` — `GET /healthz`.
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
- `is_due_soon(due_date)` — "a reminder before something goes overdue":
  the one shared definition of "coming up soon" (still in the future, but
  no further out than `settings.DUE_SOON_REMINDER_DAYS`), reused by
  `services/user_service.py` and `services/outsider_service.py` so a
  User's My Items badge, a User's Custody Ledger highlight, and an Ad-Hoc
  Outsider's Custody Ledger highlight can never disagree about what
  counts as "due soon" (the system-wide "Due Soon" dashboard banner
  applies the identical rule as its own bulk SQL filter — see
  `services/checkout_service.py`'s `list_due_soon_checkouts()`).
- `class AssetType` — an inventory pool (name, total_quantity,
  custom_fields, optional `department` describing which internal team the
  equipment originates from).
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
  `get_due_soon_checkouts`, `request_extension`, `get_extension_requests`,
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
  audit entry); `list_overdue_checkouts` backs the overdue alert feed;
  `list_due_soon_checkouts` (the proactive counterpart — "a reminder
  before something goes overdue") backs the "Due Soon" alert feed,
  mutually exclusive with the overdue one on either side of `now`.
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
  `get_user_assigned_items` (both now stamp each item with a `due_soon`
  flag via `models.is_due_soon()` — "a reminder before something goes
  overdue," surfaced on My Items and the Custody Ledger), `delete_user`
  (which also rejects the Super Admin's sentinel id); the export trio
  `export_my_assigned_items`, `export_user_assigned_items`,
  `export_all_users_items`; `reset_user_password` (Super Admin/Admin sets
  a locked-out user's new password directly — no current password needed
  — and clears lockout state, same as a self-service change);
  `list_deleted_users` (mirrors `list_users` but scoped to
  `is_deleted == True`, for the Restore panel); `restore_user` (reverses
  `delete_user`, re-enabling login — no email/username collision handling
  needed since `create_user`/`_derive_username` already check across
  soft-deleted rows too).
- **`services/outsider_service.py`** — `list_outsiders` (now accepts a
  `search` param, narrowing across name/contact_details/company),
  `get_outsider_assigned_items` (also stamps each item with a `due_soon`
  flag, same `models.is_due_soon()` helper as user_service.py's item
  builders — an Ad-Hoc Outsider's Custody Ledger gets the identical
  "due soon" highlight a User's does); the export pair
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

### Backend — Async Workers (Celery) (`backend/celery_app.py`, `backend/tasks/`)

Two different processes share one Celery app — see `celery_app.py`'s
module docstring for the full producer/consumer split: the `backend`
container only ever enqueues jobs (`.delay(...)`) and returns
immediately; the `worker` container is the only thing that actually runs
them, completely out-of-band from any HTTP request.

- **`celery_app.py`** — the shared `celery_app` instance (Redis as both
  broker and result backend), its serialization/result-TTL config, and
  `beat_schedule` — wires `tasks.send_overdue_notifications` to run every
  `OVERDUE_NOTIFICATION_INTERVAL_HOURS` AND `tasks.send_due_soon_reminders`
  to run every `DUE_SOON_NOTIFICATION_INTERVAL_HOURS`, as two independent
  schedule entries. `-B` embeds Celery Beat directly inside the `worker`
  container's own process (see `docker-compose.yml`'s `worker` service)
  rather than running it as a separate container — correct for one worker
  replica, **not** something to scale to multiple replicas without
  splitting Beat back out (see the in-code comment).
- **`tasks/export_tasks.py`** — `generate_audit_export(...)`, builds one
  audit-ledger CSV/PDF export file and returns it as a small JSON-safe
  dict (base64-encoded file bytes) that Celery stashes in Redis until
  `GET /audit-logs/export/{task_id}/download` reads it back out.
- **`tasks/notification_tasks.py`** — `send_email_task(to, subject,
  body)`, a thin, generic wrapper around
  `notification_service.send_email()` that runs on the `worker` instead
  of inline in an API request — this is what
  `services/extension_service.py`'s `_notify()` enqueues, and the reason
  the "Request Extension"/"Extension Requests" UI no longer hangs waiting
  on a slow SMTP server (see [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications)).
  `send_overdue_notifications()` is the scheduled Celery Beat job:
  reminds each overdue checkout's own holder, plus one combined
  system-wide digest for every Manager and Admin/Super Admin +
  `ADMIN_NOTIFICATION_EMAILS` (Managers have no department-scoping, so
  they get the exact same full list Admins do).
  `send_due_soon_reminders()` — "a reminder before something goes
  overdue" — is its proactive counterpart: the identical shape of
  individual-holder-reminder + Manager/Admin digest, just for checkouts
  due within `DUE_SOON_REMINDER_DAYS` instead of ones already overdue
  (`_due_soon_query()`/`_format_line()` are shared helpers used by both
  jobs).

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
  which user/outsider's ledger is open), `openCustodyModal` (highlights
  any row where the backend's `due_soon` flag is set — amber text + a
  "· Due Soon" note next to the due date), `processReturn`,
  selection/bulk-return helpers. Each item row's "Extend" button is wired
  to `extensions.js` (below) via `main.js`'s
  `data-action="open-direct-extend"`.
- **`due-soon.js`** — `loadDueSoonAlerts`. "A reminder before something
  goes overdue" — the proactive counterpart to `overdue.js` below: same
  shape, same data flow (`GET /checkouts/due-soon` instead of
  `/checkouts/overdue`), just an amber banner instead of a red one since
  nothing has actually gone wrong yet.
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
  Staff/Customer self-service view. A row due within
  `settings.DUE_SOON_REMINDER_DAYS` shows an amber "Due Soon" badge
  instead of the usual blue "On Loan" one (reads the backend's `due_soon`
  flag — see `services/user_service.py`'s `get_my_assigned_items()`).
  Each row's "Request Extension" button opens `extensions.js`'s
  self-service modal.
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

There's no bundled automated test suite in this project yet (see
[Suggested Future Features](#suggested-future-features) for adding one) —
here's how to verify a change works in the meantime.

### Fastest option: Swagger UI (`/docs`)

With the full stack running via `docker compose up`, open
`http://localhost:8080/docs` (proxied through nginx — see
[Deploying Across Environments](#deploying-across-environments-nginx-reverse-proxy)).
If you're running the backend standalone with `uvicorn` (no nginx in
front), it's at `http://localhost:8000/docs` instead. Every
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

# Log in as the demo Super Admin
r = client.post("/auth/login", json={"identifier": "r.adeyemi@corp.io", "password": "SuperAdmin123!"})
token = r.json()["token"]
headers = {"Authorization": f"Bearer {token}"}

# Try whatever you just built, e.g.:
r = client.get("/assets", headers=headers)
print(r.status_code, r.json())
EOF

rm -f /tmp/test.db   # clean up when you're done
```

This is exactly the pattern used to verify the exports, audit trail, and
account-lockout features described in this README while they were built —
copy/adapt the snippet above for whatever endpoint you're changing.

### Frontend

Since there's no build step, just refresh the page in your browser after
saving a `.js`/`.html` file. Open your browser's DevTools Console while
testing — `js/api.js` throws a real `Error` (with the backend's message)
on any failed request, which will show up there if something goes wrong
silently in the UI.

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
  runs on the background `worker` container rather than inline in the
  request/response cycle (`tasks.send_email_task` — see [Due-Date
  Extensions & Notifications](#due-date-extensions--notifications)).
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
  see `nginx/default.conf.template`'s `Content-Security-Policy` header and
  its accompanying comment for the reasoning behind each directive.
- ✅ Exactly one **Super Admin**, hardcoded via environment variables
  rather than a database row — it can never be created, edited, or deleted
  through the app, and never appears in the User Directory or any other
  listing. See [Roles & Permissions Model](#roles--permissions-model).
- ✅ Backend container runs as an unprivileged user, not root.
- ✅ Structured, correlated logging for every login attempt and password
  change (never the password itself).
- ✅ Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) can be fully
  disabled via `ENABLE_API_DOCS=false` — gated independently at **both**
  the backend (FastAPI never generates/serves the schema at all, not just
  a hidden UI) and nginx (blocks the request before it ever reaches the
  backend). Defaults to `true` for local-dev convenience; **set `false`**
  in Render/cloud (`render.yaml` already does this). See `config.py`'s
  `ENABLE_API_DOCS` docstring and the Environment Variables Reference
  above.

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
- [ ] Set `CORS_ORIGINS` to your real frontend domain(s) only.
- [ ] Decide on email: leave `NOTIFICATIONS_ENABLED=false` if you don't
      want extension-request/overdue/due-soon-checkout emails yet, or set
      it to `true` and fill in real `SMTP_HOST`/`SMTP_FROM_EMAIL`/etc. —
      see [Due-Date Extensions & Notifications](#due-date-extensions--notifications).
      Either way, confirm the `worker` service is actually running
      (`docker compose ps` / your platform's dashboard) — it's what sends
      every email AND generates every audit-ledger export; a stopped
      `worker` means "Export" buttons hang forever and extension-request
      emails silently never leave the enqueue step.
- [ ] Set `ENABLE_API_DOCS=false` for **both** the backend and frontend
      services (same `.env` key drives both locally; `render.yaml` already
      sets `false` for both services in Render). Confirm it worked by
      requesting `/docs` on your deployed URL and `/openapi.json` directly
      against the backend if it's ever reachable from anywhere but
      nginx — both should return a plain `404`, not a docs page or schema.
- [ ] This app already reverse-proxies internally via nginx (see
      [Deploying Across Environments](#deploying-across-environments-nginx-reverse-proxy)),
      but that nginx layer does **not** terminate HTTPS itself. Put TLS
      termination in front of it — a managed platform's own edge/load
      balancer (Render does this automatically), or a cloud load balancer
      / cert-manager setup if you're self-hosting nginx. This app doesn't
      set `Strict-Transport-Security` itself — see
      `middleware/security_headers.py`'s docstring for why that belongs at
      the TLS-terminating layer, not here.
- [ ] Drop `--reload` from the backend's `uvicorn` command and run with
      multiple `--workers` instead (see `backend/Dockerfile`'s comment).
- [ ] Consider swapping the in-memory login rate limiter for a
      Redis-backed one if you run more than one backend replica (see
      `middleware/rate_limit.py`'s docstring).
- [ ] Review and tighten `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` /
      `ACCOUNT_LOCKOUT_DURATION_MINUTES` and
      `LOGIN_RATE_LIMIT_MAX`/`LOGIN_RATE_LIMIT_WINDOW_SECONDS` for your
      actual expected traffic pattern.
- [ ] Set up a real backup schedule for the Postgres volume — this
      project doesn't include one, since backup strategy is very
      deployment-specific (managed Postgres providers usually handle this
      for you automatically).

## Safely Updating An Existing Production Deployment (CI/CD)

**This repo ships three GitHub Actions workflows** in
`.github/workflows/`: [`ci.yml`](.github/workflows/ci.yml) (lint/syntax
sanity checks on every push and PR — there's no full test suite yet, see
[Suggested Future Features](#suggested-future-features)),
[`deploy-docker-compose.yml`](.github/workflows/deploy-docker-compose.yml)
(push-to-deploy for a self-hosted VPS), and
[`deploy-render.yml`](.github/workflows/deploy-render.yml) (migrate-then-deploy
for the Render Free-plan target this project ships with). **You almost
certainly only want one of the two `deploy-*.yml` files active** — delete
whichever doesn't match the platform you actually deployed to, so pushes
don't try to deploy the same code to two places. Both are already wired to
follow the same rule, which is what makes any of this genuinely *safe* to
automate rather than just fast:

> **Migrate first, deploy second, and only ever ADD to the schema —
> never rename or drop a column in the same release that also removes the
> code using it.** New code talking to an old (not-yet-migrated) database
> is usually fine if you only ever add nullable columns/tables. Old code
> talking to a new (already-migrated) database breaks the moment a
> migration renames/drops something the still-running old code expects.
> Deploying migrations *after* code, or dropping/renaming columns in the
> same release the code stops using them, both create a window where
> requests fail — see [Database & Migrations](#database--migrations-alembic)
> for how this project's Alembic setup fits in.

### Self-hosted Docker Compose (VPS / bare cloud VM)

[`deploy-docker-compose.yml`](.github/workflows/deploy-docker-compose.yml)
runs on every push to `main` (or on demand via **Actions → Deploy (Docker
Compose / VPS) → Run workflow**) and, over SSH, does exactly this — in
order, with `set -e` so a failed step stops the whole deploy instead of
limping forward:

```bash
git pull                                       # 1. get the new code
docker compose build backend worker frontend   # 2. build new images
                                                #    (doesn't touch running containers yet)
docker compose exec -T backend alembic upgrade head  # 3. migrate FIRST, while the
                                                       #    OLD containers are still
                                                       #    serving traffic -- safe as
                                                       #    long as the migration is
                                                       #    additive (see the rule above)
docker compose up -d --no-deps backend worker frontend  # 4. swap in the new
                                                          #    images one service at
                                                          #    a time; --no-deps stops
                                                          #    Compose from also
                                                          #    restarting db/redis
```

It then verifies the deploy (the backend answers `/healthz` from inside
the Compose network, and nginx is serving the login page) before the
workflow reports success.

**Required repository secrets** (Settings → Secrets and variables →
Actions): `PROD_HOST`, `PROD_SSH_USER`, `PROD_SSH_KEY` (the private half of
a key pair whose public half is already in that user's
`~/.ssh/authorized_keys` on the server), and `PROD_PROJECT_DIR` (the
absolute path to this repo's checkout on the server).

- **Rollback:** `git checkout <previous-commit>` on the server, then
  re-run steps 2 and 4 by hand (skip step 3 — you never roll a migration
  *back* just to roll code back; only run `alembic downgrade -1` if the
  migration itself is the thing you need to undo, and only if you're
  certain no code depending on the new column/table has shipped anywhere
  else).
- **Zero-downtime-ish, not true zero-downtime:** `docker compose up -d`
  stops and starts each named container in place, so there's a brief gap
  (seconds) per service. For true zero-downtime you'd need two backend
  containers behind nginx and a rolling `--scale`, which is a bigger
  change than this project's single-replica Compose file supports out of
  the box — see [Suggested Future Features](#suggested-future-features).
- **This workflow has no manual-approval gate** — every push to `main`
  deploys immediately once CI-equivalent checks pass. Add an `environment:
  production` block with required reviewers (GitHub's built-in deployment
  protection rules) to the job in `deploy-docker-compose.yml` if you want
  a human to approve every deploy first.

### Render

The free-plan shape this project ships with (see [Render](#render) above)
makes Render's own automated pre-deploy migrations **genuinely
unavailable**, for two concrete reasons:

- Render's `preDeployCommand` (its supported mechanism for "run migrations
  before the new version goes live") is **only available on paid instance
  types** — it does not exist as an option on the Free plan at all.
- The Shell tab and SSH access into a running instance are **also
  paid-plan-only** — so there's no way to `exec` into the Free web service
  and run `alembic upgrade head` by hand against it either, the way
  `docker compose exec backend ...` works locally.

[`deploy-render.yml`](.github/workflows/deploy-render.yml) works around
both limits by running the migration from CI, against the database's
*external* connection string, and only calling Render's Deploy Hook
afterward — so the migration always finishes before the new code goes
live, without needing `preDeployCommand` or Shell access at all:

- [ ] **In the Render Dashboard, set `snipeit-lite-web`'s auto-deploy to
      "Off"** (Settings → Build & Deploy → Auto-Deploy) — otherwise Render
      still auto-deploys on every push on its own, racing this workflow's
      migration step, which defeats the whole point.
- [ ] Get `snipeit-lite-db`'s **External Database URL** (Render Dashboard
      → `snipeit-lite-db` → Info — Free Postgres instances still expose
      one; only Key Value/Redis is private-network-only on Free, per
      `render.yaml`'s comments) and save it as the `RENDER_DATABASE_URL`
      repository secret.
- [ ] Get `snipeit-lite-web`'s **Deploy Hook URL** (Render Dashboard →
      `snipeit-lite-web` → Settings → Deploy Hook) and save it as the
      `RENDER_DEPLOY_HOOK_URL` repository secret.
- [ ] Push to `main` (or run **Actions → Deploy (Render) → Run workflow**
      on demand). The workflow runs `alembic upgrade head` against the
      external URL first; the deploy-hook `curl` only fires if that step
      succeeds.
- [ ] Confirm the new deploy is live and a login still succeeds (expect
      the usual free-plan cold start — see [Render](#render) above).

If you've since upgraded `snipeit-lite-web` to a paid instance type, the
simpler long-term fix is to delete `deploy-render.yml` entirely and add
`preDeployCommand: cd backend && alembic upgrade head` directly to
`render.yaml` instead — Render then handles the ordering for you natively,
with no external CI step required.

### Generic cloud (AWS/GCP/Azure/Kubernetes/etc.)

Same "migrate first, deploy second" rule applies; the mechanics depend on
your platform's own release-step feature (ECS's task definition + a
one-off migration task before the service update, a Kubernetes `Job` +
`initContainer` pattern ahead of a rolling `Deployment` update, etc.) —
outside this project's scope to ship a ready-made workflow for, but the
same sequencing constraint from the top of this section governs all of
them; `deploy-docker-compose.yml` is a reasonable template to adapt.

---

## Suggested Future Features

Small, well-scoped follow-ups if you want to keep extending this project:

- **A manual-approval / staging gate on the CI/CD pipeline** —
  `.github/workflows/deploy-docker-compose.yml` and `deploy-render.yml`
  (see [Safely Updating An Existing Production
  Deployment](#safely-updating-an-existing-production-deployment-cicd))
  deploy straight to production on every push to `main`, with no staging
  environment and no human approval step in between. Adding a GitHub
  `environment: production` block with required reviewers (or a separate
  staging deploy target that has to pass smoke tests first) would close
  that gap. Gating deploys on `ci.yml` passing first (`on: workflow_run`
  instead of a parallel `on: push`) is a smaller, complementary version of
  the same idea.
- **An automated test suite** (`pytest` + `TestClient` + a throwaway
  SQLite or test-Postgres database) — see
  [Testing Your Changes](#testing-your-changes) for the manual pattern
  this would formalize.
- **Redis-backed rate limiting** (e.g. `slowapi` or `fastapi-limiter`) if
  the backend is ever scaled to multiple workers/replicas, so all
  instances share one counter instead of each enforcing its own limit
  independently.
- **A `deleted_by` column** recording which admin performed a given
  soft-delete (good first Alembic migration exercise) — `restore_user()`
  itself (undoing a soft-delete) already shipped; see [Directories](#directories-super-admin--manager).
- **Case-insensitive login** (`func.lower()` comparison + a matching
  unique index) so `T.Okafor@corp.io` and `t.okafor@corp.io` are treated
  as the same account.
- **`Strict-Transport-Security` (HSTS)**, set at your TLS-terminating
  reverse proxy once deployed with HTTPS. (A real `Content-Security-Policy`
  is no longer on this list — see `nginx/default.conf.template`, which now
  sets one tuned against the frontend's actual CDN/script usage.)
- **Per-user notification preferences** — email is currently all-or-nothing
  via `NOTIFICATIONS_ENABLED` (see [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications)); a `users` table
  column for "email me my own overdue/due-soon reminders: yes/no" would
  be a small, well-scoped follow-up.
- **`httpOnly` cookie sessions + CSRF tokens**, replacing the current
  `localStorage`-based JWT storage (see the trade-off noted in
  [Security Model](#security-model)).
- **OpenTelemetry tracing** — the request-ID/structured-logging
  foundation (`middleware/request_context.py`, `logging_config.py`) is a
  natural stepping stone toward full distributed tracing if this app ever
  calls out to other services.
- **Scheduled/async large exports** — today's exports are built
  synchronously in one request; if directories grow very large, a
  background-job + "email me the file when it's ready" pattern would
  scale better than holding the request open.

## Troubleshooting

- **Login (or literally any `/api/*` call) fails with `405 Method Not
  Allowed`, and the response body is just `{"detail": "Method Not
  Allowed"}`** — this means nginx is forwarding requests to the backend
  with the wrong path (commonly, every request collapsing down to just
  `/`, which only has a `GET` handler). This is a well-known nginx
  gotcha: `proxy_pass`'s usual "trailing slash strips the matched
  `location` prefix" behavior **only works when the upstream address is a
  static string** — it silently stops working the moment that address is
  a *variable* (which `nginx/default.conf.template`'s `/api/` block uses,
  so nginx re-resolves `BACKEND_HOST` on every request instead of caching
  a possibly-stale IP). The fix already in this repo uses an explicit
  `rewrite ^/api/(.*)$ /$1 break;` before `proxy_pass` instead of relying
  on that trick — if you ever edit that `location /api/` block, keep the
  `rewrite` line, or `/api/*` requests will start silently arriving at the
  backend as just `/` again. Rebuild the frontend image after any nginx
  config change: `docker compose build --no-cache frontend && docker
  compose up -d --force-recreate frontend`.
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
  close/clear** — every extension-related action enqueues an email via
  the `worker` container instead of sending it inline (see [Due-Date
  Extensions & Notifications](#due-date-extensions--notifications)), so
  the API itself should return almost instantly regardless of SMTP
  speed. If it's still slow, check that the `worker` container is
  actually running (`docker compose ps`) and that Redis is reachable —
  a broker `.delay()` call that can't reach Redis is caught and logged
  (never raised — see `services/extension_service.py -> _notify()`), but
  a *backend* process that can't reach Redis at all for unrelated reasons
  is worth investigating separately.
- **Extension emails never arrive, even with `NOTIFICATIONS_ENABLED=true`**
  — check the `worker` container's logs, not the `backend` container's —
  emails are sent from there (`tasks.send_email_task`), not from the API
  process that received the original request. Also confirm `SMTP_HOST`
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
