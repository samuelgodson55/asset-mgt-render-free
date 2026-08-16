# Snipe-IT Lite

A small, self-hosted **IT asset registry / equipment checkout system** —
think "who currently has the MacBook Pool unit #12, and when is it due
back?" Built with a **FastAPI + PostgreSQL** backend and two mutually exclusive
frontend options: a legacy vanilla HTML/JS site and a **React + TypeScript +
Vite** Ledger SPA. Both are served through the same nginx-based container
shape and wired together with Docker Compose.

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


### Load-test runner reliability

`scripts/load-test.py` uses per-worker HTTP/1.1 keep-alive connections and fully
drains each response before reuse. It also reports HTTP status distribution and
treats responses that do not match `--expected-status` as load-test failures.
Use `--no-keep-alive` only for diagnostics.

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
13. [Distributed Tracing (OpenTelemetry)](#distributed-tracing-opentelemetry)
14. [Full API Reference](#full-api-reference)
15. [File & Function Reference](#file--function-reference)
16. [Making Changes Safely (A Guide For Beginners)](#making-changes-safely-a-guide-for-beginners)
17. [Testing Your Changes](#testing-your-changes)
18. [Security Model](#security-model)
19. [Running In Production](#running-in-production)
20. [Safely Updating An Existing Production Deployment (CI/CD)](#safely-updating-an-existing-production-deployment-cicd)
21. [Suggested Future Features](#suggested-future-features)
22. [Troubleshooting](#troubleshooting)

---

## What This App Does

Five kinds of people use the system:

| Role | Can do |
|------|--------|
| `super_admin` | Everything `admin` can do (see below). This IS a `users` table row now — exactly one, bootstrapped by `alembic upgrade head` in production (or `database.py`'s `seed_db()` for local/dev) — but its identity (`SUPER_ADMIN_USERNAME`/`SUPER_ADMIN_NAME`) is still fixed/hardcoded and it can never be created/edited/deleted through the app. Its password is a normal database-backed hash, rotatable through the same flows as any other account (see [Environment Variables Reference](#environment-variables-reference)). It never appears in the User Directory, bulk exports, or the Audit Trail. |
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
  once. A "Sample CSV" download sits right next to the import drop zone
  (client-side generated, no round trip) with the exact `name` /
  `total_quantity` / `category` columns pre-filled with example rows,
  so there's a working template to copy your own inventory sheet into
  instead of guessing at column names. Files over 5 MiB are rejected
  before parsing (denial-of-service protection). Malformed rows are
  reported back to you individually instead of failing the whole import.
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
- **Overdue alerts** — the Notification Center bell (navbar, every
  dashboard) lists every active checkout whose due date has passed,
  most-overdue-first, with an unread-style badge count. See [The
  Notification Center](#the-notification-center-everyone) below — this
  used to be an always-visible dashboard banner; it's now closed by
  default and opens on demand.
- **Due Soon alerts** — "a reminder before something goes overdue" — the
  same bell dropdown lists every active checkout due within
  `DUE_SOON_REMINDER_DAYS` that hasn't gone overdue yet, soonest-first, in
  its own section. See [Due-Date Extensions &
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
  Requests" section inside the Notification Center bell dropdown (see
  [The Notification Center](#the-notification-center-everyone) below)
  lists every pending request system-wide (Managers have no
  department-scoping — they see exactly what an Admin/Super Admin sees)
  with one-click **Approve**/**Deny** buttons, opening straight into that
  checkout's Custody Ledger. Approving is what actually moves the
  checkout's real due date; denying leaves it untouched. Both write an
  audit log entry and email the requester back (if they're a logged-in
  User with an email address) **and** surface in the requester's own
  Notification Center ("N extension request update(s)") the next time
  they open it — see `GET /checkouts/my-extension-decisions` and
  `backend/services/extension_service.py`'s
  `list_my_recent_extension_decisions()`, in case they miss the email.
  Any account can self-request an extension on their own checkout (Staff,
  Customer, Manager, Admin alike), so this section can appear in every
  dashboard's bell, not just staff.html/customer.html.
- **Grant one directly (Manager / Admin / Super Admin)** — see the
  **Extend** button described in [Custody & Returns](#custody--returns-super-admin--manager)
  above. Same unrestricted permission as approving a request, just
  without the request existing first.
- **A reminder before something goes overdue (everyone)** — the same
  `DUE_SOON_REMINDER_DAYS` window (2 days by default — see [Environment
  Variables Reference](#environment-variables-reference)) drives three
  matching, proactive nudges, all surfaced BEFORE a checkout's due date
  actually passes:
  - **Admin / Manager** — a **"Due Soon"** section inside the
    Notification Center bell dropdown (above the "Overdue" section) lists
    every active checkout system-wide that's due within the window,
    soonest first — see `backend/services/checkout_service.py`'s
    `list_due_soon_checkouts()` and `GET /checkouts/due-soon`.
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
`backend/tasks/notification_tasks.py`) — plain SMTP by default
(`EMAIL_PROVIDER=smtp`), no vendor SDK ever (the two alternate providers
below are raw HTTP calls, not an installed SDK package), off by default
(`NOTIFICATIONS_ENABLED=false` — see [Environment Variables
Reference](#environment-variables-reference)). If you're deploying on
Render's free plan (which blocks outbound SMTP ports at the network
level), set `EMAIL_PROVIDER=brevo` or `EMAIL_PROVIDER=resend` instead —
see [Environment Variables Reference](#environment-variables-reference)
for `BREVO_API_KEY`/`RESEND_API_KEY`. Three kinds go out once
enabled and configured:
1. **Extension-request lifecycle** — the **Digest Recipients** list (the
   same admin-configured list described in #2 below, plus anything in
   `ADMIN_NOTIFICATION_EMAILS`) is emailed the moment a new request comes
   in; the checkout's holder is emailed back once their request is
   approved or denied, or once a Manager/Admin/Super Admin grants an
   extension directly (no request needed for that email to go out). Being
   a Manager/Admin account does not by itself get you this notification —
   only addresses on the configured list do, same rule as the digests. If
   the list is empty, no new-request notification is sent (the holder's
   approved/denied email still goes out either way, since that's addressed
   to them directly, not to the general list).
2. **Daily overdue digest** — a scheduled Celery Beat job
   (`send_overdue_notifications`, daily at `OVERDUE_DIGEST_HOURS_UTC`
   — 08:00 UTC by default) emails each overdue checkout's own holder a reminder,
   plus one combined system-wide summary digest to the **Digest
   Recipients** list — a runtime-editable list of email addresses
   configured by a Super Admin/Admin (see the "Daily Digest Recipients"
   panel on `admin.html`'s Audit & Backups tab, backed by
   `GET`/`PUT /settings/digest-recipients`,
   `backend/services/notification_service.py`'s
   `get_digest_recipient_emails()`/`set_digest_recipient_emails()`).
   **Being a Manager/Admin account does not by itself get you this
   digest** — only addresses on this list (plus anything in the
   env-configured `ADMIN_NOTIFICATION_EMAILS`) receive it, and an address
   here doesn't need to correspond to a `users` row at all (an ops
   distribution list works fine). If the list is empty, no digest is sent.
   The extension-request alert in #1 above draws on this exact same list.
3. **Daily due-soon reminder digest** — the proactive counterpart to #2:
   a second scheduled Celery Beat job (`send_due_soon_reminders`, daily
   at `DUE_SOON_DIGEST_HOURS_UTC` — 08:00 UTC by default) sends the
   *same shape* of individual-holder-reminder + Digest-Recipients-audience
   summary email described in #2, just for checkouts due within
   `DUE_SOON_REMINDER_DAYS` instead of ones that have already passed their
   due date — "a reminder before something goes overdue," by email as
   well as on the dashboard. `OVERDUE_DIGEST_HOURS_UTC`/
   `DUE_SOON_DIGEST_HOURS_UTC` both accept the same comma-separated
   hours-of-day-UTC syntax as the [Backups](#backups) section's
   `BACKUP_HOURS_UTC` (e.g. `8,20` for twice a day) — a fixed clock
   time, not "N hours after the worker booted," so you know exactly
   when a digest lands in your inbox.
4. **Pending-approval SLA nudges** — closes a quiet gap the three kinds
   above don't cover: an Extension Request or Quotation that nobody ever
   opens the panel/tab to decide on can otherwise sit **`pending`/
   `submitted` forever** with no reminder at all. Two more scheduled
   Celery Beat jobs (`escalate_pending_extension_requests`/
   `escalate_pending_quotations`, `backend/tasks/sla_tasks.py`), running
   every `APPROVAL_SLA_CHECK_INTERVAL_MINUTES` (60 minutes by default —
   a plain "every N minutes" interval like the audit-partition check, not
   a fixed clock time like #2/#3 above), find anything still `pending`
   past `EXTENSION_REQUEST_SLA_HOURS`, or still `submitted` past
   `QUOTATION_SLA_HOURS` (24 hours each by default), and email ONE
   combined digest for each queue to the exact same Digest-Recipients +
   `ADMIN_NOTIFICATION_EMAILS` audience #1–#3 already use. Once a
   row has been nudged, it won't be nudged again until
   `APPROVAL_SLA_ESCALATION_REPEAT_HOURS` (24 hours by default) has
   passed since that nudge — repeatedly enough that a long-neglected item
   keeps resurfacing, not so often that the alert becomes noise people
   learn to ignore. As soon as a request/quote is actually decided
   (approved/denied/fulfilled), it stops matching this check for good,
   regardless of how many times it was nudged before. If the combined
   recipient list is empty, nothing is sent — same as every other digest
   above.

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
the app locally. Set `SEND_INDIVIDUAL_HOLDER_REMINDERS=false` to keep
sending the combined Digest-Recipients summary in #2/#3 above while
skipping the individual per-holder reminder email — useful if you only
want one ops-facing digest and not one email per overdue person.

### The Notification Center (everyone)

A single bell icon in every dashboard's navbar (`js/components/notifications.js`),
replacing what used to be a stack of always-visible dashboard banners
(Overdue / Due Soon / Extension Requests / extension-decision updates).
It's closed by default and shows an unread-style badge count instead:

- **Super Admin / Manager** see the review-facing feeds — Overdue
  Checkouts, Due Soon, and pending Extension Requests awaiting their
  decision — plus the personal sections below, since they can also have
  their own checked-out items.
- **Staff / Customer** see only the personal sections — their own items
  overdue/due soon, their own pending extension requests, and updates on
  decisions made about their requests.
- **Clicking a notification IS the action** — an Overdue/Due Soon or
  pending Extension Request entry opens straight into that checkout's
  Custody Ledger (where Approve/Deny for a pending request actually
  happens), and a personal Due Soon/Overdue notification opens the
  Request Extension modal directly.
- Nothing needs its own dismiss/recall bookkeeping the way the old banner
  stack did — the dropdown just always reflects whatever is currently
  true the moment it's opened.

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

### Equipment Quotations (Quote-to-Checkout)

A self-service "shopping cart" for equipment rental requests, built on
top of the Asset Inventory, with a full request → approve → fulfill
workflow behind it. See `backend/services/quotation_service.py` and
`frontend/js/components/quotation.js` for the implementation.

**Staff / Customer, self-service:**
- **Browse the Asset Catalog** (`GET /assets/catalog`) — a read-only view
  of every active asset pool showing name, category, and day-rate
  price. Stock levels (available quantity, in/out-of-stock) are hidden
  by default — see `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER` below — since a
  Staff/Customer account doesn't need to see live inventory to request
  equipment.
- **Build an order** — add items with a quantity and a start/due date to
  one standing draft order ("My Order"). Editing a quantity or removing a
  line updates the running subtotal/VAT/total immediately; pricing is
  always read live off the current asset price and the global VAT
  setting, never snapshotted, so an Admin's price/VAT edit is reflected
  the next time the cart is opened.
- **Export as PDF** — download the current draft as a branded PDF (asset,
  category, quantity, dates, VAT, total) to share with a manager
  offline, before ever submitting it.
- **Submit the order** — turns the draft into a permanent, ID-tagged
  Quotation (e.g. `QT-000001`) an Admin/Manager can look up. It becomes
  read-only to view in "My Order" history, though the requester can still
  adjust quantities or remove lines on their own submitted quote while it
  sits in the initial "submitted" state — an Admin/Manager takes over
  edits from "Approved" onward. Adding a new item afterward starts a
  fresh draft, exactly like the old always-one-cart behavior.

**Admin / Manager, the "Quotes" tab:**
- **Look up any submitted Quotation** by its reference number or the
  requester's name/email, or start a brand-new one directly on someone's
  behalf.
- **Adjust it** — change quantities, remove lines, add/remove a
  **not-in-inventory line** (a specialty rental sourced externally, with
  its own one-off price — visible to the requester but only ever
  editable by an Admin/Manager), edit internal notes, and assign the
  quote to a specific user (or an Ad-Hoc Individual who has no login).
- **Approve** — flips a Quotation to "Approved / Ready for Pickup" and
  locks it against further item/notes/assignment edits, by anyone.
- **Fulfill (the Fulfillment Drawer)** — bulk physical checkout: turns
  every line on an approved Quotation into a real `AssetCheckout` in one
  atomic transaction, evaluating and deducting stock only at this exact
  moment (never earlier in the workflow), and marks the Quotation
  fulfilled.
- **Export as PDF** — the same branded PDF export, available for any
  Quotation by ID.
- **Delete (Admin/Super Admin only)** — permanently deletes a submitted
  or approved Quotation, from either the Quotes table row or the Quote
  Detail modal. A Manager can do everything else above but cannot delete
  (`DELETE /quotations/{id}` is gated by `deps.require_super_admin`, a
  strictly stronger check than every other Quotes-tab action, which only
  needs `deps.require_privileged_role`). Refused once a Quotation is
  **fulfilled** — by that point it has real `AssetCheckout` history
  pointing back at it, so it's locked against deletion the same way it's
  already locked against every other edit.

**Global settings (Admin/Super Admin only):**
- **VAT percentage** — one editable value applied to every Quotation's
  total, everywhere, immediately.
- **`CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER`** (default `false`) — whether
  Staff/Customer accounts can see stock availability in the catalog. A
  Manager/Admin/Super Admin's own Asset Inventory view is unaffected
  either way.
- **`CURRENCY_CODE`** (default `NGN`) and **`SITE_NAME`** — the currency
  applied to every price shown/exported, and the deployment's brand name
  shown in the navbar/login header, `<title>`, AND the letterhead printed
  on every Quotation PDF. See [Environment Variables
  Reference](#environment-variables-reference).

**Notifications** — every Admin/Manager change to a Quotation notifies
its current recipient (whoever it's assigned to, else the original
requester) — never the person making the change, if that happens to be
the same account. Two things fire together, per notify-worthy change:
1. An **in-app bell notification** (`backend/models.py`'s
   `QuotationNotification`, `GET`/`PUT /quotations/notifications/*`) —
   always created, regardless of any email setting below, so nothing is
   ever silently lost.
2. A best-effort **email**, gated by `SEND_QUOTATION_RECIPIENT_EMAILS`
   (default `true`) on top of the master `NOTIFICATIONS_ENABLED` switch —
   set it `false` if customers find an email for every line-item tweak
   intrusive; they'll still see the same update in-app next time they log
   in.

Notify-worthy changes: a line item added/changed/removed, a
**not-in-inventory line added** (the email/in-app message names just the
asset and quantity — the "not-in-inventory"/"sourced from" detail stays
Admin/Manager-internal, in the audit log only), notes or the discount
edited, the quote **assigned** to someone, **approved**, and — the final,
most consequential step — **fulfilled** ("your equipment is ready").
Removing a not-in-inventory line is the one exception that still doesn't
notify (unlike removing a regular catalog line, which does) — a
pre-existing gap, not something this feature changed. See
`services/quotation_service.py`'s `_notify_quotation_recipient()`.

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

**Timestamp consistency**: every timestamp is stored/queried as UTC, but
the *displayed* hour is converted to the `DISPLAY_TIMEZONE` setting (see
[Environment Variables Reference](#environment-variables-reference))
before being written into a CSV/PDF cell, labeled with that zone's real
abbreviation (e.g. "WAT") instead of a hardcoded "UTC". This keeps every
export's hour in sync with what the Audit Trail already shows on screen
(which independently converts UTC → the viewer's own browser-local time)
— previously the two could differ by an hour or more for anyone outside
UTC. Audit-ledger export date-range filters ("from"/"to") are interpreted
in the same `DISPLAY_TIMEZONE`, so picking "today" matches the calendar
day a person is actually looking at.

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
- **Locked out or forgot your password? Two recovery paths:**
  - **Self-service "Forgot password?"** on the login page (`POST
    /auth/forgot-password`, `POST /auth/reset-password`) — emails a
    single-use, time-limited link (`PASSWORD_RESET_TOKEN_EXPIRY_MINUTES`,
    30 minutes by default) to the account's registered email address.
    Works for ANY account, including the root admin itself (which has no
    admin "above" it to reset it the other way). Always returns the same
    generic response whether or not the email/username matched a real
    account, so it can't be used to enumerate valid accounts; requires
    `NOTIFICATIONS_ENABLED=true` and a working email provider (see
    [Due-Date Extensions & Notifications](#due-date-extensions--notifications)),
    since the whole point is not needing a Super Admin available.
  - **Admin-issued reset** — a Super Admin or Admin can reset it for you
    directly from the User Directory instead — see
    [Directories](#directories-super-admin--manager). This is a separate,
    admin-only recovery path from both the self-service email flow above
    and the "My Profile" password change, and never requires knowing (or
    being told) the old password.
  - Either path also clears any active per-account lockout, same as the
    account holder finally remembering their own password.
- Sessions use signed JWTs; deactivating or deleting an account takes
  effect **immediately** on their next request, rather than waiting for
  their token to naturally expire.
- An idle dashboard automatically logs you out after a period of
  inactivity.
- **Super Admin accounts additionally require TOTP two-factor
  authentication** (Google Authenticator/Authy/1Password-compatible) to
  log in, with one-time-use recovery codes as a backup if the
  authenticator device is unavailable — see [Two-factor authentication
  (2FA)](#two-factor-authentication-2fa) below for the full enrollment,
  verification, and recovery-code flow.

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
- **Frontend:** Two mutually exclusive builds are supported. The legacy site
  under `frontend/` is vanilla HTML/JS with Tailwind CSS compiled ahead of time
  into `frontend/css/tailwind.css`; the newer Ledger SPA under `frontend-app/`
  is React + TypeScript + Vite with Tailwind CSS v4 and Recharts. Both are
  packaged by [`frontend/Dockerfile`](frontend/Dockerfile), but a final image
  contains exactly one frontend target: `frontend-react-only` or
  `frontend-legacy-only`. See [`frontend-app/README.md`](frontend-app/README.md)
  and [`build-tailwind/README.md`](build-tailwind/README.md).
- **Infra:** Docker Compose, 6 services: `db` (Postgres), `redis`
  (Celery broker/result backend, and the shared counter store for the
  Redis-backed login rate limiter — see [Security
  Model](#security-model) — not exposed to the host), `backend`
  (FastAPI/uvicorn, not exposed to the host), `worker` (Celery — builds
  audit-ledger/quotation CSV/PDF exports and sends every email
  notification, out-of-band; can be scaled to multiple replicas during
  peak use — see `docker-compose.yml`'s comments and
  [`DEPLOYMENT.md`](DEPLOYMENT.md)), `beat` (Celery Beat — runs the daily
  overdue AND due-soon scheduled digests; deliberately kept as its own
  single, never-scaled service so a scaled-up `worker` never fires the
  same digest more than once — see the comment at the top of the `beat`
  service in `docker-compose.yml`), `frontend` (nginx — serves the static
  site AND reverse-proxies `/api/*` to `backend`, the only
  publicly-exposed service).

The legacy frontend keeps its compiled Tailwind CSS in the repository, so
HTML/JS edits can be refreshed directly while working on that path. The React
frontend is a real Vite application: use `npm install` + `npm run dev` inside
`frontend-app/`, and use `npm run build` for a production bundle. The shared
Dockerfile selects exactly one target at image-build time, so changing the
frontend flavor is an explicit deployment choice rather than an accidental
side effect.

## Project Structure

```
snipe-it-lite/
├── .github/
│   └── workflows/
│       ├── ci.yml                     # Runs on every push + PR: ruff lint,
│       │                                # the real `pytest backend/tests`
│       │                                # suite (against real Postgres/Redis
│       │                                # service containers, not mocks --
│       │                                # see "Automated test suite" below),
│       │                                # pip-audit, a Gitleaks secret scan,
│       │                                # frontend build/rendering tests,
│       │                                # a boot-tested nginx clean-URL
│       │                                # config check, image build + Trivy
│       │                                # scan, and `infra/main.bicep`
│       │                                # validation;
│       │                                # reusable (workflow_call) -- also
│       │                                # gates deploy-azure-aca.yml,
│       │                                # deploy-azure-vm.yml, and
│       │                                # release.yml below
│       ├── build-push-images.yml       # Reusable (workflow_call) -- the ONE
│       │                                  # place that builds, tags, pushes,
│       │                                  # and Trivy-scans the backend +
│       │                                  # frontend images. release.yml,
│       │                                  # deploy-azure-vm.yml, and
│       │                                  # deploy-azure-aca.yml all call this
│       │                                  # instead of each running their own
│       │                                  # copy-pasted `docker build` matrix
│       ├── deploy-azure-aca.yml        # ONE workflow for the Container Apps
│       │                                  # target -- pick `staging` or
│       │                                  # `production` from the Actions tab.
│       │                                  # workflow_dispatch ONLY -- no
│       │                                  # push-to-deploy, and release.yml
│       │                                  # does NOT call this; a `git tag`
│       │                                  # push never deploys anything by
│       │                                  # itself. To ship a tagged release
│       │                                  # here, run this by hand and paste
│       │                                  # the version into `image_tag`.
│       │                                  # Builds -> migrates -> rolls out
│       │                                  # via a blue-green canary (see
│       │                                  # .github/scripts/aca-blue-
│       │                                  # green.sh) with automatic rollback
│       │                                  # on a failed smoke test. Replaces
│       │                                  # replaces the old separate
│       │                                  # staging/production workflows;
│       │                                  # choose the target in workflow_dispatch
│       ├── release.yml                 # Triggered by `git tag v1.x.x` push --
│       │                                # builds + tags both images with the
│       │                                # VERSION (not just a SHA), pushes
│       │                                # them to Docker Hub, updates
│       │                                # CHANGELOG.md, and cuts a GitHub
│       │                                # Release. Does NOT deploy anywhere --
│       │                                # deploy-azure-aca.yml/deploy-azure-
│       │                                # vm.yml are run by hand afterward
│       │                                # (workflow_dispatch), with the
│       │                                # version tag pasted into image_tag,
│       │                                # whenever you choose to ship it
│       ├── infra-deploy.yml            # One-time/occasional: provisions or
│       │                                  # updates infra/main.bicep itself
│       │                                  # (separate from the workflows
│       │                                  # above, which only ship new images)
│       ├── deploy-azure-vm.yml         # VM-path equivalent of
│       │                                  # deploy-azure-aca.yml above -- pick
│       │                                  # `vm-staging`/`prod` from the
│       │                                  # Actions tab. workflow_dispatch
│       │                                  # ONLY, same as the ACA path -- a
│       │                                  # `git tag` push never triggers
│       │                                  # this; run it by hand with the
│       │                                  # version in `image_tag` to ship a
│       │                                  # release -- build + push both
│       │                                  # images via build-push-
│       │                                  # images.yml, blocking Trivy scan,
│       │                                  # SSH over the Cloudflare Tunnel,
│       │                                  # sync docker-compose.vm.yml/
│       │                                  # Caddyfile, migrate, blue-green
│       │                                  # rollout (scripts/blue-green-
│       │                                  # deploy.sh), smoke test
│       ├── infra-deploy-vm.yml         # VM-path equivalent of infra-deploy.yml --
│       │                                  # provisions the VM itself via
│       │                                  # infra-vm/'s Terraform
│       ├── sync-secrets-vm.yml         # Pushes updated .env values out to an
│       │                                  # already-running VM without a full
│       │                                  # image redeploy
│       ├── repair-tunnel-token-vm.yml  # Scripted recovery for a stale
│       │                                  # CLOUDFLARE_TUNNEL_TOKEN after
│       │                                  # Terraform recreates the tunnel --
│       │                                  # pushes the current token to the
│       │                                  # VM over Azure's control plane
│       │                                  # (az vm run-command), which still
│       │                                  # works even when SSH/cloudflared
│       │                                  # itself is down -- see
│       │                                  # DEPLOYMENT_VM.md's Troubleshooting
│       └── dependabot.yml              # Weekly PR for every package manifest +
│                                          # both Dockerfiles' base images (see
│                                          # "Automated Dependency Updates" below)
│
├── scripts/                    # Manual operator scripts (not run by Docker) --
│   │                              # see "Distributed Tracing" below for the
│   │                              # full walkthrough of both
│   ├── tail-errors.sh             # Live-tails ERROR/CRITICAL structured logs
│   │                                # across every backend-side container,
│   │                                # request_id/trace_id pulled to the front
│   └── trace-request.sh           # Given a request_id or trace_id, greps
│                                     # every backend-side container's logs for
│                                     # it and prints the full story in order
│
├── CHANGELOG.md                # One dated section per `git tag v*.*.*`
│                                  # release, generated and inserted
│                                  # automatically by release.yml
├── DEPLOYMENT.md              # Companion to this file: production safety
│                                # checklist, scaling, backups, and the full
│                                # Azure Container Apps walkthrough
├── DEPLOYMENT_VM.md            # Same companion role as DEPLOYMENT.md, but
│                                 # for the single-Azure-VM path end-to-end:
│                                 # Terraform setup, Cloudflare Tunnel, secrets,
│                                 # rollback, backups, cost -- see "Azure VM"
│                                 # below
├── infra/
│   └── main.bicep              # Azure Container Apps infra, cost-optimized:
│                                  # 4 container apps (frontend, backend, db,
│                                  # redis) + migrate job -- Postgres/Redis
│                                  # run as containers (not managed
│                                  # services), 2 images pulled from Docker
│                                  # Hub (not ACR), no Key Vault -- see
│                                  # DEPLOYMENT.md
├── .env.azure.example         # Env var reference for the Azure deployment
│                                # shape (db/redis as internal container
│                                # apps, frontend/backend split, Container-
│                                # Apps-secret-backed secrets)
│
├── infra-vm/                   # Azure VM path's infra -- Terraform, not
│   │                             # Bicep (this target has no Container
│   │                             # Apps control plane to describe)
│   ├── main.tf                    # The VM itself, its managed data disk,
│   │                                # NSG, and the Cloudflare Tunnel/DNS
│   │                                # resources -- see DEPLOYMENT_VM.md
│   ├── variables.tf                # vm_size, disk size, region, etc.
│   ├── outputs.tf                   # ssh_hostname and friends, consumed by
│   │                                  # deploy-azure-vm.yml
│   ├── versions.tf                   # Provider/Terraform version pins
│   └── terraform.tfvars.example       # Copy to terraform.tfvars and fill in
│                                         # (see DEPLOYMENT_VM.md step 7)
├── docker-compose.vm.yml       # The VM path's compose file -- same six
│                                 # services as docker-compose.yml below,
│                                 # plus caddy (TLS re-presentation) and
│                                 # cloudflared (the Tunnel); images pulled
│                                 # by tag from Docker Hub instead of built
│                                 # locally -- see DEPLOYMENT_VM.md
├── Caddyfile                    # caddy's config for docker-compose.vm.yml --
│                                  # presents a free Cloudflare Origin CA cert
│                                  # for the inner hop from cloudflared
│
├── docker-compose.yml        # 6 services: db, redis, backend, worker, beat,
│                                # frontend -- worker/beat are split apart
│                                # specifically so worker can be scaled to
│                                # multiple replicas without duplicating the
│                                # scheduled digest jobs (see DEPLOYMENT.md)
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
│   ├── default.conf.template        # nginx config for the legacy frontend
│   ├── default.react.conf.template   # nginx config with SPA fallback for React
│   └── docker-entrypoint.d/
│       ├── 15-detect-resolver-ip.envsh # Auto-detects RESOLVER_IP from
│       │                                  # /etc/resolv.conf when unset
│       └── 25-fetch-deploy-status-htpasswd.sh # Fetches the protected
│                                              # deployment-dashboard credentials
│
├── build-frontend/              # Build tooling ONLY -- runs inside
│   │                              # frontend/Dockerfile's build stage, never
│   │                              # directly in Docker Compose. Minifies
│   │                              # frontend/js and frontend/*.html (and,
│   │                              # in production, obfuscates the JS) --
│   │                              # see build.js's own header comment for
│   │                              # the three modes.
│   ├── build.js                    # BUILD_ENV=local|development|production
│   └── package.json                 # terser + javascript-obfuscator +
│                                     # html-minifier-terser deps
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
│   │                                        # unmodified, by the `worker` and
│   │                                        # `beat` services -- see docker-compose.yml)
│   ├── alembic.ini                         # Alembic config (points at alembic/)
│   │
│   ├── scripts/                    # One-off/manual admin scripts -- never run
│   │   │                             # by Docker automatically
│   │   ├── gdrive_oauth_setup.py     # Interactive, run-once helper that
│   │   │                               # exchanges a downloaded Google OAuth
│   │   │                               # client JSON for a long-lived refresh
│   │   │                               # token -- prints the three
│   │   │                               # BACKUP_GDRIVE_OAUTH_* env values to
│   │   │                               # paste into .env. See "Backups" below.
│   │   ├── audit_partition_status.py    # Read-only report of every
│   │   │                                  # `audit_logs` partition that
│   │   │                                  # exists today (year, row count,
│   │   │                                  # on-disk size) -- run by hand once
│   │   │                                  # a year when deciding whether to
│   │   │                                  # retire the oldest one. See
│   │   │                                  # SRE_STRATEGY.md.
│   │   └── dev_seed_fake_old_partition.py  # Dev/staging-only helper that
│   │                                          # backdates a throwaway
│   │                                          # partition so the annual
│   │                                          # retirement runbook can be
│   │                                          # rehearsed safely without
│   │                                          # waiting for a real year to
│   │                                          # roll over -- see
│   │                                          # SRE_STRATEGY.md §6.4
│   │
│   ├── assets/                     # Static binary assets bundled INTO the
│   │   │                             # backend image (not user-uploaded data)
│   │   └── fonts/
│   │       ├── DejaVuSans.ttf         # Unicode-capable fonts `reportlab` embeds
│   │       └── DejaVuSans-Bold.ttf     # into every PDF export so non-ASCII
│   │                                    # currency symbols (e.g. Naira "₦")
│   │                                    # render correctly instead of as boxes
│   │
│   ├── tasks/                     # Celery tasks -- run on the `worker` container
│   │   ├── export_tasks.py          # generate_audit_export(): builds the CSV/PDF
│   │   │                              # off the request/response cycle
│   │   ├── notification_tasks.py     # send_email_task() (generic async email --
│   │   │                                # used by extension_service.py too) +
│   │   │                                # send_overdue_notifications() +
│   │   │                                # send_due_soon_reminders() (Celery
│   │   │                                # Beat digests -- see celery_app.py)
│   │   └── audit_partition_tasks.py   # ensure_audit_log_partitions() -- daily
│   │                                     # Celery Beat job that pre-creates
│   │                                     # `audit_logs`'s future yearly
│   │                                     # partitions (see
│   │                                     # services/audit_partition_service.py)
│   │
│   ├── middleware/                # ASGI middleware, one concern per file
│   │   ├── request_context.py       # Request Correlation ID (X-Request-ID)
│   │   ├── rate_limit.py             # Per-IP login rate limiting
│   │   ├── security_headers.py       # Standard defensive response headers
│   │   ├── error_handling.py         # Global unhandled-exception safety net
│   │   │                                # -- logs full traceback + request_id,
│   │   │                                # returns a safe {"detail", "request_id"}
│   │   │                                # JSON body instead of a bare 500
│   │   └── clean_urls.py             # Render single-service mode only --
│   │                                    # rewrites /admin -> admin.html etc.
│   │                                    # before StaticFiles, 301s old *.html
│   │                                    # links (see main.py's SERVE_FRONTEND
│   │                                    # block for why this is conditional)
│   │
│   ├── api/                       # Thin FastAPI routers (HTTP layer only)
│   │   ├── auth_api.py, assets_api.py, users_api.py, outsiders_api.py,
│   │   │     checkouts_api.py, audit_api.py
│   │   ├── backup_api.py               # System Backups panel -- status, list,
│   │   │                                # create, download, delete, restore
│   │   ├── quotations_api.py            # Self-service Asset Catalog + the
│   │   │                                  # whole Equipment Quotation /
│   │   │                                  # quote-to-checkout workflow (see
│   │   │                                  # "Equipment Quotations" below)
│   │   └── notifications_api.py         # Admin/Super Admin-only Daily Digest
│   │                                       # Recipients setting -- GET/PUT
│   │                                       # /settings/digest-recipients
│   │
│   ├── schemas/                   # Pydantic request/response models
│   │   ├── auth_schema.py, assets_schema.py, users_schema.py,
│   │   │     checkouts_schema.py, outsiders_schema.py
│   │   ├── quotations_schema.py        # QuotationItemCreate,
│   │   │                                 # QuotationItemQuantityUpdate,
│   │   │                                 # VatUpdateRequest,
│   │   │                                 # QuotationAssignRequest,
│   │   │                                 # QuotationMetaUpdate,
│   │   │                                 # QuotationCreateRequest,
│   │   │                                 # QuotationOutsourcedItemCreate
│   │   └── notifications_schema.py      # DigestRecipientsUpdateRequest
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
│   │   ├── backup_service.py               # pg_dump/psql backup+restore,
│   │   │                                     # optional Google Drive upload,
│   │   │                                     # the daemon-thread scheduler
│   │   ├── quotation_service.py             # Asset Catalog, draft "cart",
│   │   │                                      # submit -> approve -> fulfill
│   │   │                                      # workflow, VAT setting, PDF
│   │   │                                      # export -- the biggest service
│   │   │                                      # module in the app; see
│   │   │                                      # "Equipment Quotations" below
│   │   ├── export_service.py               # Shared CSV/PDF builders
│   │   ├── search_utils.py                  # Shared ILIKE search-filter helper
│   │   │                                       # (GET /assets, /users, /outsiders)
│   │   ├── stock.py                         # The Available-quantity formula
│   │   └── audit_partition_service.py       # Keeps `audit_logs`'s native
│   │                                           # Postgres RANGE partitions
│   │                                           # healthy -- pre-creates future
│   │                                           # yearly partitions, reports
│   │                                           # partition status; NEVER drops
│   │                                           # one (see SRE_STRATEGY.md's
│   │                                           # "Audit log partitioning &
│   │                                           # annual archive" runbook)
│   │
│   └── alembic/                    # Database migration scripts
│       ├── env.py
│       └── versions/                 # 11 migrations, each additive-only --
│           │                          # see "Database & Migrations" below
│           ├── 0001_baseline_schema.py         # starting schema
│           ├── 0002_bootstrap_root_admin.py    # inserts the one root
│           │                                     # `super_admin` row
│           ├── 0003_outsider_soft_delete.py    # soft-delete columns on
│           │                                     # outsiders
│           ├── 0004_outsider_convert_to_user.py  # Outsider -> real User
│           ├── 0005_user_convert_to_outsider.py  # User -> Outsider (revoke
│           │                                        # login)
│           ├── 0006_purge_deleted_users_and_assets.py  # `purged_at` for
│           │                                              # Purge Deleted
│           ├── 0007_split_contact_details.py   # outsider contact_details ->
│           │                                     # email/phone_number
│           ├── 0008_super_admin_totp.py        # TOTP secret/enabled columns
│           │                                     # (2FA)
│           ├── 0009_recovery_codes.py          # recovery_codes table (2FA
│           │                                      # backup codes)
│           ├── 0010_partition_audit_logs.py    # Converts `audit_logs` to a
│           │                                      # native Postgres RANGE-
│           │                                      # partitioned table (one
│           │                                      # partition per calendar
│           │                                      # year, plus a DEFAULT
│           │                                      # catch-all) -- see
│           │                                      # SRE_STRATEGY.md
│           ├── 0011_password_reset_tokens.py   # Self-service password reset tokens
│           ├── 0012_user_company.py            # User/company relationship
│           ├── 0013_quotation_notifications.py # Quotation notification persistence
│           ├── 0014_pending_approval_sla_nudges.py # Approval SLA tracking
│           ├── 0015_asset_department.py        # Asset-pool department data
│           └── 0016_quotation_paid_status.py   # Current `paid` quotation status
│
├── frontend-app/                # React + TypeScript Ledger SPA (Vite)
│   ├── src/
│   │   ├── components/          # Shared UI, drawers, modals, tables and status UI
│   │   ├── pages/               # Dashboard, Assets, Checkouts, Quotations,
│   │   │                          # My Items, Reports, Profile, Notifications,
│   │   │                          # Login and admin panels
│   │   ├── lib/                 # API client, auth, roles, custody/quote contexts,
│   │   │                          # theme, search, pagination, receipts and types
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── package.json             # Vite/React/TypeScript build and test scripts
│   ├── vite.config.ts           # `/api` dev proxy + Vite configuration
│   └── README.md                # React frontend architecture and deployment notes
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
    │   ├── tailwind.css       # Compiled by build-tailwind/ -- committed as a
    │   │                        # plain static file, not generated by Docker
    │   ├── theme.css           # Hand-written CSS the Tailwind build doesn't
    │   │                         # cover: dark-theme variables, the same
    │   │                         # narrow-desktop breakpoint override as
    │   │                         # tailwind.config.js's `sm`, small
    │   │                         # animation/utility classes
    │   └── auth-guard.css       # Tiny stylesheet loaded before auth.js runs,
    │                              # so a not-yet-authorized dashboard is
    │                              # hidden (not just unstyled) during the
    │                              # instant it takes JS to redirect -- avoids
    │                              # a "flash of protected content"
    └── js/
        ├── main.js            # Wires up every DOM event (event delegation)
        ├── api.js             # The one place that calls fetch()
        ├── auth.js            # Login/session/JWT-decode/role guard
        ├── auth-guard.js       # Runs BEFORE the rest of the page's JS --
        │                         # immediately kicks an unauthorized visitor
        │                         # back to the login page (pairs with
        │                         # css/auth-guard.css above)
        ├── ui.js               # Shared table/pagination/modal helpers
        ├── theme.js             # Light/dark theme toggle button + persistence
        ├── theme-init.js         # Inlined-early theme bootstrap -- applies
        │                           # the saved theme before first paint so
        │                           # there's no flash of the wrong theme
        ├── dashboard.js         # refreshDashboard() orchestrates all loads
        ├── vendor/
        │   └── qrcode.js          # Vendored qrcode-generator (npm), unmodified
        │                            # apart from the .mjs->.js rename -- renders
        │                            # the 2FA enrollment QR code only
        └── components/           # One file per feature area
            ├── assets.js           # Inventory table, dispatch, exceptions, CSV import
            ├── audit.js             # Audit ledger table + CSV/PDF export
            ├── backups.js            # System Backups panel (admin.html,
            │                           # true Super Admin only) -- status,
            │                           # list, create, download, delete, restore
            ├── custody.js            # Custody Ledger modal + returns + direct Extend
            ├── due-soon.js            # "Due Soon" feed -- rendered inside the
            │                            # Notification Center bell dropdown
            │                            # (see components/notifications.js)
            ├── exports.js             # Properties-assigned CSV/PDF downloads
            ├── extensions.js           # Request/Approve/Deny + direct-extend modals
            ├── myitems.js               # Staff/Customer "what do I have?" view
            ├── notifications.js          # THE NOTIFICATION CENTER -- a single
            │                               # bell icon (every dashboard's navbar)
            │                               # with an unread-style badge, replacing
            │                               # the old always-visible dashboard
            │                               # banner stack (Overdue/Due Soon/
            │                               # Extension Requests/decisions). Pulls
            │                               # together overdue.js/due-soon.js/
            │                               # extensions.js's review-facing feeds
            │                               # (Super Admin/Manager) plus each
            │                               # person's own item alerts (everyone)
            │                               # into one closed-by-default dropdown
            ├── outsiders.js             # Ad-Hoc directory table
            ├── overdue.js                # Overdue-checkouts feed -- rendered
            │                               # inside the Notification Center bell
            │                               # dropdown (see components/notifications.js)
            ├── profile.js                 # "My Profile" modal + change password
            ├── quotation.js                # Asset Catalog browsing, the
            │                                 # self-service cart, the
            │                                 # Admin/Manager "Quotes" tab, and
            │                                 # the Fulfillment Drawer bulk
            │                                 # checkout -- see "Equipment
            │                                 # Quotations" below
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
nginx (frontend container)   <-- reverse proxy: forwards the FULL path,
                                  "/api" prefix included, straight through
                                  to the backend container unchanged (no
                                  rewriting — see nginx/default.conf.template's
                                  `proxy_pass http://$backend_upstream$request_uri;`
                                  and the "Deploying Across Environments"
                                  section). The backend's own routers are
                                  mounted at that same "/api" prefix (see
                                  `backend/main.py`), so the path a route
                                  handler actually sees is identical to what
                                  the browser sent.
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

**Super Admin** isn't in the table above because it's provisioned
separately from the other demo accounts — see [The root admin
account](#the-root-admin-account). For local Docker Compose
(`AUTO_SEED_DEMO_DATA=true` by default), `database.py`'s `seed_db()` seeds
it with the same well-known-demo-password convention as every other
account: username `superadmin` / password `RootAdmin123!`. Rotate it (via
"My Profile" → Change Password, once logged in) before using this
anywhere real — see [The root admin account](#the-root-admin-account) for
how a real production deployment bootstraps it differently, with a
randomly generated password instead.

## Deploying Across Environments (nginx Reverse Proxy)

This app is designed to run, **unmodified**, across three tiers: your local
Docker Compose setup, a Render staging environment, and a real cloud
environment (AWS/GCP/Azure/etc.). The piece that makes that possible is the
`frontend` service — it's no longer a bare static-file server, it's an
**nginx reverse proxy** built from [`frontend/Dockerfile`](frontend/Dockerfile).

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
| `RESOLVER_IP` | Internal DNS server nginx uses to re-resolve `BACKEND_HOST` on every request (so a backend redeploy never leaves nginx pointed at a stale IP) | `127.0.0.11` (Docker's built-in DNS) | Auto-detected at boot from `/etc/resolv.conf` if left unset — see [`nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh`](nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh) |

`PORT`, `BACKEND_HOST`, and `BACKEND_PORT` all have sensible defaults baked
into `frontend/Dockerfile`. `RESOLVER_IP` deliberately does **not** — instead of
hardcoding a guess that could go stale on some future platform, it's
auto-detected at container boot (see the table above and
[`nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh`](nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh)
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
- [ ] That's it — no manual hostname copy-pasting. `JWT_SECRET_KEY` is
      auto-generated (`generateValue: true`). The root admin account is
      seeded by `AUTO_SEED_DEMO_DATA=true` (this Blueprint's default) with
      the same well-known demo password as every other seeded account --
      username `superadmin` / password `RootAdmin123!` (see [The root
      admin account](#the-root-admin-account) and `database.py`'s
      `seed_db()`) -- log in and rotate it immediately via "My Profile" →
      Change Password. For a real deployment, set `AUTO_SEED_DEMO_DATA` to
      `false` in the dashboard's Environment tab instead and bootstrap the
      root admin via `alembic upgrade head` (see below), which generates
      a random password instead of using the well-known demo one.
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

**Choosing a frontend (legacy or React)** — `Dockerfile.render` bakes
*both* the legacy multi-page static site and the React "Ledger" SPA into
this same image unconditionally; `render.yaml`'s `FRONTEND_VARIANT`
environment variable picks which one this service actually serves
(`react`, the default, or `legacy`). Switching is just a value change in
the Render Dashboard's Environment tab (or `render.yaml`) followed by a
redeploy — since nothing about the image itself changes, that's a plain
restart, not a rebuild. See `backend/config.py`'s `FRONTEND_VARIANT`
docstring and `backend/main.py`'s "STATIC FRONTEND" section for how the
choice is applied (`middleware/clean_urls.py`'s clean-URL rewrite for
`legacy`, `middleware/spa_fallback.py`'s client-side-route fallback for
`react`).

**Render secrets are intentionally persistent.** The Blueprint marks the
JWT signing secret, Brevo/Resend API keys, SMTP credentials/sender settings,
admin notification recipients, and all Google Drive OAuth/service-account
credentials plus the Drive folder ID with `sync: false`. Render therefore
leaves those Dashboard-managed values untouched on later Blueprint syncs, so
a normal deploy does not force you to re-enter or rotate those credentials.

<details>
<summary>Need a paid, multi-service, horizontally-scalable deployment instead? Click to expand.</summary>

Once you're off the Free plan, you can split this back into the original
three-service shape (a private backend, a private Celery worker, and a
public nginx frontend) for proper horizontal scaling and no shared-process
tradeoffs:

- [ ] Create a **Web Service** (`plan: starter` or higher) built from
      `frontend/Dockerfile` (build context = repo root) for the frontend/
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
Same pattern: deploy the `frontend/Dockerfile` image as your public-facing
service, deploy `backend/Dockerfile` as an internal-only service (e.g.
behind a private load balancer or in the same VPC/private subnet with no
public IP), and set `BACKEND_HOST`/`BACKEND_PORT` to match that
environment's actual internal DNS naming (e.g. an ECS Service Connect name,
a Kubernetes Service DNS name like `backend.default.svc.cluster.local`, or
an internal ALB/NLB hostname). Leave `RESOLVER_IP` unset unless you've
confirmed a specific value your platform needs — it's auto-detected from
`/etc/resolv.conf` at boot otherwise (see
[`nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh`](nginx/docker-entrypoint.d/15-detect-resolver-ip.envsh)).

**Deploying to Azure specifically?** This project ships a complete,
fully-automated, cost-optimized version of this pattern already —
[`infra/main.bicep`](infra/main.bicep) (three Azure Container Apps —
`frontend`, `backend`, `redis` — plus a managed Azure Database for
PostgreSQL Flexible Server and one Container Apps `migrate` Job;
`frontend`/`backend` scale independently, Redis remains internal-only and
Postgres uses Microsoft-managed database storage)
plus
`.github/workflows/deploy-azure-aca.yml` /
`infra-deploy.yml` — instead of the generic
mechanics above. See [`DEPLOYMENT.md`](DEPLOYMENT.md)'s **Azure Container
Apps Production Deployment (Cost-Optimized)** section for the full
one-time setup, cost breakdown, and how the pipeline runs day to day.

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

For the frontend, use the React/Vite app directly during development:

```bash
cd frontend-app
npm install
npm run dev
```

`frontend-app/vite.config.ts` proxies `/api/*` to `http://localhost:8000` by
default. Override that target with `VITE_DEV_API_PROXY_TARGET`, or set
`VITE_API_BASE_URL` in a local `.env.local` file if you intentionally want to
bypass the Vite proxy. For the production-shaped nginx path, use
`docker compose up` so nginx owns the `/api/*` reverse proxy. The legacy
`frontend/` site remains available for the `frontend-legacy-only` Docker target.

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
| `EXPORT_RESULT_DIR` | `/app/export_results` | Where the `worker` service writes finished audit/quotation export files to disk (a volume shared with `backend`, which streams them back out on download) — used instead of embedding the file's bytes in the Celery/Redis result, to keep memory use down under load. |
| `EXPORT_RESULT_TTL_SECONDS` | `3600` | How long a finished export file is kept on disk under `EXPORT_RESULT_DIR` before a cleanup pass deletes it. Only the small `{task_id, status, filename}` JSON result (not the file itself) lives in Redis, for this same duration. |
| `JWT_SECRET_KEY` | *(required, no insecure default allowed)* | Signs/verifies session tokens. **Must** be a long random string in production. |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm. |
| `JWT_EXPIRY_HOURS` | `12` | How long a login session stays valid. |
| `DISPLAY_TIMEZONE` | `Africa/Lagos` | IANA timezone every CSV/PDF export (audit ledger, properties-assigned reports) renders its timestamps in, and the zone an export date-range filter's "from"/"to" boundaries are interpreted in. Data is always stored/queried as UTC either way — this only controls the DISPLAY layer, so exported hours match what the Audit Trail already shows on screen (which converts UTC → browser-local automatically). The backend refuses to start if this isn't a real IANA zone name. |
| `CORS_ORIGINS` | localhost variants | Comma-separated list of origins allowed to call the API. |
| `AUTO_INIT_DB` | `true` | If true, runs `create_all()` on startup (creates missing tables). Set `false` in production and use Alembic instead. |

**Local Docker note (v11):** the Compose stack intentionally overrides `AUTO_INIT_DB=false` and runs a one-shot `migrate` service (`alembic upgrade head`) before the backend, worker, and Beat start. This keeps the local schema versioned. You do **not** need `alembic stamp head`; for a disposable local database, use `docker compose down -v` and rebuild.
| `AUTO_SEED_DEMO_DATA` | `true` | If true, seeds demo accounts/data on an empty DB at startup. Set `false` in production. Independently of this flag, the login page's "Demo accounts" credentials hint box (`frontend/index.html`) is physically removed from the shipped HTML whenever the frontend image is built with `BUILD_ENV=production` — see `build-frontend/build.js`'s `BUILD:PROD-STRIP` markers and the "Frontend build modes" section below. |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. |
| `LOG_FORMAT` | `json` | `json` (production/log aggregators) or `text` (readable local dev). |
| `LOGIN_RATE_LIMIT_MAX` | `5` | Max `/auth/login` attempts per IP per window before HTTP 429. |
| `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | `60` | The window (in seconds) the above limit applies over. |
| `ENABLE_API_DOCS` | `true` | Whether `/docs`, `/redoc`, `/openapi.json` exist at all. **Set `false` in Render/cloud** — see the Security Model section below. Also read by the frontend/nginx service (see the table below) as a second, independent layer. |
| `SUPER_ADMIN_USERNAME` | `superadmin` | Login identifier for the root admin account — see [Roles & Permissions Model](#roles--permissions-model). Also read directly (same env var name) by `alembic/versions/0002_bootstrap_root_admin.py` when it bootstraps this account in production. |
| `SUPER_ADMIN_NAME` | `Super Admin` | Display name for that account (shown in the navbar/profile, same as any other user's `name`). Also read by the bootstrap migration. |
| `ROOT_ADMIN_BOOTSTRAP_PASSWORD` | *(empty — auto-generated if unset)* | OPTIONAL. Read once, directly, by `alembic/versions/0002_bootstrap_root_admin.py` the first time it inserts the root admin row in production — never by the running app. Leave unset to have that migration generate a random password and print it to the migration job's own output exactly once instead. Not needed at all for local dev (`database.py`'s `seed_db()` uses a fixed demo password there). |
| `NOTIFICATIONS_ENABLED` | `false` | Master switch for all email (see [Due-Date Extensions & Notifications](#due-date-extensions--notifications)). Leave `false` for local dev with no mail server — every send is logged at `DEBUG` instead. |
| `EMAIL_PROVIDER` | `smtp` | `smtp` \| `brevo` \| `resend`. Plain SMTP works everywhere except Render's free plan, which blocks outbound SMTP ports at the network level — set this to `brevo` or `resend` there instead (both send over plain HTTPS via `requests`, no vendor SDK installed). `render.yaml` defaults this to `brevo` for exactly that reason. |
| `SMTP_HOST` | *(empty)* | Mail server hostname. Required if `NOTIFICATIONS_ENABLED=true` and `EMAIL_PROVIDER=smtp`. |
| `SMTP_PORT` | `587` | Mail server port (587 = STARTTLS, the standard). |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | *(empty)* | SMTP auth credentials, if your provider requires them. |
| `SMTP_USE_TLS` | `true` | STARTTLS vs. a plain/unencrypted connection (only appropriate for a local/private relay). |
| `SMTP_USE_SSL` | `false` | Use implicit TLS (typically port 465) instead of STARTTLS — for providers/relays that require it. |
| `SMTP_FROM_EMAIL` | *(empty)* | The `From:` address. Required if `NOTIFICATIONS_ENABLED=true` — most providers reject sends where this doesn't match a verified domain/sender. |
| `BREVO_API_KEY` | *(empty)* | Only read when `EMAIL_PROVIDER=brevo`. From your Brevo (formerly Sendinblue) account's API Keys page. |
| `RESEND_API_KEY` | *(empty)* | Only read when `EMAIL_PROVIDER=resend`. From your Resend account's API Keys page. |
| `ADMIN_NOTIFICATION_EMAILS` | *(empty)* | Comma-separated extra recipients who get every new-extension-request alert, plus every daily digest (overdue + due-soon) on top of the runtime-editable **Digest Recipients** list (`GET`/`PUT /settings/digest-recipients`, Super Admin/Admin only). Being an Admin/Manager account no longer implies receiving the daily digests by itself — see [Due-Date Extensions & Notifications](#due-date-extensions--notifications). |
| `OVERDUE_DIGEST_HOURS_UTC` | `8` | Comma-separated hours of day (UTC, each 0-23) the Celery Beat job checks for overdue checkouts and sends the digest — `8` fires once a day at 08:00 UTC, `8,20` fires twice a day. Same syntax as [Backups](#backups)' `BACKUP_HOURS_UTC` below — a fixed clock time, not "N hours after the worker booted." Lower to a couple of minutes from now while testing locally if you want to see it fire sooner. |
| `DUE_SOON_REMINDER_DAYS` | `2` | "A reminder before something goes overdue" — how many days ahead of its `due_date` an active checkout counts as "due soon". Drives the "Due Soon" section of the Notification Center, the "Due Soon" badge on My Items, AND the due-soon reminder email below, all from this one setting. |
| `DUE_SOON_DIGEST_HOURS_UTC` | `8` | Comma-separated hours of day (UTC, each 0-23) the Celery Beat job checks for checkouts about to go overdue and sends the due-soon reminder digest. Same comma-separated-hours syntax as `OVERDUE_DIGEST_HOURS_UTC` above — its own independent schedule. |
| `SEND_INDIVIDUAL_HOLDER_REMINDERS` | `true` | Whether the daily overdue/due-soon digests also email each affected checkout's own holder individually, on top of the combined Digest-Recipients summary. Set `false` to send only the one ops-facing summary email per run. |
| `EXTENSION_REQUEST_SLA_HOURS` | `24` | How many hours a `pending` Extension Request can go without a Manager/Admin/Super Admin decision before the SLA-nudge job escalates it — see [Due-Date Extensions & Notifications](#due-date-extensions--notifications). |
| `QUOTATION_SLA_HOURS` | `24` | Same idea for a `submitted` Quotation waiting on an Admin/Manager's approve/adjust decision — its own independent threshold, since the two queues can reasonably need different response-time expectations. |
| `APPROVAL_SLA_CHECK_INTERVAL_MINUTES` | `60` | How often (in minutes) the Celery Beat job checks both the Extension Requests and Quotations queues for anything past its SLA threshold above. A plain "every N minutes" interval, not a fixed clock time like `OVERDUE_DIGEST_HOURS_UTC` above. |
| `APPROVAL_SLA_ESCALATION_REPEAT_HOURS` | `24` | Once a pending request/quote has been escalated once, how many hours before it's eligible to be escalated again if it's still undecided — keeps a long-neglected item nudging repeatedly instead of firing once and going quiet. |
| `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` | `5` | Wrong-password attempts against **the same account** before it's locked, regardless of which IP they came from. |
| `ACCOUNT_LOCKOUT_DURATION_MINUTES` | `15` | How long that per-account lock lasts once triggered. |
| `PASSWORD_RESET_TOKEN_EXPIRY_MINUTES` | `30` | How long a self-service "Forgot password?" email link stays valid before it must be requested again — see [Account Security](#account-security-built-in-mostly-invisible-until-you-need-it). |
| `ENABLE_AUTO_BACKUP` | `true` | Runs a `pg_dump` backup inside this same process (a plain daemon thread — no Celery/Redis dependency) at each hour in `BACKUP_HOURS_UTC`. See [Backups](#backups). |
| `BACKUP_HOURS_UTC` | `3` | Comma-separated hours of day (UTC, each 0–23) the backup runs at — `3` for once a day, `3,15,21` for three times a day. |
| `BACKUP_HOUR_UTC` | *(unset)* | DEPRECATED single-hour alias kept for backward compatibility — if set, it wins outright over `BACKUP_HOURS_UTC`. New setups should use `BACKUP_HOURS_UTC` instead. |
| `BACKUP_DIR` | `/app/backups` | Where local backup files + their `index.json` metadata live inside the container. |
| `BACKUP_RETENTION_COUNT` | `7` | How many local backup files to keep before deleting the oldest. Google Drive copies (if enabled) are unaffected. |
| `BACKUP_GDRIVE_ENABLED` | `false` | Uploads every backup to Google Drive right after it's written locally — the only thing that makes a backup survive a Render redeploy/spin-down. |
| `BACKUP_GDRIVE_OAUTH_CLIENT_ID` | *(empty)* | Mode 1 (personal Google account) — printed by `backend/scripts/gdrive_oauth_setup.py`. Takes priority over Mode 2 if both are set. See [Backups](#backups). |
| `BACKUP_GDRIVE_OAUTH_CLIENT_SECRET` | *(empty)* | Mode 1, paired with the above. |
| `BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN` | *(empty)* | Mode 1, paired with the above. |
| `BACKUP_GDRIVE_CREDENTIALS_JSON` | *(empty)* | Mode 2 (Google Workspace) — the raw contents of a service account's JSON key. Only works if `BACKUP_GDRIVE_FOLDER_ID` is a Shared Drive folder. See [Backups](#backups) for the 5-minute setup. |
| `BACKUP_GDRIVE_FOLDER_ID` | *(empty)* | The destination Drive folder's ID (from its URL). Mode 1: any folder in your own Drive. Mode 2: must be inside a Shared Drive, shared with the service account as an Editor. |
| `CURRENCY_CODE` | `NGN` | ISO 4217 currency code applied to every price shown/exported anywhere in the app — the Asset Inventory's per-unit price, the Quotation Catalog's day-rate, and every line/subtotal/VAT/total on a Quotation PDF export. See [Equipment Quotations](#equipment-quotations-quote-to-checkout). |
| `SITE_NAME` | `Snipe-IT Lite` | Brand name shown across the deployment — the on-screen navbar/login brand + browser tab `<title>` (read live from `GET /config/public` on every page load) AND the letterhead printed on the Quotation PDF. One setting rebrands both. |
| `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER` | `false` | Whether a Staff/Customer account browsing the self-service Quotation Catalog can see each pool's available-quantity/in-stock status. `false` (recommended for production) shows them only name, category, and price. A Manager/Admin/Super Admin's own Asset Inventory view is unaffected either way. |
| `SEND_QUOTATION_RECIPIENT_EMAILS` | `true` | Whether a Quotation's own recipient (whoever it's assigned to, else the original requester) gets emailed every time an Admin/Manager changes it (line items, notes, discount, assignment, approval, fulfillment). The in-app bell notification is **always** created regardless of this setting — this only gates the extra email. Only takes effect when `NOTIFICATIONS_ENABLED=true`. |
| `AUDIT_PARTITION_YEARS_AHEAD` | `2` | Postgres-only. How many years of FUTURE `audit_logs` partitions the daily `ensure_audit_log_partitions` Celery Beat job keeps pre-created, so a write never falls through to the DEFAULT catch-all partition just because the calendar rolled over. No-op against non-Postgres databases. See `SRE_STRATEGY.md`. |
| `AUDIT_PARTITION_CHECK_INTERVAL_HOURS` | `24` | How often that same Celery Beat job runs. |

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

### Equipment Quotation permissions

See [Equipment Quotations](#equipment-quotations-quote-to-checkout) above
for the full workflow; the short version of who can do what:

| Action | Who |
|---|---|
| Browse the catalog, build/edit/submit your own draft order, export it as a PDF | Any logged-in User (Staff/Customer/Manager/Admin). |
| See live stock levels in the catalog | Manager/Admin/Super Admin always; Staff/Customer only if `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER=true`. |
| Look up / adjust / assign any submitted Quotation, add a not-in-inventory line, approve, bulk-fulfill | Manager / Admin / Super Admin only (`require_privileged_role`). No department-scoping. |
| Change the global VAT percentage | Super Admin / Admin only (`require_super_admin`). |

### The root admin account

`super_admin` used to be treated completely differently from every other
role: not a row in the `users` table at all, just a fixed identity built
entirely from environment variables. That's changed — the root admin **is**
a real `users` row now, exactly one, bootstrapped once by
`alembic/versions/0002_bootstrap_root_admin.py` during `alembic upgrade
head` in production (or by `database.py`'s `seed_db()` for local/dev/test —
see that migration's and that function's docstrings). What's still fixed
**structurally**, not just by convention, is its identity, not its
credential:

- **Exactly one exists, always.** `POST /users` explicitly rejects `role:
  "super_admin"` as a reserved value no matter who's provisioning the
  account (see `services/user_service.py`'s `RESERVED_ROLES`), and the
  bootstrap migration itself checks for an existing row before ever
  inserting one — anyone who needs the same privileges gets the `admin`
  role instead (see below).
- **It can never be deleted or edited through the app.**
  `DELETE /users/{id}` and `PATCH /users/{id}` both respond with a plain
  404 — not a clearer "that's the root account" message — the instant they
  resolve to this row (see `services/user_service.py`'s
  `is_hidden_root_admin()`), so its existence isn't revealed even to
  someone probing ids directly.
- **It never appears in the User Directory, bulk exports, or the Audit
  Trail.** Every listing/export in `services/user_service.py` and
  `services/audit_service.py` explicitly filters it out — a real,
  fully-auditable database row that nonetheless never shows up in the
  ordinary admin-facing UI ("a secure door for the developer").
- **Its password is a normal database-backed hash, not an environment
  variable.** It logs in through the exact same DB-backed lookup in
  `services/auth_service.py`'s `login()` as any other account, and it can
  be rotated through the exact same self-service `POST
  /auth/update-password` / admin-issued `POST /users/{id}/reset-password`
  flows as any other account — each producing a normal, queryable
  `AuditLog` row. There is no more "edit an env var and restart the
  backend" escape hatch, and no more "this fully disables the login path"
  empty-password special case.
- **Its JWT is re-validated against the database like anyone else's.**
  `deps.get_current_user` re-queries `users` on every request, so
  deactivating this row (an Admin/another Super Admin session, working
  directly against its known id) revokes its access immediately, exactly
  like any other account.

`admin` exists precisely so you're not stuck using this one hardcoded
identity for everyday work: it's a normal, database-backed account with
**every privilege `super_admin` has** (`deps.py`'s `_FULL_ADMIN_ROLES`
groups them together in every permission check), but it can be created,
renamed, given a new password through the app, and soft-deleted like any
other user — see the seeded demo `admin` account in [Demo Login
Credentials](#demo-login-credentials).

#### Two-factor authentication (2FA)

`super_admin` — and, structurally, only `super_admin` today — additionally
requires TOTP (Google Authenticator/Authy/1Password-compatible) 2FA to log
in. `services/auth_service.py`'s `login()` checks `user.role ==
SUPER_ADMIN_ROLE` right after password verification and, instead of
issuing a session, returns one of two challenges:

- **`mfa_setup_required`** (this account has never confirmed a code) — a
  freshly generated secret plus its `otpauth://` provisioning URI, shown
  in the login response body **exactly once**. `POST
  /auth/mfa/setup/confirm` only flips `totp_enabled` to `True` (and
  finally issues the session cookie) once a live code generated from that
  secret is actually verified — see `models.py`'s `User.totp_enabled`
  docstring for why a generated-but-never-confirmed secret deliberately
  doesn't count as "protected yet".
- **`mfa_required`** (already enrolled) — a short-lived, single-purpose
  token; `POST /auth/mfa/verify` exchanges a correct code for the real
  session cookie.

Both challenge tokens are ordinary JWTs signed with the same
`JWT_SECRET_KEY`, just with a `purpose` claim (`mfa_setup` / `mfa_pending`)
and a 5-minute expiry — see `security.py`'s `create_mfa_token()` /
`decode_mfa_token()`. The secret itself is Fernet-encrypted at rest
(`security.py`'s `encrypt_totp_secret()`, key derived from
`JWT_SECRET_KEY`) — never stored in plaintext, and never retrievable again
through any endpoint once the setup screen has shown it once. Wrong 2FA
codes count against the exact same `failed_login_attempts`/`locked_until`
lockout columns a wrong password does, so guessing the 6-digit code is
throttled the same way guessing a password already is, and both `/auth/
mfa/verify` and `/auth/mfa/setup/confirm` sit behind the same IP rate
limiter as `/auth/login` (`main.py`'s `RateLimitMiddleware`).

Because every test's database is freshly seeded, `super_admin` starts
unenrolled in every test run too — see `tests/conftest.py`'s
`auth_headers()` for how the test suite transparently completes
enrollment using `pyotp` so every other fixture didn't need to change, and
`tests/test_mfa.py` for the dedicated coverage (enrollment, wrong codes,
lockout, recovery codes, and that no other role is ever asked for 2FA).

**Recovery (backup) codes.** The moment enrollment is confirmed,
`mfa_setup_confirm()` also issues a batch of 10 single-use recovery codes
(`security.py`'s `generate_recovery_codes()`, format `XXXXX-XXXXX` from a
32-symbol alphabet that excludes easily-confused characters like `0`/`O`
and `1`/`I`), returned in that same response's `recovery_codes` field —
also shown **exactly once**, same as the TOTP secret. The frontend's
`#mfa-recovery-codes-screen` (`index.html`/`js/main.js`) displays them
with a "Download as .txt" option before finally continuing to the
dashboard.

`POST /auth/mfa/verify`'s `code` field accepts EITHER a live TOTP code OR
one of these recovery codes interchangeably —
`security.py`'s `is_recovery_code_format()` tells them apart by shape, so
the frontend never needs to ask which kind the person is submitting. A
live TOTP code, on success, completes the login exactly like a normal
password-only login would. A recovery code is handled differently on
purpose: it means the device holding the TOTP secret is no longer
available, so simply logging the person back in against that same
(now-unreachable) secret would leave them no better off. Instead, a
correct recovery code immediately retires the account's TOTP secret
(`totp_enabled` reset to `False`, the encrypted secret cleared) and hands
back the exact same `mfa_setup_required` shape `login()` returns for a
brand-new account — a fresh secret to enroll on whatever device is at
hand right now (the response also carries `recovery_code_used: true` and
a human-readable `message` so the frontend can explain why it's showing
the setup screen again). No session is granted at this point. The
frontend's `#mfa-setup-screen` (`index.html`/`js/main.js`) picks this up
the same way it handles first-time enrollment, just with that message
displayed instead of the generic copy. Only once `POST
/auth/mfa/setup/confirm` verifies a live code from that *new* secret does
the real session get issued — which also reissues a whole fresh batch of
10 recovery codes, invalidating every remaining code from the old batch
too, since a lost device is reason enough to treat the old codes as
potentially compromised right along with the old secret. The matched
recovery code itself is stamped `used_at` (`models.py`'s `RecoveryCode`)
the moment it's accepted and can never be replayed, whether or not the
re-enrollment that follows is ever completed. Wrong recovery-code
attempts count against the same lockout counters wrong TOTP/password
attempts do.

Lost your authenticator app AND used up your recovery codes? There's
deliberately no self-service reset for that combination — it would defeat
the point of requiring a second factor at all. Recovery means direct
database access: clear that row's `totp_secret_encrypted` and
`totp_enabled` (and its `recovery_codes` rows, via the cascade delete on
`User.recovery_codes` — deleting the `User` row itself isn't needed, just
those columns), which re-triggers a normal `mfa_setup_required` enrollment
on the next login.

An already-logged-in, already-enrolled `super_admin` can invalidate every
existing recovery code and get a fresh batch via `POST
/auth/mfa/recovery-codes/regenerate` (`auth_service.py`'s
`regenerate_recovery_codes()`) — covers "I used most of them" or routine
hygiene. It requires re-entering the current password first (same
pattern as `update_password()`), since it's a sensitive action taken from
inside an already-authenticated session rather than a login step. This
endpoint isn't wired into the "My Profile" UI yet — call it directly (or
build that button next) if you need to regenerate codes today.

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

**Viewing the one-time-generated root admin password:** in production
(`ENVIRONMENT=production`), `alembic upgrade head`'s first run bootstraps
the root admin via `0002_bootstrap_root_admin.py`, which prints a randomly
generated password to **stderr, exactly once** (see that file's docstring)
-- it is never written to the database in plaintext, never logged again
afterward, and there's no "view it later" endpoint or dashboard field,
so you need to actually be watching when the command runs:
- **Local / Docker Compose:** just run the command from a terminal you're
  watching -- `docker compose exec backend alembic upgrade head` (or
  `alembic upgrade head` directly if running the backend without Docker)
  prints straight to that terminal's stderr, which most terminals render
  inline with stdout.
- **Render (free plan):** there's no Background Worker/Job type to run a
  one-off command against, so use the **Shell** tab on the
  `snipeit-lite-web` service in the Render dashboard -- it drops you into
  a live shell inside the running container. Run `cd backend && alembic
  upgrade head` there; the password prints directly in that Shell session
  (not the Logs tab), so keep it open and copy the password immediately --
  it won't reappear if you close the tab before copying it.
- **Other clouds (ECS, Cloud Run, an Azure/GCP VM, etc.):** run it as a
  one-off task/exec against the running container (e.g. `az container exec`,
  `gcloud run jobs execute`, `kubectl exec`) with your terminal attached, so
  stderr streams to you interactively rather than only to a log aggregator
  you'd have to search through afterward (log aggregators sometimes scrub
  or truncate lines that look like secrets, which this one does).
- **This repo's own Azure deployment (`infra/main.bicep`):** the `migrate`
  Container Apps Job runs `alembic upgrade head` for you, and its
  `rootAdminBootstrapPassword` parameter defaults to empty (see
  `infra/main.parameters.example.json`), so the random-password path is
  what runs by default there too. Its console output -- including that
  one-time password -- flows into the shared Log Analytics workspace like
  every other container (see `DEPLOYMENT.md`'s "Monitoring" section):
  ```bash
  az containerapp job execution list --name migrate --resource-group rg-snipeit-lite-prod
  az monitor log-analytics query --workspace <workspace-id> \
    --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'migrate' | order by TimeGenerated desc | take 100"
  ```
  Log Analytics ingestion can lag a minute or two behind the job actually
  running, so if you need it immediately, pass `rootAdminBootstrapPassword`
  as an explicit parameter to `az deployment group create` instead (see
  `infra/main.bicep`'s top-of-file `USAGE` comment) so you choose the
  password yourself rather than reading it back out of logs at all.

**If you missed it (closed the terminal too soon, etc.):** the migration
only inserts the root admin row once -- re-running `alembic upgrade head`
against the same database is a no-op and won't print a new password (see
that file's `already_bootstrapped` guard). Recovery options instead: (1)
if you already have any other Admin/Super Admin account, use its
`POST /users/{id}/reset-password` (Admin-issued reset, no old password
needed -- see `services/user_service.py`'s `reset_user_password()`) once
you know the root admin's user id, or (2) as a last resort, connect
directly to the database and update that row's `password_hash` column to
a fresh Argon2id hash you generate yourself (e.g. `python3 -c "from
security import hash_password; print(hash_password('your-new-password'))"`
from inside the `backend/` folder).

`init_db()` (`Base.metadata.create_all()`) is still safe to leave enabled
for local development — it only creates tables that don't exist yet and
never alters existing ones, so it won't fight with Alembic. In production,
disable it (`AUTO_INIT_DB=false`) and let `alembic upgrade head` be the
only thing that ever changes your schema.

**Current migrations** (`backend/alembic/versions/`, applied in order by
`alembic upgrade head`) — sixteen so far, each one additive-only (see the
"migrate first, only ever ADD" rule in the CI/CD section below):

| Revision | What it does |
|----------|--------------|
| `0001_baseline_schema` | Starting schema — every table this app began with. |
| `0002_bootstrap_root_admin` | Inserts the one hidden `super_admin` row (see [The root admin account](#the-root-admin-account)) — a no-op if it already exists. |
| `0003_outsider_soft_delete` | Adds soft-delete columns to `outsiders`. |
| `0004_outsider_convert_to_user` | Adds `converted_to_user_id`, backing the Outsider → real User conversion flow. |
| `0005_user_convert_to_outsider` | Adds `converted_to_outsider_id`, backing the User → Outsider (revoke login) flow. |
| `0006_purge_deleted` | Adds `purged_at` to `users` and `asset_types` for the Purge Deleted Users/Assets feature. |
| `0007_split_contact_details` | Splits outsider `contact_details` into `email`/`phone_number`; adds `users.phone_number`. |
| `0008_super_admin_totp` | Adds `users.totp_secret_encrypted` / `users.totp_enabled` — see [Two-factor authentication (2FA)](#two-factor-authentication-2fa). |
| `0009_recovery_codes` | Adds the `recovery_codes` table (2FA backup codes). |
| `0010_partition_audit_logs` | Converts `audit_logs` into a native Postgres RANGE-partitioned table (one partition per calendar year, plus a DEFAULT catch-all) — a no-op shape-wise against non-Postgres databases. See `SRE_STRATEGY.md`'s "Audit log partitioning & annual archive" section for the full rationale and the ongoing-maintenance/retirement runbook (`services/audit_partition_service.py`, `tasks/audit_partition_tasks.py`). |
| `0011_password_reset_tokens` | Adds the `password_reset_tokens` table backing the self-service "Forgot password?" email flow — see [Account Security](#account-security-built-in-mostly-invisible-until-you-need-it). |
| `0012_user_company` | Adds the company relationship/fields used by the current user model. |
| `0013_quotation_notifications` | Adds quotation notification persistence used by the in-app quotation notification feed. |
| `0014_pending_approval_sla_nudges` | Adds SLA tracking fields used by pending extension and quotation approval reminders. |
| `0015_asset_department` | Adds department data to asset pools for the current inventory/department model. |
| `0016_quotation_paid_status` | Adds the `paid` quotation status used by the current quotation workflow. |

A fresh install just runs `alembic upgrade head` and applies all sixteen in
order — nothing special to do. `backend/tests/test_migrations.py` runs
this exact `upgrade head` → `downgrade` chain against a throwaway Postgres
database in CI on every push (see `.github/workflows/ci.yml`), so a
migration that doesn't apply or reverse cleanly fails CI before it ever
reaches a real database.

**Going forward, every schema change should be its own NEW migration**
(via `alembic revision --autogenerate -m "description"`) layered on top of
`0016_quotation_paid_status.py` — don't hand-edit an already-applied migration
file once any real data exists anywhere; write a new one instead, even for
a one-line fix.

**When you add a new column to `models.py`, always write a migration for
it** (either by hand or via `alembic revision --autogenerate`) — don't
rely on `create_all()` alone, since it will never alter an *existing*
table that's missing a new column.

## Backups

Everything lives in `backend/services/backup_service.py` (the logic),
`backend/api/backup_api.py` (the `/api/backup/*` routes, Super Admin/Admin
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
is enabled, with no Redis dependency. Backups still *run* on the
`BACKUP_HOURS_UTC` schedule (that setting is about "when", not
"how it's displayed"), but each backup's **filename** embeds its creation
time in `DISPLAY_TIMEZONE` (see [Environment Variables
Reference](#environment-variables-reference)) with the real zone
abbreviation, e.g. `snipeit_backup_20260711_004500_WAT.sql.gz` — matching
the "Created" column right next to it in the System Backups panel (which
independently converts to browser-local time), instead of a raw,
unlabeled UTC timestamp that used to disagree with it by an hour.
Sorting/retention (`BACKUP_RETENTION_COUNT`) are unaffected — they key off
the stored `created_at` (a real UTC instant), never the filename text.

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

## Distributed Tracing (OpenTelemetry)

Structured logs (`LOG_FORMAT=json`, see the Environment Variables
Reference) answer "what happened, on this one request, in this one
process". They can't easily answer "this request was slow — was that
time spent in our own code, in a Postgres query, or waiting on a queued
Celery task?", because that needs the timing of nested operations across
process boundaries, not a flat list of log lines. That's what tracing
(`backend/telemetry.py`) is for: every HTTP request, SQL query, Celery
task, and Redis command becomes a "span" with a start/end time and a
parent, and every structured log line gets tagged with the trace/span
that produced it (`otelTraceID`/`otelSpanID`), so you can jump from "a
user reported error X, here's their `request_id`" straight to the exact
trace that produced it.

**Off by default** — `OTEL_ENABLED=false`, matching every other opt-in
flag in this app. Turning it on costs nothing until you also point it at
somewhere to send spans. `OTEL_SERVICE_NAME` (default
`snipeit-lite-backend`) is the base name spans/logs are tagged with —
it's what shows up in Jaeger's/Application Insights' service picker
(`telemetry.py` also derives a `-db` suffixed name for SQLAlchemy spans
from it), so change it if you're running more than one deployment of
this app and want to tell their traces apart in a shared collector.

### Try it locally in 3 steps (no Azure account needed)

1. Set `OTEL_ENABLED=true` in your `.env` (see `.env.example`).
2. `docker compose --profile tracing up` — this also starts a local
   Jaeger UI (`docker-compose.yml`'s `jaeger` service), which every
   backend/worker/beat container already points at by default.
3. Use the app for a bit (log in, check something out, trigger an
   export), then open **http://localhost:16686**. Pick `backend` (or
   `backend-worker`) from the Service dropdown, click Find Traces, and
   click into any one trace to see its full waterfall — the HTTP
   request span at the top, its child SQL query spans nested underneath,
   and (if it enqueued one) the Celery task span it kicked off, all in
   one continuous timeline even though the task ran in a different
   container.

### Running it in Azure (Application Insights)

`infra/main.bicep` can provision an Application Insights resource for
you — it's **off by default** (`otelAzureMonitorEnabled=false`), same
reasoning as everything else in that file's cost-optimized design (see
its top-of-file comment): nothing is provisioned, and nothing costs
anything, unless you ask for it.

**On the free tier question:** Application Insights includes **5 GB of
free data ingestion per month, per Azure billing account** (not per
resource — it's shared across everything else in that billing account
already using Log Analytics/Application Insights), with the first 90
days of retention included at no extra cost on top of that. Past 5 GB,
it's billed per-GB ingested (roughly a few dollars/GB — check
[Azure's current Application Insights pricing page](https://azure.microsoft.com/en-us/pricing/details/monitor/)
for the exact number, since it does change). For an app at this project's
scale (a small team's asset-tracking tool, not a high-traffic public
service), 5 GB/month of trace data is a generous amount of headroom —
you would need a sustained, meaningfully busy workload to get anywhere
near it. `otelTracesSampleRatio` (default `1.0`, trace everything) is
there to dial down ingestion further if you ever do.

**To turn it on:**

1. In your GitHub repo, set the **Variable** (not Secret —
   see `.github/workflows/infra-deploy.yml`'s own comment on why this
   one isn't sensitive) `OTEL_AZURE_MONITOR_ENABLED=true` and
   `OTEL_ENABLED=true` (Settings → Secrets and variables → Actions →
   Variables tab).
2. Push/re-run `infra-deploy.yml`. This provisions the Application
   Insights resource (reusing the SAME Log Analytics workspace the app
   already provisions for container console logs — no second fixed-cost
   resource) and wires its connection string onto `backend` as a
   Container Apps secret automatically. You never copy/paste a
   connection string yourself.
3. Use the app for a bit, then go monitor it (see below).

**How to actually find your traces once they're flowing (Azure Portal):**

1. Azure Portal → your resource group → the Application Insights
   resource (named `<your-app-name>-<env>-insights` — also printed as
   this deploy's `appInsightsName` output, see "Show outputs" in
   `infra-deploy.yml`'s run).
2. Left sidebar → **Investigate → Transaction search** (or
   **Application Map** for a visual service-to-service view, or
   **Performance** to sort by slowest operations first) — any of these
   let you search/filter and click into an individual trace's full
   waterfall, the same view Jaeger showed locally.
3. For your own queries: left sidebar → **Logs** (this opens a Log
   Analytics query pane, since Application Insights is workspace-based
   here — see `infra/main.bicep`'s `appInsights` resource comment). A
   couple of starting points:

   ```kusto
   // Slowest 20 backend operations in the last 24 hours
   requests
   | where timestamp > ago(24h)
   | top 20 by duration desc
   | project timestamp, name, duration, resultCode, cloud_RoleName

   // Follow one specific trace end-to-end (paste a trace ID from the
   // Transaction search UI, or from a structured log line's otelTraceID)
   union requests, dependencies
   | where operation_Id == "<paste trace id here>"
   | order by timestamp asc
   | project timestamp, itemType, name, duration, cloud_RoleName
   ```

   This is the same Log Analytics workspace/query experience
   `SRE_STRATEGY.md`'s existing alert queries already use for container
   logs — traces just show up as more tables (`requests`, `dependencies`)
   in the same place.

### Fast paths that don't require opening Jaeger at all

The current repository does not contain the older `scripts/tail-errors.sh` or
`scripts/trace-request.sh` helpers that earlier versions of this README
described. Use the platform-native log paths instead:

- **Local / Docker Compose:** `docker compose logs -f backend worker beat`
- **Azure Container Apps:** `az containerapp logs show --name backend --resource-group <resource-group> --tail 500`
- **Azure VM:** `docker compose -f docker-compose.vm.yml logs backend worker beat`

Use the `request_id` or trace identifiers emitted by the backend logs to narrow
the output. This matches the current `backend/logging_config.py`, request-context
middleware, and OpenTelemetry implementation without depending on helper files
that are not present in this repository.

### Using something other than Jaeger/Application Insights

`OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS` (see
`.env.example`) point at any OTLP/HTTP-compatible collector instead —
Grafana Cloud, Honeycomb, a self-hosted otel-collector, etc. — and can be
used alongside Application Insights, not just instead of it, if you want
spans in two places at once.

## Full API Reference

Full interactive docs (auto-generated from the code, with a "Try it out"
button for every endpoint) are always available at `/docs` (Swagger UI)
once the backend is running. This table is the high-level map:

| Method & Path | Who | Purpose |
|---|---|---|
| `POST /auth/login` | anyone | Exchange email/username + password for a JWT. Matches the submitted identifier against EITHER field, case-insensitively (`"T.Okafor@corp.io"` and `"t.okafor@corp.io"` both work — see `services/auth_service.py`'s `login()`). Rate-limited by IP; also enforces per-account lockout after repeated failures. For the Super Admin account specifically, returns `mfa_setup_required`/`mfa_setup_token` (first login ever, no session cookie yet) or `mfa_required`/`mfa_pending_token` (already enrolled) instead of a session — no other role uses 2FA at all. |
| `POST /auth/mfa/setup/confirm` | Super Admin, mid-enrollment | Completes first-time 2FA enrollment: exchanges the `mfa_setup_token` from `POST /auth/login` above plus a valid TOTP code for the real session cookie. |
| `POST /auth/mfa/verify` | Super Admin, already enrolled | Exchanges the `mfa_pending_token` from `POST /auth/login` above plus a valid TOTP code for the real session cookie. Wrong codes are rejected and repeated failures lock the account out, same as a wrong password would. |
| `POST /auth/mfa/recovery-codes/regenerate` | Super Admin (self) | Invalidates every existing one-time recovery code and issues ten brand-new ones — requires re-confirming the current password first. |
| `POST /auth/logout` | logged in | Clears the `HttpOnly` session cookie set at login (see [Security Model](#security-model)). |
| `GET /auth/me` | logged in | "Who am I?" — fresh profile data for the "My Profile" window. |
| `PATCH /auth/me` | self (any role) | Self-service: rotate your own name/username/email. Requires re-confirming `current_password` first; a summary email goes to the pre-change (and, if email itself changed, the new) address. Self-only by design — the one path the root admin account has to correct its own details, since no Admin/Super Admin can edit that hidden row. |
| `POST /auth/update-password` | self or Super Admin/Admin | Change a password (self-service requires the current password; a Super Admin resetting someone else's does not). |
| `POST /auth/forgot-password` | anyone (unauthenticated) | Self-service password recovery: emails a single-use reset link to the matched account's registered email address if `identifier` (email or username) matches a real, active account. Always returns the same generic response either way, so it can't be used to enumerate accounts. Works for any account, including the root admin. |
| `POST /auth/reset-password` | anyone holding a valid token | Redeems a still-valid, not-yet-used token from the emailed link (`PASSWORD_RESET_TOKEN_EXPIRY_MINUTES`) for a brand-new password, and clears any account lockout as a side effect. |
| `GET /assets` | logged in | List asset pools. TRUE server-side pagination + search — `?limit=&offset=&search=` (searches pool name). |
| `GET /assets/deleted` | Super Admin / Admin | List soft-deleted asset pools, so one can be found to restore or purge. TRUE server-side pagination + search, same as `GET /assets`. |
| `POST /assets` | Super Admin / Admin | Create a new pool. |
| `GET /assets/{id}/details` | logged in | Full pool detail: stock breakdown, active checkouts, isolated units. |
| `PUT /assets/{id}/quantity` | Super Admin / Admin | Adjust total capacity. |
| `PUT /assets/{id}/name` | Super Admin / Admin | Rename a pool. |
| `PUT /assets/{id}/category` | Super Admin / Admin | Change a pool's category. |
| `PUT /assets/{id}/price` | Super Admin / Admin | Change a pool's per-unit price (see `CURRENCY_CODE`). |
| `DELETE /assets/{id}` | Super Admin / Admin | Soft-delete a pool. |
| `POST /assets/{id}/restore` | Super Admin / Admin | Reverses a soft delete: returns the pool to active inventory. |
| `POST /assets/{id}/purge` | Super Admin / Admin | Permanently anonymizes a soft-deleted pool's name so it's free to be reused by a new pool. Irreversible — unlike restore, there's no undo. |
| `POST /assets/{id}/exception` | Super Admin / Admin | Flag a serial as under repair/stolen. |
| `POST /assets/{id}/exception/{eid}/recall` | Super Admin / Admin | Return an isolated unit to service. |
| `POST /assets/{id}/checkin` | Super Admin / Admin | Reconcile newly-found stock. |
| `POST /assets/{id}/checkout_advanced` | Super Admin / Admin / Manager | Dispatch units to a Staff member, a linked Customer account, or an ad-hoc Outsider. |
| `POST /assets/import` | Super Admin / Admin | Bulk-create pools from a CSV (max 5 MiB, columns `name`, `total_quantity`, optional `category`). |
| `GET /assets/categories` | logged in | Distinct list of categories currently set on any active pool — powers the Asset Inventory Export button's per-category options. |
| `GET /assets/export` | logged in | Download the Asset Inventory table itself (one row per pool) as `?format=csv\|pdf`, optionally narrowed with `?category=` (omit, or pass `all`, for every pool). |
| `GET /users` | Super Admin / Admin / Manager | Directory listing (both Managers and Admins see the entire directory — no department-scoping). TRUE server-side pagination + search — `?limit=&offset=&search=` (searches name, email, role, department, department_role). |
| `POST /users` | Super Admin / Admin / Manager | Provision a new login. |
| `GET /users/me/items` | logged in | Self-service: my own checked-out items. |
| `GET /users/me/items/export` | logged in | Self-service download of the above as `?format=csv` or `?format=pdf`. |
| `GET /users/{id}/items` | Super Admin / Admin / Manager | Someone else's custody ledger (unrestricted for Managers too). |
| `GET /users/{id}/items/export` | Super Admin / Admin / Manager | Download one specific user's custody ledger (CSV/PDF). |
| `PATCH /users/{id}` | Super Admin / Admin / Manager | Edit an account's name/username/email (a Manager may only target Staff/Customer accounts — enforced server-side). |
| `GET /users/export` | Super Admin / Admin / Manager | Bulk download of every active checkout across every user, system-wide, for both roles (CSV/PDF). |
| `DELETE /users/{id}` | Super Admin / Admin | Soft-delete an account. |
| `POST /users/{id}/convert-to-outsider` | Super Admin / Admin / Manager | Revoke an account's login access and turn it into an Ad-Hoc (no-login) profile instead — the reverse of `POST /outsiders/{id}/convert-to-user` below. A Manager may only target Staff/Customer accounts, same ceiling as account provisioning. |
| `POST /users/{id}/reset-password` | Super Admin / Admin | "Forgot password" recovery: set a brand-new password for another user's account, no current password required. |
| `GET /users/deleted` | Super Admin / Admin | List soft-deleted accounts. TRUE server-side pagination + search — `?limit=&offset=&search=`, same fields as `GET /users`. |
| `POST /users/{id}/restore` | Super Admin / Admin | Undo a soft-delete: re-enables login and returns the account to the User Directory. |
| `POST /users/{id}/purge` | Super Admin / Admin | Permanently anonymizes a soft-deleted account's email/username so they're free to be reused by a new account. Irreversible — unlike restore, there's no undo. |
| `GET /outsiders` | Super Admin / Admin / Manager | Ad-Hoc directory listing. TRUE server-side pagination + search — `?limit=&offset=&search=` (searches name, contact details, company). |
| `GET /outsiders/{id}/items` | Super Admin / Admin / Manager | An outsider's custody ledger. |
| `PATCH /outsiders/{id}` | Super Admin / Admin / Manager | Edit an ad-hoc individual's name/contact details/company. |
| `DELETE /outsiders/{id}` | Super Admin / Admin / Manager | Soft-delete an ad-hoc individual's profile; blocked while it still has items in active custody. |
| `POST /outsiders/{id}/convert-to-user` | Super Admin / Admin / Manager | Turn an ad-hoc individual into a real, log-in-capable user account — migrates their active/returned checkouts and any Quotation assignment along with them (see `test_outsider_convert_to_user.py` under [Testing Your Changes](#testing-your-changes)). Subject to the same Manager role ceiling as provisioning any other new account. |
| `GET /outsiders/{id}/items/export` | Super Admin / Admin / Manager | Download one specific outsider's custody ledger (CSV/PDF). |
| `GET /outsiders/export` | Super Admin / Admin / Manager | Bulk download of every active checkout across every ad-hoc individual (CSV/PDF). |
| `POST /checkouts/{id}/return` | Super Admin / Admin / Manager | Process a (partial or full) return. |
| `GET /checkouts/overdue` | Super Admin / Admin / Manager | Dashboard alert feed of overdue checkouts, system-wide for both roles (no department-scoping). |
| `GET /checkouts/due-soon` | Super Admin / Admin / Manager | Dashboard alert feed of checkouts due within `DUE_SOON_REMINDER_DAYS` but not yet overdue — "a reminder before something goes overdue," system-wide for both roles. |
| `POST /checkouts/{id}/extension-requests` | logged in | Request more time on your own active checkout (or, if Manager/Admin/Super Admin, on behalf of an Ad-Hoc Individual). Creates a **pending** request — does not change the due date by itself. |
| `GET /checkouts/extension-requests` | Super Admin / Admin / Manager | List extension requests — `?status=pending\|approved\|denied&limit=&offset=` (Managers see every request, same as Admin/Super Admin). |
| `GET /checkouts/my-extension-decisions` | logged in | Self-service: the caller's own extension requests decided (approved/denied) in the last 14 days — powers the in-app "extension request update(s)" banner on every dashboard. |
| `POST /checkouts/extension-requests/{id}/decision` | Super Admin / Admin / Manager | Approve or deny a pending request — `{approve, override_due_date?, note?}`. Approving is what actually updates the checkout's due date. |
| `POST /checkouts/{id}/extend` | Super Admin / Admin / Manager | Grant more time **directly** — `{new_due_date, reason?}` — no request/decision round trip. Used by the Custody Ledger drawer's "Extend" button. |
| `POST /checkouts/bulk-extend` | Super Admin / Admin / Manager | Apply one new due date to many active checkouts at once — the Custody Ledger drawer's "Bulk Extend Selected" action, reusing the same checkbox selection as Bulk Process Returns. |
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
| `GET /config/public` | logged in | Non-secret config the frontend needs before rendering the catalog/cart — `{currency_code, show_stock_to_staff_customer}`. |
| `GET /assets/catalog` | logged in | The self-service Quotation Catalog — every active asset pool, shaped by role + `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER`. |
| `GET /quotations/me` | logged in | The caller's own open draft order, with live-computed totals. |
| `GET /quotations/me/history` | logged in | Every Quotation the caller has formally submitted. |
| `GET /quotations/me/{id}` | logged in | Full detail for one of the caller's own submitted Quotations. |
| `GET /quotations/me/{id}/export` | logged in | Self-service PDF export of one of the caller's own submitted (or assigned-to-them) Quotations by ID — the "My Quotes" history equivalent of `GET /quotations/{id}/export` below. |
| `PUT /quotations/me/{id}/items/{item_id}` | logged in | Requester adjusts a quantity on their own quotation while still "submitted". |
| `DELETE /quotations/me/{id}/items/{item_id}` | logged in | Requester removes a line from their own quotation while still "submitted". |
| `POST /quotations/me/{id}/items` | logged in | Requester/assignee adds another catalog asset to their own quotation while still "submitted" — nested under `/quotations/me/...` so it never collides with the Admin/Manager-only `POST /quotations/{id}/items` below. |
| `POST /quotations/items` | logged in | Add (or update) an item on the caller's own draft cart. |
| `PUT /quotations/items/{id}` | logged in | Update a quantity on the caller's own draft cart. |
| `DELETE /quotations/items/{id}` | logged in | Remove an item from the caller's own draft cart. |
| `POST /quotations/submit` | logged in | Finalize the caller's draft — stamps it with a Quotation reference number (e.g. `QT-000001`). |
| `GET /quotations/export` | logged in | Download the caller's current draft order as a PDF. |
| `GET /quotations` | Super Admin / Admin / Manager | The "Quotes" tab — every submitted Quotation, `?search=&status=&limit=&offset=`. |
| `POST /quotations` | Super Admin / Admin / Manager | Start a brand-new Quotation on someone's behalf (starts empty). |
| `GET /quotations/fulfillment-queue` | Super Admin / Admin / Manager | Every Approved / Ready for Pickup Quotation, oldest first — the Fulfillment Drawer's data. |
| `GET /quotations/{id}` | Super Admin / Admin / Manager | Full detail for one Quotation by numeric ID. |
| `DELETE /quotations/{id}` | Super Admin / Admin only | Permanently delete a submitted or approved Quotation (and its lines). Refused once fulfilled — stricter gate than every other row on this table, which a Manager can also perform. |
| `PUT /quotations/{id}` | Super Admin / Admin / Manager | Update a Quotation's internal notes. |
| `PUT /quotations/{id}/discount` | Super Admin / Admin / Manager | Set the discount percentage (0-100) applied to this Quotation's subtotal, before VAT. Editable right up until the quote is fulfilled, same as its line items. |
| `POST /quotations/{id}/items` | Super Admin / Admin / Manager | Add/update a line on someone else's submitted Quotation. |
| `PUT /quotations/{id}/items/{item_id}` | Super Admin / Admin / Manager | Update a quantity on someone else's submitted Quotation. |
| `DELETE /quotations/{id}/items/{item_id}` | Super Admin / Admin / Manager | Remove a line from someone else's submitted Quotation. |
| `POST /quotations/{id}/outsourced-items` | Super Admin / Admin / Manager | Add a "not currently in inventory" line with its own name/price. |
| `DELETE /quotations/{id}/outsourced-items/{item_id}` | Super Admin / Admin / Manager | Remove a previously-added outsourced line. |
| `POST /quotations/{id}/assign` | Super Admin / Admin / Manager | Assign (or clear the assignment of) a Quotation to a user. |
| `POST /quotations/{id}/approve` | Super Admin / Admin / Manager | Flip a submitted Quotation to "approved" and lock it against further edits. |
| `POST /quotations/{id}/checkout` | Super Admin / Admin / Manager | The Fulfillment Drawer's bulk physical checkout — turns every line into a real `AssetCheckout`. |
| `GET /quotations/{id}/export` | Super Admin / Admin / Manager | PDF export of any Quotation by ID. |
| `GET /settings/vat` | logged in | The current global VAT percentage. |
| `PUT /settings/vat` | Super Admin / Admin | Change the global VAT percentage applied to every Quotation immediately. |
| `GET /settings/digest-recipients` | Super Admin / Admin | The current list of email addresses that receive the daily overdue/due-soon digest (see [Due-Date Extensions & Notifications](#due-date-extensions--notifications)). |
| `PUT /settings/digest-recipients` | Super Admin / Admin | Replace the entire digest recipients list. Takes effect on the next scheduled digest run — no restart needed. |
| `GET /healthz` | anyone | Liveness check — process is up and answering HTTP, no DB dependency. `backend/Dockerfile`'s own `HEALTHCHECK` instruction already points here (so plain `docker compose ps`/`docker ps` report it too), and `infra/main.bicep` points Azure Container Apps' liveness probe here as well. |
| `GET /readyz` | anyone | Readiness check — queries the DB and compares its Alembic revision against what this build expects; `200` + `{"ready": true, ...}` once the schema matches, `503` + `{"ready": false, "reason": "..."}` otherwise. Point orchestrator readiness probes (e.g. Azure Container Apps' `Readiness` probe) here, not at `/healthz`. |

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
- `health_check()` — `GET /healthz`. Liveness only; no DB dependency.
- `readiness_check()` — `GET /readyz`. Readiness; calls
  `database.py`'s `get_schema_status()` to confirm the live DB's Alembic
  revision matches this build's code before reporting `200`. This is
  what Azure Container Apps' readiness probe (see `infra/main.bicep`)
  actually polls before shifting traffic to a new replica.
- Also where the middleware stack (`UnhandledExceptionMiddleware`,
  `RateLimitMiddleware`, `RequestContextMiddleware`, `CORSMiddleware`,
  `SecurityHeadersMiddleware`)
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
  `JWT_SECRET_KEY`. There is no equivalent Super Admin check anymore --
  its password lives in the database as a normal hash, not an env var
  (see [The root admin account](#the-root-admin-account)), so there's
  nothing here to validate at startup.

#### `backend/database.py`
- `init_db()` — `Base.metadata.create_all()`; creates any tables that
  don't exist yet.
- `get_db()` — FastAPI dependency that yields a SQLAlchemy `Session` and
  always closes it afterwards.
- `seed_db()` — inserts demo accounts/asset pools **only if the database
  is empty**, including a local/dev/test root admin row (`_root_admin_demo_row()`,
  well-known demo password -- see [The root admin
  account](#the-root-admin-account); production gets its root admin from
  the `0002_bootstrap_root_admin.py` migration instead, with a randomly
  generated password).

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
  custom_fields, optional `category` describing which internal team the
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
- `class AppSetting` — tiny runtime-editable key/value store (unlike
  `config.py`, which needs a restart to change) — today holds exactly one
  key, `vat_percent`. See `services/quotation_service.py`'s
  `get_vat_percent()`/`set_vat_percent()`.
- `class Quotation` — a staff/customer account's self-service equipment
  rental request (the "cart"). Lifecycle: `draft` → `submitted` →
  `approved` → `fulfilled`. Stock is only ever deducted at the final
  `fulfilled` step. See [Equipment
  Quotations](#equipment-quotations-quote-to-checkout).
- `class QuotationItem` — one line of a Quotation (asset, quantity,
  start/due date); always priced live off the current `AssetType.price`,
  never snapshotted.
- `class QuotationOutsourcedItem` — a Manager/Admin-only Quotation line
  for equipment not currently in the Asset Inventory catalog, with its
  own one-off `unit_price` (unlike `QuotationItem`, this price IS
  snapshotted, since there's no catalog row to join back to).

#### `backend/security.py`
- `hash_password(plain_password)` — Argon2id hash of a plaintext password.
- `verify_password(plain_password, hashed_password)` — constant-time
  comparison against a stored hash.
- `validate_password_strength(password)` — enforces the password
  complexity policy; raises with a specific reason on failure.
- `create_access_token(user)` — issues a signed JWT for a logged-in user.
- `decode_access_token(token)` — verifies signature + expiry and returns
  the token's claims.
- `SUPER_ADMIN_ROLE` — the reserved `role` string (`"super_admin"`)
  identifying the root admin's real `users` row (see [The root admin
  account](#the-root-admin-account)). `SUPER_ADMIN_EMAIL` is its derived
  placeholder email (`{SUPER_ADMIN_USERNAME}@local`), used only to build
  the seed/bootstrap row -- there's no `SUPER_ADMIN_ID`,
  `SUPER_ADMIN_PASSWORD_HASH`, or `super_admin_principal()` anymore: the
  root admin logs in through the exact same database lookup as any other
  account, so it needs no JWT-issuing stand-in of its own.

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
- **`rate_limit.py`** — `class RateLimitMiddleware`, a Redis-backed
  fixed-window limiter (shared `INCR`/`EXPIRE` counters keyed by client
  IP, on the same Redis instance as the Celery broker) applied only to
  `POST /auth/login` — Redis-backed specifically so the limit is enforced
  consistently across every `backend` replica when scaled, instead of
  each replica keeping its own independent count. See
  [`DEPLOYMENT.md`](DEPLOYMENT.md)'s load balancing section.
- **`security_headers.py`** — `class SecurityHeadersMiddleware` stamps
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and a
  restrictive `Permissions-Policy` onto every response.
- **`error_handling.py`** — `class UnhandledExceptionMiddleware`, the
  last-resort safety net for a genuinely unanticipated exception (a bug,
  an unexpected third-party error, anything nothing else already caught
  with its own `try/except`). Logs the full traceback — automatically
  tagged with that request's correlation ID, same as every other log
  line (see `request_context.py` above) — and returns the SAME
  `{"detail": ...}` JSON shape every other error in this API uses, plus
  a `request_id` field so a user/support agent can hand back an ID that
  maps straight to the matching log line. Registered as the *innermost*
  middleware layer (see `main.py`'s "MIDDLEWARE STACK" comment) rather
  than as `@app.exception_handler(Exception)` — that alternative gets
  routed by Starlette to the outermost `ServerErrorMiddleware`, *outside*
  `CORSMiddleware`, which would silently strip CORS headers from every
  unhandled 500 and leave the browser reporting an opaque network error
  instead. See that file's own module docstring for the full mechanism.
- **`clean_urls.py`** — `class CleanUrlsMiddleware`, only registered when
  `settings.SERVE_FRONTEND` is on (the free-tier Render single-service
  deployment shape — see `Dockerfile.render`/`render-start.sh` — where
  FastAPI itself serves the built frontend via a `StaticFiles(html=True)`
  mount, not nginx). Rewrites clean URLs like `/admin` to the actual
  `admin.html` file *before* that mount ever sees the request (no redirect,
  no URL flash in the address bar), and 301-redirects anyone still hitting
  an old `/admin.html`-style link to its clean equivalent. In the
  nginx-fronted deployment shape (`docker-compose.yml` / most cloud
  deployments), this middleware isn't loaded at all — the identical
  rewrite/redirect behavior lives in `nginx/default.conf.template`'s
  `location /` and `location ~ ^/(.+)\.html$` blocks instead, so both
  deployment shapes present identical URLs to the browser either way. See
  that file's own module docstring for the full explicit `CLEAN_URL_MAP`.

### Backend — API Routes (`backend/api/`)

- **`api/auth_api.py`** — `login`, `mfa_setup_confirm`, `mfa_verify`,
  `logout`, `get_my_profile`, `update_password`,
  `mfa_recovery_codes_regenerate`, `forgot_password`, `reset_password`,
  `update_my_identity` (the `PATCH /auth/me` behind "My Profile"'s
  name/username/email editing). `_resolve_frontend_base_url()` derives
  the mailed password-reset link's base URL from the actual incoming
  request (checked against `CORS_ORIGINS`) rather than a hardcoded
  setting, so it can never be spoofed into pointing a reset link at an
  attacker's own site.
- **`api/assets_api.py`** — `create_asset_type`, `list_assets`,
  `get_asset_details`, `update_asset_quantity`, `delete_asset_type`,
  `flag_asset_exception`, `recall_asset_exception`, `checkin_asset`,
  `checkout_advanced`, `import_assets_from_csv`.
- **`api/users_api.py`** — `create_user`, `get_users`, `get_deleted_users`,
  `get_my_assigned_items`, `export_my_assigned_items`,
  `export_all_users`, `get_user_assigned_items`,
  `export_user_assigned_items`, `delete_user`, `reset_user_password`,
  `restore_user`.
- **`api/outsiders_api.py`** — `get_outsiders`, `export_all_outsiders`,
  `get_outsider_assigned_items`, `export_outsider_assigned_items`.
- **`api/checkouts_api.py`** — `return_checkout`, `get_overdue_checkouts`,
  `get_due_soon_checkouts`, `request_extension`, `get_extension_requests`,
  `decide_extension_request`, `extend_checkout`.
- **`api/audit_api.py`** — `get_audit_logs`, `export_audit_logs`.
- **`api/backup_api.py`** — `backup_status`, `list_backups`,
  `create_backup_now`, `download_backup`, `delete_backup`,
  `restore_backup`, `restore_backup_upload`. Thin router — see
  `services/backup_service.py` for the actual `pg_dump`/`psql`/Google
  Drive implementation.
- **`api/quotations_api.py`** — `get_public_config`, `get_asset_catalog`;
  self-service cart routes (`get_my_quotation`,
  `get_my_quotation_history`, `add_quotation_item`,
  `update_quotation_item`, `remove_quotation_item`, `submit_quotation`,
  `export_quotation`); Admin/Manager "Quotes" tab routes
  (`list_quotations`, `create_quotation`, `get_fulfillment_queue`,
  `get_quotation`, `update_quotation`, `admin_add_item`/`admin_update_item`/
  `admin_remove_item`, `add_quotation_outsourced_item`,
  `assign_quotation`, `approve_quotation`, `checkout_quotation`,
  `export_quotation_admin`); and the global `get_vat_setting`/
  `update_vat_setting`. See [Equipment
  Quotations](#equipment-quotations-quote-to-checkout).
- **`api/notifications_api.py`** — `get_digest_recipients`/
  `update_digest_recipients` (the daily digest's admin-editable recipient
  list, Super Admin/Admin only).

### Backend — Services (`backend/services/`) — the business logic layer

- **`services/asset_service.py`** — `create_asset_type`, `list_assets`
  (now accepts a `search` param, narrowing to pool name — see
  `services/search_utils.py`), `get_asset_details`, `update_asset_quantity`,
  `delete_asset_type`, `flag_asset_exception`, `recall_asset_exception`,
  `checkin_asset`, `checkout_advanced`, `import_assets_from_csv`.
  `MAX_CSV_UPLOAD_BYTES` caps upload size.
- **`services/auth_service.py`** — `login(db, req)` verifies credentials
  against the database (the root admin is a real row like anyone else --
  see [The root admin account](#the-root-admin-account)), enforces
  per-account lockout, and issues a JWT. `_DUMMY_PASSWORD_HASH` keeps the
  "no such account" response timing-consistent with "wrong password";
  `get_profile(db, current_user)` backs `GET /auth/me`; `update_password`
  changes a password and clears any lockout as a side effect, and works
  for the root admin exactly the same way as any other account.
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
  shared CSV/PDF machinery every exporter in the app uses (fonts loaded
  from `backend/assets/fonts/` so non-ASCII currency symbols render).
- **`services/backup_service.py`** — `create_backup()` (`pg_dump` to a
  gzip file under `BACKUP_DIR`, optional Google Drive upload),
  `list_backups()`/`get_status()`, `restore_from_file()` (takes an
  automatic pre-restore safety backup first), the daemon-thread scheduler
  that fires at each hour in `BACKUP_HOURS_UTC`, and the Google Drive
  upload helpers for both OAuth modes. See [Backups](#backups).
- **`services/quotation_service.py`** — the largest service module in the
  app; see [Equipment Quotations](#equipment-quotations-quote-to-checkout)
  for the full picture. Key functions: `list_catalog()` (role- and
  `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER`-shaped), `_get_or_create_draft()`
  (lazily creates a user's standing cart), `add_item()`/
  `update_item_quantity()`/`remove_item()` (self-service cart),
  `submit_my_quotation()` (stamps a `QT-######` reference number),
  `admin_create_quotation()`/`assign_quotation()`/`approve_quotation()`
  (the "Quotes" tab and quote-to-checkout state machine),
  `bulk_checkout_quotation()` (Fulfillment Drawer — turns every line into
  a real `AssetCheckout`, deducting stock only at this step),
  `admin_add_outsourced_item()` (not-in-inventory lines),
  `get_vat_percent()`/`set_vat_percent()` (the `AppSetting` row), and the
  PDF builders `export_quotation_pdf()`/`export_quotation_pdf_by_id()`.
- **`services/stock.py`** — `recalculate_asset_stock(db, asset)`, the
  single shared formula for `Available = Total − Outbound − Isolated`,
  called after every mutation that could change it (including
  `bulk_checkout_quotation()` above).

### Backend — Async Workers (Celery) (`backend/celery_app.py`, `backend/tasks/`)

Two different processes share one Celery app — see `celery_app.py`'s
module docstring for the full producer/consumer split: the `backend`
container only ever enqueues jobs (`.delay(...)`) and returns
immediately; the `worker` container is the only thing that actually runs
them, completely out-of-band from any HTTP request.

- **`celery_app.py`** — the shared `celery_app` instance (Redis as both
  broker and result backend), its serialization/result-TTL config, and
  `beat_schedule` — wires `tasks.send_overdue_notifications` to run daily
  at `OVERDUE_DIGEST_HOURS_UTC` AND `tasks.send_due_soon_reminders`
  to run daily at `DUE_SOON_DIGEST_HOURS_UTC` (both `crontab` schedules,
  not plain intervals — a fixed UTC clock time, same idea as the Backups
  section's `BACKUP_HOURS_UTC`), plus `tasks.escalate_pending_extension_requests`
  and `tasks.escalate_pending_quotations` (see [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications)'s SLA-nudges item),
  which run on a plain `timedelta(minutes=APPROVAL_SLA_CHECK_INTERVAL_MINUTES)`
  interval instead — "how promptly after crossing the SLA line" is what
  matters there, not a specific time of day, same reasoning as the
  audit-partition check's own interval schedule. `-B` embeds Celery Beat
  directly inside the `worker` container's own
  process (see `docker-compose.yml`'s `worker` service) rather than
  running it as a separate container. Safe to scale to multiple replicas
  of an embedded worker+beat process (Render's/Azure's cost-optimized
  deployment shapes) without any of them double-firing a scheduled job —
  RedBeat (`redbeat_redis_url`/`beat_scheduler` in this same file's
  config) stores a distributed lock in Redis so only one replica is ever
  the active scheduler at a time; see that config block's own comment
  for the full mechanism.
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
  system-wide digest for the admin-configured **Digest Recipients** list
  (`GET`/`PUT /settings/digest-recipients`) + `ADMIN_NOTIFICATION_EMAILS`
  — no longer every Manager/Admin account automatically (see [Due-Date
  Extensions & Notifications](#due-date-extensions--notifications)).
  `send_due_soon_reminders()` — "a reminder before something goes
  overdue" — is its proactive counterpart: the identical shape of
  individual-holder-reminder + Digest Recipients-audience digest, just
  for checkouts due within `DUE_SOON_REMINDER_DAYS` instead of ones
  already overdue (`_due_soon_query()`/`_format_line()` are shared
  helpers used by both jobs).
- **`tasks/sla_tasks.py`** — the two SLA-nudge scheduled jobs from the
  `celery_app.py` bullet above:
  `escalate_pending_extension_requests()`/`escalate_pending_quotations()`
  find every `ExtensionRequest`/`Quotation` row still stuck `pending`/
  `submitted` past its own `EXTENSION_REQUEST_SLA_HOURS`/
  `QUOTATION_SLA_HOURS`, and email ONE combined digest per queue to the
  same Digest Recipients + `ADMIN_NOTIFICATION_EMAILS` audience as
  `notification_tasks.py`'s digests above. `_due_for_escalation()` is a
  small shared helper both jobs call: past-SLA AND (never nudged before
  OR past `APPROVAL_SLA_ESCALATION_REPEAT_HOURS` since the last nudge) —
  each row's own `sla_last_reminded_at` column (see `models.py`) is what
  makes the cooldown per-row instead of re-spamming every single
  `APPROVAL_SLA_CHECK_INTERVAL_MINUTES` tick forever.

### Backend — Schemas (`backend/schemas/`)

Pure Pydantic request/response models, no logic:
- **`schemas/auth_schema.py`** — `LoginRequest`, `PasswordUpdateRequest`
  (enforces password strength via a `field_validator`).
- **`schemas/assets_schema.py`** — asset/checkout request bodies, including
  the server-side due-date min/max check.
- **`schemas/users_schema.py`** — `UserCreateRequest` (also enforces
  password strength); `UserPasswordResetRequest` (the admin-reset body —
  just `new_password`, same strength `field_validator`, no
  `current_password` since the whole point is not needing the old one).
- **`schemas/checkouts_schema.py`** — `ReturnRequest`;
  `ExtensionRequestCreate` (self-service or on-behalf-of-Outsider request
  body); `ExtensionDecisionRequest` (approve/deny, with an optional
  `override_due_date`/`note`); `DirectExtensionRequest` (the "Extend"
  button's request body — same shape as `ExtensionRequestCreate`, but
  skips the request/approval workflow entirely).
- **`schemas/quotations_schema.py`** — `QuotationItemCreate`/
  `QuotationItemQuantityUpdate` (self-service cart line bodies);
  `QuotationOutsourcedItemCreate` (Admin/Manager not-in-inventory line);
  `QuotationCreateRequest`/`QuotationMetaUpdate`/`QuotationAssignRequest`
  (the "Quotes" tab); `VatUpdateRequest` (the global VAT setting).
- **`schemas/notifications_schema.py`** — `DigestRecipientsUpdateRequest`
  (the daily digest's admin-editable recipient list; validates/normalizes
  each address without an `email-validator` dependency — see the file's
  own comments).

### Backend — Scripts (`backend/scripts/`)

- **`scripts/gdrive_oauth_setup.py`** — a one-off, interactive script you
  run manually on your own machine (never inside Docker/CI) to set up
  Google Drive backup uploads. Walks you through exchanging a downloaded
  OAuth client JSON for a refresh token and prints the
  `BACKUP_GDRIVE_OAUTH_*` values to paste into `.env`. See
  [Backups](#backups) for the full walkthrough.

### Frontend — Core (`frontend/js/`)

- **`js/api.js`** — `apiRequest(path, options)`, the ONE function that
  calls `fetch()`; attaches the JWT header, parses JSON, throws a
  normalized `Error` on failure. `formatErrorDetail(detail)` turns
  FastAPI's `detail` field (string OR validation-error array) into one
  readable message.
- **`js/auth.js`** — `parseJwt`, `getSession`, `logout`, `login`,
  `currentPageName`, `checkAccess`, `redirectByUserRole`,
  `startIdleWatchdog` (auto-logout after inactivity).
- **`js/auth-guard.js`** — a tiny, render-blocking, non-module script
  loaded first in every role-restricted dashboard's `<head>` (paired with
  `css/auth-guard.css`). Decodes the JWT and checks it against the
  `data-allowed-roles` attribute on its own `<script>` tag *before* the
  page paints, so an unauthorized visitor never sees a flash of the real
  dashboard before being redirected to `index.html`. `js/auth.js`'s
  `checkAccess` re-checks the same rule again once the full module bundle
  has loaded, as defense-in-depth.
- **`js/theme.js`** — `getTheme`, `setTheme`, `toggleTheme`,
  `initThemeToggle` (wires the navbar's dark/light toggle button, persists
  the choice to `localStorage`).
- **`js/theme-init.js`** — a tiny, synchronous, non-module script loaded
  before `css/tailwind.css` — applies the saved (or OS-preferred) theme
  class to `<html>` before first paint, so there's no flash of the wrong
  theme on load. `js/theme.js` above handles the toggle button itself;
  this file only handles the initial, pre-paint choice.
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
- **`quotation.js`** — the biggest component file in the app; see
  [Equipment Quotations](#equipment-quotations-quote-to-checkout) for the
  feature itself. Self-service: `renderCatalogTable` (Asset Catalog,
  shaped by `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER`), `renderMyQuotation`/
  `renderMyQuotationHistory`/`renderMyQuoteDetail` ("My Order" cart +
  submitted-quote history), `switchQuotationTab`/`initQuotationSwipeNav`
  (Catalog ↔ My Order tabs, with swipe support on mobile). Admin/Manager
  "Quotes" tab: `renderQuotesTable`/`changeQuotesPage` (server-side
  pagination, same pattern as `audit.js`), `renderQuoteDetail`/
  `refreshQuoteDetail`, `selectQuoteDetailAsset`/`clearQuoteDetailAsset`
  (adding a catalog item to someone else's quote), `openCreateQuoteModal`,
  `toggleQuoteAssignAdhocForm` (assign to a linked user vs. an Ad-Hoc
  Individual), `renderFulfillmentQueue`/`updateFulfillmentSelection`/
  `toggleSelectAllFulfillment` (the Fulfillment Drawer's bulk checkout
  selection). Shared: `quotationStatusBadge` (draft/submitted/approved/
  fulfilled badge styling).
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
3. **`schemas/assets_schema.py`** — add `notes: Optional[str] = None` to
   whichever request model creates/updates a pool.
4. **`services/asset_service.py`** — read `payload.notes` and set it on
   the `AssetType` row in `create_asset_type()`/`update_asset_quantity()`
   (or wherever makes sense).
5. **`api/assets_api.py`** — usually needs NO change at all, since routes
   just pass the whole validated `payload` object through to the service.
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

## Resilience, zero-downtime and performance validation

The P1/P2 hardening suite is documented in [`P1_P2_RESILIENCE.md`](P1_P2_RESILIENCE.md).

Quick local checks:

```bash
./scripts/chaos-test.sh
python scripts/load-test.py --url http://localhost:8080/healthz --requests 500 --concurrency 25
```

The chaos test verifies Redis, Celery worker, ErrorBeacon and PostgreSQL failure/recovery behavior. The load runner reports throughput and p50/p95/p99 latency. Safe browser GETs also retry brief 502/503/504/network failures, while mutations are deliberately never retried automatically.

## Testing Your Changes

### Automated test suite (`backend/tests/`)

The primary way to verify a backend change: `pytest`, `TestClient`, and a
fresh throwaway SQLite database created and torn down automatically for
every single test function (see `backend/tests/conftest.py`'s module
docstring for exactly how — same idea as the manual pattern further down,
minus the manual bookkeeping and safe against the SQLite/Postgres
`connect_args` mismatch that pattern has to work around by hand).

```bash
cd backend
pip install -r requirements.txt --break-system-packages
pip install pytest httpx --break-system-packages   # only needed to run tests

pytest tests -v
```

Runs in CI on every push/PR too (`.github/workflows/ci.yml`'s `Pytest`
step) — a failing test now fails the build, not just gets logged.
`backend/pytest.ini` is what makes both `cd backend && pytest tests` above
and CI's own `pytest backend/tests` (run from the repo root) behave
identically regardless of which directory pytest was started from —
`tests/conftest.py` handles putting `backend/` on `sys.path` either way.

What's covered today:
- `test_auth.py` — login for every seeded role (including the hardcoded
  Super Admin) plus username-vs-email login, bad credentials, and
  self-service password changes.
- `test_asset_pools.py` — pool CRUD permission gating, capacity updates,
  and the "can't delete a pool with outstanding checkouts" guard.
- `test_checkouts_and_extensions.py` — dispatch → partial/full return and
  the Available/Outbound recalculation, plus the full extension-request
  lifecycle (self-service request → Manager approve/deny, and the direct
  "extend" shortcut).
- `test_notification_recipients.py` — regression coverage confirming
  extension-request email notifications go to the admin-configured
  **Digest Recipients** list, never a raw "every Admin/Manager" role query
  (see [Due-Date Extensions & Notifications](#due-date-extensions--notifications)).
- `test_permissions.py` — spot-checks of `deps.py`'s role gates across
  several routers.
- `test_error_handling.py` — forces a genuinely unanticipated exception
  (via monkeypatch, not a route's own `try/except`) and confirms
  `middleware/error_handling.py`'s global safety net: a 500 with the
  same `{"detail": ...}` shape every other error uses, a `request_id`
  in the body that matches the `X-Request-ID` response header, the full
  traceback actually reaching the logger, CORS headers still present on
  the error response, and ordinary `HTTPException` paths (e.g. a plain
  401) staying completely unaffected.
- `test_health.py` — `/healthz` never touches the database even when the
  schema is missing/broken (pure liveness), and `/readyz` correctly
  reports not-ready when the `alembic_version` table is missing or its
  revision is stale, and ready once it matches this build's expected head.
- `test_mfa.py` — the Super Admin-only TOTP enrollment/verification
  flow: regular roles never get prompted for 2FA, first-login forces
  setup vs. later logins requiring verify, wrong codes are rejected and
  lock out repeated attempts, expired/garbage tokens are rejected, and
  enrollment issues exactly ten distinct one-time recovery codes.
- `test_clean_urls_middleware.py` — `middleware/clean_urls.py`'s clean-URL
  rewriting (`/admin` → `admin.html`) and old-link 301 redirects
  (`/admin.html` → `/admin`, query string preserved), confirms `/api/*`
  routes are never touched by it, and an unrecognized clean-looking path
  404s rather than guessing at a filename.
- `test_csv_import.py` — bulk asset-pool CSV import: only a Super Admin
  can import, a file with a mix of valid/invalid rows partially succeeds
  (valid rows saved, invalid ones rejected, never silently merged/
  duplicated), and missing required columns are rejected before any row
  is processed at all.
- `test_outsiders.py` — Outsider (non-employee borrower) lifecycle:
  soft-delete blocked while items are in active custody, role gating on
  who can delete an Outsider, 404s on an already-deleted or nonexistent
  Outsider ID, and dispatching a checkout/quote to an existing vs. brand
  new Outsider profile.
- `test_outsider_convert_to_user.py` / `test_user_convert_to_outsider.py`
  — the two-way "convert an Outsider into a logged-in User" (and back)
  flow: active/returned checkouts and any Quotation assignment migrate
  along with the record, a Manager can't promote someone into an Admin
  account or revoke an Admin/Manager account, Staff can't perform the
  conversion at all, and converting an already-converted record 404s
  instead of double-converting.
- `test_quotation_workflow.py` — the full Equipment Quotation lifecycle
  (draft → submit → approve → fulfill/checkout), discount-then-VAT
  calculation order, per-line rental-day subtotals on a multi-line cart,
  and the guardrails against approving/checking-out a Quotation twice or
  checking one out before it's approved.
- `test_redbeat_scheduling.py` — confirms Celery is actually configured
  with `RedBeatScheduler` (not the default file-based one — see
  `celery_app.py`), and that only one replica can hold the Beat lock at
  a time, including the lock expiring and failing over if the active
  replica dies, and two replicas racing to dispatch the same scheduled
  task only ever resulting in one actual dispatch.
- `test_migrations.py` — runs real `alembic upgrade head`/`downgrade`
  round-trips against a throwaway database (not the SQLite fixtures the
  rest of the suite uses): full schema creation, the root-admin bootstrap
  migration behaving correctly in development (never auto-created) vs.
  production (created exactly once, with a generated password if none
  was supplied), re-running the upgrade never duplicating that row, and
  a downgrade-by-one-step removing only what it added.

Two of the above (`test_migrations.py`, `test_redbeat_scheduling.py`)
need a real Postgres/Redis, not just the SQLite fixtures — see each
file's own module docstring for how to point them at a scratch instance;
`ci.yml` runs them against real service containers, so `pytest tests -v`
locally without those services up will show them failing for
environment reasons, not a real regression.

Not everything has dedicated tests yet — audit-log/export-related
service functions and the Backups panel's create/restore paths are the
current gaps — extend it the same way: add a new `test_*.py` file under `backend/tests/`, reuse the
`client`/`db_session`/`as_admin`/`as_manager`/`as_staff`/`as_customer`/
`as_super_admin` fixtures from `conftest.py` rather than hand-rolling a
new database/login setup per file.

### Fastest option for one-off manual checks: Swagger UI (`/docs`)

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

For a quick one-off script instead of a real test (the automated suite
above is almost always the better choice for anything you intend to keep):

```bash
cd backend
pip install httpx --break-system-packages   # only needed for this test client

python3 << 'EOF'
import os
os.environ["JWT_SECRET_KEY"] = "local-manual-test-secret"

# NOTE: database.py's real `engine` is built with a `connect_args={"connect_timeout": 10}`
# that's psycopg2/Postgres-only -- sqlite3.connect() doesn't accept that
# keyword, so just pointing DATABASE_URL at a sqlite file isn't enough.
# Swap the engine/SessionLocal directly instead, exactly like
# backend/tests/conftest.py's `db_engine` fixture does.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import database
test_engine = create_engine("sqlite:////tmp/test.db", connect_args={"check_same_thread": False})
database.engine = test_engine
database.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
database.init_db()
database.seed_db()

from fastapi.testclient import TestClient
import main
main.app.dependency_overrides[database.get_db] = lambda: database.SessionLocal()
client = TestClient(main.app)

# Log in as the demo Super Admin -- POST /auth/login sets the JWT as an
# HttpOnly cookie (see api/auth_api.py), not a "token" field in the response
# body, so `client` (which keeps its own cookie jar) is already
# authenticated for every request after this one; no headers= needed.
r = client.post("/api/auth/login", json={"identifier": "r.adeyemi@corp.io", "password": "Admin123!"})
print(r.status_code, r.json())

# Try whatever you just built, e.g.:
r = client.get("/api/assets")
print(r.status_code, r.json())
EOF

rm -f /tmp/test.db   # clean up when you're done
```

**Why this is safe against any endpoint:** SQLite has no native
timezone-aware datetime type, so a `DateTime(timezone=True)` column (e.g.
`AssetCheckout.due_date`) silently round-trips as a naive datetime here,
even though the exact same column always comes back tz-aware against real
Postgres. `models.py`'s `is_overdue()`/`is_due_soon()` — used by
`GET /users`, `GET /users/me/items`, `GET /outsiders`, and the Custody
Ledger — normalize a naive `due_date` to UTC before comparing, specifically
so this SQLite testing pattern behaves the same as production instead of
raising `TypeError: can't compare offset-naive and offset-aware datetimes`
the moment a checkout with a due date is involved.

### Frontend

For the React frontend, `npm run dev` gives Vite hot reload while you work.
For the legacy frontend, changes to `.html`/`.js` are reflected after a
refresh when you run the corresponding static/nginx development path. The
React app's API client also falls back to its mock data when a backend fetch
fails, while the legacy client surfaces request errors in the browser console.

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
- [ ] Generate and set a real `JWT_SECRET_KEY` and `POSTGRES_PASSWORD`
      (the former alone makes the backend **refuse to start** if
      `JWT_SECRET_KEY` is still empty/placeholder or too short — see
      `config.py`).
- [ ] `AUTO_INIT_DB=false` and `AUTO_SEED_DEMO_DATA=false` — run
      `alembic upgrade head` as its own explicit deploy step instead
      (this also bootstraps the root admin with a randomly generated
      password, printed to stderr once — see "Viewing the
      one-time-generated root admin password" above), and never create
      the public demo accounts against a real database. **Already the
      default** the moment `ENVIRONMENT=production` is set — `config.py`'s
      `apply_environment_defaults()` auto-flips this, `ENABLE_API_DOCS`,
      and `ENABLE_AUTO_BACKUP` to production-safe values for you. Setting
      these explicitly in `.env` anyway is still recommended (explicit
      beats implicit for a deployment's actual config), but nothing
      breaks if you forget.
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
      sets `false` for both services in Render; `config.py` — see above —
      also defaults the backend's copy to `false` under
      `ENVIRONMENT=production`, but the frontend/nginx copy has no such
      auto-default, so set it explicitly for both). Confirm it worked by
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
- [x] ~~Drop `--reload` from the backend's `uvicorn` command and run with
      multiple `--workers` instead~~ — automatic now: `backend/start.sh`
      picks the right `uvicorn` invocation based on `ENVIRONMENT` at
      container start (no `--reload`, `UVICORN_WORKERS` worker processes
      for `production`; `--reload` for anything else). Nothing to do here
      as long as `ENVIRONMENT=production` is set — just confirm
      `UVICORN_WORKERS` (default `2`) matches the CPU you've given the
      container.
- [ ] The login rate limiter is Redis-backed (see
      `middleware/rate_limit.py`'s docstring), so it's already safe to run
      more than one `backend` replica — just confirm every replica points
      at the SAME `REDIS_URL`. See **[DEPLOYMENT.md](./DEPLOYMENT.md)** for
      the full production deployment, safety, and load balancing guide
      (scaling `backend`/`worker` during peak traffic, the scheduled-backup
      leader lock, disk-backed exports, and more).
- [ ] Review and tighten `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` /
      `ACCOUNT_LOCKOUT_DURATION_MINUTES` and
      `LOGIN_RATE_LIMIT_MAX`/`LOGIN_RATE_LIMIT_WINDOW_SECONDS` for your
      actual expected traffic pattern.
- [ ] Set up a real backup schedule for the Postgres volume — this
      project doesn't include one, since backup strategy is very
      deployment-specific (managed Postgres providers usually handle this
      for you automatically).

## Safely Updating An Existing Production Deployment (CI/CD)

**This repo ships seven GitHub Actions workflows** in `.github/workflows/`:
[`ci.yml`](.github/workflows/ci.yml) (ruff lint, the real
`pytest backend/tests` suite against real Postgres/Redis service
containers — including the actual `alembic upgrade head`/`downgrade` chain
and the RedBeat distributed-lock test, see [Automated test
suite](#automated-test-suite-backendtests) — a `pip-audit` dependency
scan, a Gitleaks secret scan, frontend build/rendering tests, an nginx
config job that renders `nginx/default.conf.template`, `nginx -t`s it,
then actually boots it and curls every clean-URL/redirect/static-asset
path (see `nginx/test-config.sh`), image build + Trivy scan, and
`infra/main.bicep` validation — runs on every push/PR, can also be started
manually from **Actions → CI → Run workflow**, and is also invoked as a
reusable `workflow_call` by every deploy workflow below; coverage isn't
100% of the app yet, see [Suggested Future
Features](#suggested-future-features) for what's still missing),
[`deploy-azure-aca.yml`](.github/workflows/deploy-azure-aca.yml)
(manual `workflow_dispatch` ONLY -- pick `staging` or `production` -- no
push trigger and no `workflow_call` entry point, so nothing but a human
running this workflow ever deploys here; a push to `develop`, and a `git
tag` push, both never auto-deploy),
[`release.yml`](.github/workflows/release.yml) (triggered by pushing a
`git tag v1.x.x` — builds and pushes both images tagged with that VERSION,
not just a commit SHA, opens a pull request against `main` with a new
[`CHANGELOG.md`](CHANGELOG.md) section (never a direct commit — this
repo's `main` only changes via reviewed PR) and cuts a GitHub Release. It
does NOT deploy anywhere -- run `deploy-azure-aca.yml`/`deploy-azure-vm.yml`
by hand, whenever you choose, with the version tag pasted into `image_tag`,
to actually ship a release)
— plus
[`infra-deploy.yml`](.github/workflows/infra-deploy.yml), run separately and
occasionally, for provisioning/updating `infra/main.bicep` itself. All of
these target the **Azure Container Apps** path.

The **Azure VM** path (see below) has its own three, self-contained
workflows instead: [`infra-deploy-vm.yml`](.github/workflows/infra-deploy-vm.yml)
(provisions the VM itself via `infra-vm/`'s Terraform — the VM-path
equivalent of `infra-deploy.yml` above),
[`deploy-azure-vm.yml`](.github/workflows/deploy-azure-vm.yml) (build both
images → Trivy scan → SSH over the Cloudflare Tunnel → sync
`docker-compose.vm.yml`/`Caddyfile` → `docker compose up -d` → migrate →
smoke test — same manual-`workflow_dispatch`-only shape as
`deploy-azure-aca.yml` above (no push trigger, no `workflow_call` entry
point); a `git tag v1.x.x` push builds and publishes the release images via
`release.yml` but never deploys them here on its own, just without a
Container Apps control plane in the middle), and
[`sync-secrets-vm.yml`](.github/workflows/sync-secrets-vm.yml) (pushes
updated `.env` values out to the running VM without a full redeploy). The
two paths are independent — use one, the other, or both side by side —
and never share GitHub Environment secrets (see
[DEPLOYMENT_VM.md](DEPLOYMENT_VM.md)'s "Using both deployment targets"
section).

> Not itemized above: `build-push-images.yml` (the reusable
> `workflow_call`-only workflow every deploy path above delegates the
> actual `docker build`/push/Trivy-scan step to -- see that file's own
> header comment) and `repair-tunnel-token-vm.yml` (a standalone
> break-glass workflow for the VM path's Cloudflare Access service token).
> Neither is a deploy trigger you'd run directly day to day, which is why
> they're left out of the walkthrough above, but they do exist in
> `.github/workflows/` alongside the seven described here.

All of these already follow the same rule, which is what makes any of this genuinely
*safe* to automate rather than just fast:

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

### Running CI manually from GitHub

CI still runs automatically on pushes and pull requests, but you can now
run the same validation pipeline on demand without making a commit.

1. Open the repository on GitHub and go to **Actions**.
2. Select **CI**.
3. Click **Run workflow**.
4. Select the branch or commit you want to validate.
5. Leave **Run infra/main.bicep validation too** enabled unless you are
   deliberately testing only application code.
6. Click **Run workflow**.

A manual CI run does not deploy anything, push Docker images, change Azure
resources, or require Azure credentials. It deliberately treats the
repository as fully changed, so the complete validation suite runs rather
than being skipped by the changed-path optimization.

### Azure Container Apps (the primary production target)

The full walkthrough — one-time setup, what each workflow does stage by
stage, rollback, scaling, monitoring, and cost — lives in
[`DEPLOYMENT.md`](DEPLOYMENT.md)'s **Azure Container Apps Production
Deployment** section rather than being duplicated here. Short version:
deploys are always manually triggered (`workflow_dispatch` from the
Actions tab), for both staging and production. Pushing a `git tag v1.x.x`
off `main` builds and publishes the official release images and cuts a
GitHub Release/CHANGELOG entry, but does not ship anything by itself —
run `deploy-azure-aca.yml` (or `deploy-azure-vm.yml`) by hand, pick
`production`, and paste that version into `image_tag` whenever you're
ready to deploy it.

### Azure VM

A second, self-contained deployment target for a single Azure VM instead
of Container Apps — `infra-vm/`'s Terraform provisions the VM, a
Cloudflare Tunnel replaces both inbound SSH and any open inbound app port
(no public IP ever has port 22/80/443 listening on it), and `docker
compose -f docker-compose.vm.yml` runs the same six services as local dev
as plain containers pulled by tag from Docker Hub, plus `caddy` (TLS
re-presentation) and `cloudflared` (the tunnel). The VM infrastructure is
fully lifecycle-managed by GitHub Actions: the workflow creates/reuses the
Terraform state resource group, Storage Account, and blob container before
`terraform init`, then owns Terraform plan/apply/destroy. The state backend is
kept outside the VM resource group so a destroy cannot delete the state it
needs. The only one-time Azure bootstrap is the local OIDC identity setup
(`az login`, `gh auth login`, then `scripts/bootstrap-azure-github.sh`).
After that, no VM resource group or Terraform storage needs to be created
manually.

The application deployment remains manual-`workflow_dispatch`-only: a pushed
version tag publishes the images but does not deploy them. The automated Azure
bootstrap creates/reuses the Entra CI identity and GitHub OIDC configuration;
its subscription and Terraform-state RBAC assignments are created through the
ARM `Microsoft.Authorization` REST API, not `az role assignment`, so the
bootstrap is resilient to the Azure CLI `MissingSubscription` behavior seen in
some tenants. Full setup,
Cloudflare configuration, OIDC bootstrap, remote-state lifecycle, rollback,
growing the data disk, Google Drive backups, and updating secrets on a running
VM live in [`DEPLOYMENT_VM.md`](DEPLOYMENT_VM.md).

### Render

The free-plan shape this project ships with (see [Render](#render) above)
needs no separate CI/CD workflow at all: `AUTO_INIT_DB=true` in
`render.yaml` means the web service brings its own schema up to date via
`create_all()` on every boot, so pushing to your connected branch and
letting Render's own Blueprint auto-deploy rebuild `snipeit-lite-web` is
the entire "deploy" step.

One gotcha worth knowing: Render's auto-deploy on push only rebuilds and
restarts the service from the code that changed — it does **not** re-parse
`render.yaml` itself. If you edit `render.yaml` (e.g. add an env var or
change a plan), that change only takes effect after you trigger **Manual
Sync** from the Blueprint's page in the Render Dashboard; a plain `git push`
silently leaves the old resource/env-var shape in place.

### Generic cloud (AWS/GCP/Kubernetes/etc.)

Same "migrate first, deploy second" rule applies; the mechanics depend on
your platform's own release-step feature (ECS's task definition + a
one-off migration task before the service update, a Kubernetes `Job` +
`initContainer` pattern ahead of a rolling `Deployment` update, etc.) —
outside this project's scope to ship a ready-made workflow for every
platform, but `deploy-azure-aca.yml` (build → scan → migrate →
roll out → smoke test → rollback) is a reasonable template to adapt if
your platform doesn't have a closer native equivalent.

### Automated Dependency Updates (Dependabot)

[`.github/dependabot.yml`](.github/dependabot.yml) opens a PR once a week
for every package manifest in this repo — `backend/requirements.txt`,
all three `package.json`s (`build-frontend`, `build-tailwind`,
`frontend/tests`), both Dockerfiles' base images (`backend`, `frontend`),
and the GitHub Actions themselves (`actions/checkout`, `trivy-action`,
etc.) — rather than dependency drift being something you have to remember
to go check for.

This is complementary to `ci.yml`'s existing `pip-audit` step, not a
duplicate of it: `pip-audit` is a point-in-time check ("are any CURRENTLY
pinned versions known-vulnerable right now?") that runs on every push.
Dependabot instead proactively proposes version bumps on a schedule —
including plain staleness with no CVE attached — so upgrades land as
small, individually-reviewable PRs instead of a single "everything is two
years behind" PR later. Every Dependabot PR runs through the exact same
`ci.yml` gate as a human-authored one (`ci.yml` already declares
`workflow_call` and triggers on any push/PR), so reviewing and merging one
is no riskier than merging your own PR.

You still own the merge decision — Dependabot opens the PR, it doesn't
auto-merge. `SRE_STRATEGY.md`'s quarterly checklist is where "actually
merge the accumulated PRs, run the full suite, ship it as its own release"
lives, so bumps don't quietly pile up unreviewed either.

---

## Suggested Future Features

Small, well-scoped follow-ups if you want to keep extending this project:

- **Broader automated test coverage** — `backend/tests/` now covers a lot
  of ground (auth/MFA, asset pools, checkouts/extensions, CSV import,
  Outsiders and both conversion directions, the full Quotation workflow,
  clean URLs, health/readiness, RedBeat scheduling, migrations, the
  global error handler, distributed tracing, and role permission gates —
  see [Testing Your Changes](#testing-your-changes) for the full
  file-by-file breakdown; the Backups panel's create/restore paths in
  particular are now covered in depth by `test_backup_restore.py`,
  including cross-schema restores and the reconciliation logic), but
  audit-log/export service functions still don't have a dedicated test
  file — a good first PR for getting familiar with the
  `client`/`db_session`/`as_*` fixtures in `backend/tests/conftest.py`.
- **A `deleted_by` column** recording which admin performed a given
  soft-delete (good first Alembic migration exercise) — `restore_user()`
  itself (undoing a soft-delete) already shipped; see [Directories](#directories-super-admin--manager).
- **`Strict-Transport-Security` (HSTS)**, set at your TLS-terminating
  reverse proxy once deployed with HTTPS. (A real `Content-Security-Policy`
  is no longer on this list — see `nginx/default.conf.template`, which now
  sets one tuned against the frontend's actual CDN/script usage.)
- **Per-user notification preferences** — email is currently all-or-nothing
  via `NOTIFICATIONS_ENABLED` (see [Due-Date Extensions &
  Notifications](#due-date-extensions--notifications)); a `users` table
  column for "email me my own overdue/due-soon reminders: yes/no" would
  be a small, well-scoped follow-up.

## Troubleshooting

- **Login (or literally any `/api/*` call) fails with `405 Method Not
  Allowed`, and the response body is just `{"detail": "Method Not
  Allowed"}`** — this means nginx is forwarding requests to the backend
  with the wrong path (commonly, every request collapsing down to just
  `/`, which only has a `GET` handler). This is a well-known nginx gotcha:
  `proxy_pass`'s usual "trailing slash strips the matched `location`
  prefix" behavior **only works when the upstream address is a static
  string** — it silently stops working the moment that address is a
  *variable* (which `nginx/default.conf.template`'s `/api/` block uses, so
  nginx re-resolves `BACKEND_HOST` on every request instead of caching a
  possibly-stale IP). The fix already in this repo sidesteps that trick
  entirely instead of relying on it: `proxy_pass http://$backend_upstream
  $request_uri;` forwards the request's full original path — `/api/...`
  prefix included — and every FastAPI router is mounted with
  `app.include_router(..., prefix="/api")` in `backend/main.py` to match
  (see that file's `BUG FIX` comment above the router-mounting block for
  the concrete brute-force-throttle bug this exact mismatch caused before
  it was fixed). If you ever edit `nginx/default.conf.template`'s
  `location /api/` block, keep the `$request_uri` variable in `proxy_pass`
  (don't swap in a bare `$uri`, which drops the query string, or a
  hardcoded path) — and if you ever change a backend router's prefix,
  nginx needs no changes at all, since it never strips or rewrites the
  path in the first place. Rebuild the frontend image after any nginx
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

### Bicep plan/apply/destroy summaries

The **Deploy ACA Infrastructure (Bicep)** workflow now surfaces infrastructure previews in the GitHub Actions **Summary** tab. `plan` runs Azure Resource Manager What-If without changing Azure and reports Create/Modify/Delete/Deploy/No-change counts plus resource IDs. `apply` runs the same What-If immediately before the deployment and records the preview before applying it. `destroy` reads the Deployment Stack's managed-resource list and publishes the resources that will be deleted; the parent resource group is retained.

The Bicep preview uses `az deployment group what-if`, while actual lifecycle management continues to use the Azure Deployment Stack. Azure's What-If supports resource-group scoped previews, and Deployment Stacks support `deleteResources` for deleting resources that are no longer managed.

## ErrorBeacon real-time monitoring

The application now has an isolated ErrorBeacon service for fast exception detection and Telegram notification. It does not replace Azure Monitor/Application Insights; it shortens the path from an unexpected application error to a human-readable alert.

See:

- [`ERRORBEACON_COVERAGE.md`](ERRORBEACON_COVERAGE.md) for the system-wide error-handling scan and capture points.
- [`errorbeacon/README.md`](errorbeacon/README.md) for the monitor itself.
- [`errorbeacon/DEPLOYMENT.md`](errorbeacon/DEPLOYMENT.md) for local Docker, Render Free, ACA, VM and other deployment paths.
