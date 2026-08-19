# Post-Deployment Configuration (Azure)

This is for you if `infra-deploy.yml` and `deploy-azure-aca.yml`
have already run successfully, the app is
live at its `*.azurecontainerapps.io` FQDN, and you've logged in as
`superadmin`. Three things are still optional and off by default at that
point:

1. [SMTP (email notifications)](#1-smtp-email-notifications)
2. [Google Drive backup uploads](#2-google-drive-backup-uploads)
3. [Mapping a custom domain](#3-mapping-a-custom-domain)
4. [Site branding, security limits & operational tuning](#4-site-branding-security-limits--operational-tuning)

All four follow the same shape: set some GitHub Environment config, then
re-run `infra-deploy.yml` for that environment (Actions tab → **Deploy ACA
Infrastructure (Bicep)** → Run workflow → pick `staging` or `production` →
`apply`). The workflow derives the resource group automatically
(`rg-snipeit-lite-staging` or `rg-snipeit-lite-prod`), registers the Bicep
providers, and reconciles the Deployment Stack. You do not create or select
a resource group manually. It also reads the currently deployed backend
image tag before applying infra changes so an infrastructure reconciliation
does not silently replace a running application image with a placeholder.

GitHub gives you two kinds of repository config, and this doc uses both —
the table under each section below says which one applies:

- **Secrets** (Settings → Secrets and variables → Actions → **Secrets**
  tab) — encrypted at rest, masked in logs, never shown in
  `az containerapp show`'s output. Used for anything sensitive: passwords,
  tokens, API keys. Sections 1-3 below are entirely secrets.
- **Variables** (same path, **Variables** tab instead) — plain text,
  visible to anyone with repo access, used for non-sensitive config like
  feature flags, numeric limits, or display strings. Section 4 below is
  entirely variables.

Both kinds can be set at **repo level** (one value shared by every
environment) or scoped to a **GitHub Environment** (`staging` /
`production` individually, overriding the repo-level value of the same
name for jobs that run against that environment — `infra-deploy.yml`
already declares `environment: ${{ github.event.inputs.target }}`, so this
works out of the box for anything in this file). Sections 1-3's secrets are
**per-environment** by default (staging and production almost always want
different values — e.g. a real mail server for production, nothing or a
throwaway inbox for staging), scoped to the `staging` / `production`
**Environment**, same place `POSTGRES_PASSWORD` and the others from
[DEPLOYMENT.md](DEPLOYMENT.md)'s setup table already live. Section 4's
variables are **repo-level** by default (both environments share one
value) since there's rarely a reason for staging and production to look or
behave differently on those — but nothing stops you from scoping any of
them per-Environment instead if you do want a split (e.g. a different
`SITE_NAME` for staging).

## Table of Contents

1. [SMTP (email notifications)](#1-smtp-email-notifications)
2. [Google Drive backup uploads](#2-google-drive-backup-uploads)
3. [Mapping a custom domain](#3-mapping-a-custom-domain)
4. [Site branding, security limits & operational tuning](#4-site-branding-security-limits--operational-tuning)
5. [Troubleshooting: "Backup Now" fails](#troubleshooting-backup-now-fails-with-a-pg_dumppassword-error)
6. [Reference: where each setting actually lives](#reference-where-each-setting-actually-lives)

---

---

## 1. SMTP (email notifications)

This app sends three kinds of email — extension-request alerts, a daily
overdue-checkout digest, and a daily "due soon" reminder. See
[README.md's Notifications section](README.md#notifications) for what
triggers each one; this section is only about wiring up the mail server
itself. Any plain SMTP server works — your own Postfix, SendGrid, Mailgun,
AWS SES's SMTP endpoint, etc. — there's no vendor-specific SDK involved.

**Steps:**

1. Get SMTP credentials from whichever provider you're using (host,
   port — almost always `587`, username, password, and a "From" address
   the provider will let you send as — most providers reject a `From:`
   that doesn't match a domain/sender you've verified with them).

2. Add these GitHub Secrets and this one Variable, scoped to the
   environment you're configuring (Settings → Secrets and variables →
   Actions — Secrets and Variables are two separate tabs):

   | Secret | Value |
   |---|---|
   | `SMTP_HOST` | e.g. `smtp.sendgrid.net` |
   | `SMTP_USERNAME` | Your provider's SMTP username |
   | `SMTP_PASSWORD` | Your provider's SMTP password/API key |
   | `SMTP_FROM_EMAIL` | The verified "From" address |
   | `ADMIN_NOTIFICATION_EMAILS` | *(optional)* comma-separated extra recipients for extension-request alerts, on top of Admins/Managers/the Super Admin, who already get those automatically |

   | Variable (not Secret) | Value |
   |---|---|
   | `NOTIFICATIONS_ENABLED` | `true` |

   (Port/transport default to `587`/STARTTLS, but are configurable — if
   your provider requires implicit SSL on `465` instead, set the
   `SMTP_PORT`, `SMTP_USE_TLS`, and `SMTP_USE_SSL` **Variables** described
   in [section 4](#4-site-branding-security-limits--operational-tuning)
   below rather than editing `infra/main.bicep` directly.)

3. Re-run `infra-deploy.yml` for that environment.

4. **Verify it actually works.** There's no dedicated "send test email"
   button — the fastest real check is the daily digest's recipient list,
   which is separate from the extension-request alert audience above and
   editable at runtime without a redeploy: log in as an Admin/Super Admin,
   and `PUT /notifications/settings/digest-recipients` (see
   `backend/api/notifications_api.py`) sets who receives it. Simplest test:
   set that list to just your own email, then trigger a run without
   waiting for the schedule at all — SSH/exec into the `worker` container
   and run `celery -A celery_app call tasks.send_overdue_notifications`
   (or `tasks.send_due_soon_reminders`); Celery queues it immediately and
   the worker picks it up within seconds, same task code the daily
   `OVERDUE_DIGEST_HOURS_UTC`/`DUE_SOON_DIGEST_HOURS_UTC` schedule would
   have run. (Unlike the old interval-based schedule, temporarily
   "lowering" a fixed clock-time schedule doesn't reliably shorten the
   wait — the next crontab-scheduled hour could still be nearly 24 hours
   away — so `celery call` is the more direct check.) Alternatively, if
   you have any checkout that's overdue or due soon already, it'll show
   up in the very next scheduled run without needing any of this at all.

   The same `celery call` trick also works for the two SLA-nudge jobs
   (`tasks.escalate_pending_extension_requests`/
   `tasks.escalate_pending_quotations` — see
   [Environment Variables Reference](README.md#environment-variables-reference)'s
   `EXTENSION_REQUEST_SLA_HOURS`/`QUOTATION_SLA_HOURS`/etc.), and since
   those run on a plain interval (`APPROVAL_SLA_CHECK_INTERVAL_MINUTES`)
   rather than a fixed clock time, temporarily lowering that interval
   *does* reliably shorten the wait for the next real scheduled run, if
   you'd rather test that path than force one manually.

---

## 2. Google Drive backup uploads

By default, backups (`pg_dump`, gzip-compressed, once daily at
`BACKUP_HOURS_UTC`, plus on-demand via the "Backup Now" button in
Admin → Audit & Backups) are written to the `backend` container's local
disk only (`BACKUP_DIR=/app/backups`, backed by the Azure Files share this
environment already provisions — see `infra/main.bicep`'s top-of-file
comment for why Postgres itself can't sit on that same Azure Files share).
That's durable across restarts/redeploys on this architecture (unlike, say,
Render's free tier, which is why this feature exists at all), but a second,
independent, off-Azure copy is still worth having. Google Drive backup
upload is the built-in way to get one.

Two auth modes exist (see `backend/config.py`'s `BACKUP_GDRIVE_*`
docstring for the full "why"); this deployment only wires up the one that
actually fits a normal use case here:

- **OAuth as a real Google user** (what's covered below) — uploads count
  against that account's own 15GB Drive quota, exactly like uploading
  through drive.google.com by hand. Works with any personal/consumer
  Google account.
- **A Google Cloud service account** — only works if the destination
  folder is a Shared Drive, which is a Google Workspace-only feature.
  Deliberately **not** wired up as a Bicep parameter in this deployment,
  since it doesn't apply to a personal-account setup — if you specifically
  need it, see `backend/config.py`'s `BACKUP_GDRIVE_CREDENTIALS_JSON` and
  add it to `infra/main.bicep`/`infra-deploy.yml` yourself, following the
  same pattern as the OAuth secrets below.

**Steps:**

1. **Run the one-time OAuth setup script on your own machine — not in CI,
   not inside the container.** It opens a browser, has you log into the
   Google account whose Drive you want backups to land in, and prints
   three values.

   ```bash
   # In the repo, on your own machine:
   pip install google-auth-oauthlib
   ```

   Then, in the [Google Cloud Console](https://console.cloud.google.com/):
   - Create (or reuse) a project — free, no billing account required.
   - **APIs & Services → Library** → enable the **Google Drive API**.
   - **APIs & Services → OAuth consent screen** → choose **External** →
     fill in an app name/support email → Save. Leave it in "Testing" mode
     (fine to stay there forever for this) — on the "Test users" step, add
     your own Google account's email.
   - **APIs & Services → Credentials → Create Credentials → OAuth client
     ID** → Application type **Desktop app** → Create → **Download JSON**.

   Then run:

   ```bash
   python backend/scripts/gdrive_oauth_setup.py /path/to/the_downloaded.json
   ```

   A browser window opens — log in with that same Google account and click
   Allow. The script prints three values:

   ```
   BACKUP_GDRIVE_OAUTH_CLIENT_ID=...
   BACKUP_GDRIVE_OAUTH_CLIENT_SECRET=...
   BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN=...
   ```

   Publish Your App to Production. Go to the Google Cloud Console. Select your project and navigate to APIs & Services -> OAuth consent screen ->.

2. In Google Drive, create (or pick) a normal folder for backups to land
   in — no sharing step needed, since uploads happen as yourself. Grab its
   ID from the folder's URL: `https://drive.google.com/drive/folders/<THIS_PART>`.

3. Add these GitHub Secrets and this one Variable, scoped to the
   environment you're configuring (Settings → Secrets and variables →
   Actions — Secrets and Variables are two separate tabs):

   | Secret | Value |
   |---|---|
   | `BACKUP_GDRIVE_OAUTH_CLIENT_ID` | Printed by the script in step 1 |
   | `BACKUP_GDRIVE_OAUTH_CLIENT_SECRET` | Printed by the script in step 1 |
   | `BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN` | Printed by the script in step 1 |
   | `BACKUP_GDRIVE_FOLDER_ID` | The folder ID from step 2 |

   | Variable (not Secret) | Value |
   |---|---|
   | `BACKUP_GDRIVE_ENABLED` | `true` |

4. Re-run `infra-deploy.yml` for that environment.

5. **Verify it actually works:** Admin → Audit & Backups → **Backup Now**.
   The resulting entry in the backups list reports its Google Drive upload
   state directly (also visible via `GET /backup/status` /
   `GET /backup/list` if you'd rather check via the API) — confirm the
   file actually landed in the Drive folder from step 2, then you're done.

   The refresh token from step 1 doesn't expire from time passing alone —
   only from a manual revoke at
   [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
   or from 6 months of the app going unused. Re-run the script and update
   the three secrets if that ever happens.

---

## 3. Mapping a custom domain

`infra/main.bicep` already supports this — `frontend`'s ingress declares a
`customDomains` binding whenever the `customDomain` parameter is set — but
Azure Container Apps requires the domain to be **verified and a managed
certificate bound to it once, outside of Bicep**, before that
parameter does anything useful (Bicep alone can't complete domain
ownership verification for you). After that one-time binding, this becomes
part of the app's normal state and future `infra-deploy.yml` runs just
keep declaring it — no repeated manual step.

**Steps:**

1. **Get the domain's verification ID.** This ID is what proves to Azure
   you actually control the domain, via a TXT record.

   ```bash
   az containerapp show \
     --name frontend \
     --resource-group <your resource group> \
     --query properties.customDomainVerificationId -o tsv
   ```
    az containerapp show  --name frontend --resource-group rg-snipeit-lite-prod --query properties.customDomainVerificationId -o tsv

2. **Add two DNS records** at your domain registrar/DNS provider, for the
   (sub)domain you want to use (e.g. `assets.yourcompany.com`):

   | Type | Name | Value |
   |---|---|---|
   | `CNAME` | `assets` (or whatever subdomain you're using) | `frontend.<your environment's default domain>` — get this exact value from `az containerapp env show --name <env name> --resource-group <rg> --query properties.defaultDomain -o tsv`, then prefix `frontend.` |
   | `TXT` | `asuid.assets` (i.e. `asuid.` + your subdomain) | The verification ID from step 1 |

   Root/apex domains (`yourcompany.com` with no subdomain) need an `A`
   record instead of `CNAME`, pointed at the environment's static IP —
   see
   [Microsoft's custom domain docs](https://learn.microsoft.com/azure/container-apps/custom-domains-managed-certificates)
   for the apex-specific steps if that's your situation; a subdomain is
   simpler and is what's covered here.

   DNS propagation can take anywhere from a few minutes to a few hours —
   `dig CNAME assets.yourcompany.com` (or
   [whatsmydns.net](https://www.whatsmydns.net/)) confirms once it's live
   globally.

3. **Register & Bind the domain with a free managed certificate** (once records have
   propagated):

   ```bash
   az containerapp hostname bind \
     --name frontend \
     --resource-group <your resource group> \
     --hostname assets.yourcompany.com \
     --environment <your Container Apps environment name> \
     --validation-method CNAME
   ```
   To get your container apps environment name, run
   az containerapp env list --resource-group rg-snipeit-lite-prod --output table

   Register
   az containerapp hostname add --name frontend --resource-group rg-snipeit-lite-prod --hostname stack.multione.online
   
   Bind
   az containerapp hostname bind --name frontend --resource-group rg-snipeit-lite-prod --hostname stack.multione.online --environment snipeit-lite-prod-env --validation-method CNAME

   This validates the TXT/CNAME records, provisions a free managed TLS
   certificate for the domain, and binds it to `frontend` — takes a few
   minutes. Azure renews this certificate automatically for as long as the
   DNS records above stay in place; nothing further to do here.

4. **Add the `CUSTOM_DOMAIN` GitHub secret**, scoped to the environment,
   set to the domain itself (e.g. `assets.yourcompany.com`), and re-run
   `infra-deploy.yml`. This keeps the binding declared in Bicep going
   forward (so it survives, rather than fights, future infra deploys) and
   also flips `PUBLIC_ORIGIN`/CORS to the custom domain instead of the
   `*.azurecontainerapps.io` FQDN (see `infra/main.bicep`'s
   `publicOrigin` variable) — anything that generates absolute links
   (quotation PDFs, notification emails) uses this value, so this step
   matters even after the hostname itself is already reachable.

5. **Verify:** visit `https://assets.yourcompany.com` — should load with a
   valid certificate (green padlock, no warnings) and behave identically
   to the `*.azurecontainerapps.io` URL. The old generated FQDN keeps
   working side by side; nothing about this steps stops using it if you
   still need to.

---

## 4. Site branding, security limits & operational tuning

Unlike sections 1-3 above, none of these are secrets — no passwords or
tokens among them — so all 21 are **GitHub Variables** (Settings → Secrets
and variables → Actions → **Variables** tab, not Secrets), repo-level by
default. Every one of them already has a working default matching
`.env.example` (see the "Default" column below), so you only need to set
the ones you actually want to change — leaving all of them unset behaves
exactly as if this section didn't exist.

**Steps:** add whichever Variables below you want to change, then re-run
`infra-deploy.yml` for the environment(s) you set them on (repo-level
Variables apply to both `staging` and `production` in one run each; an
Environment-scoped Variable only takes effect for that one environment's
run).

### Branding & API docs

| Variable | Default | What it changes |
|---|---|---|
| `SITE_NAME` | `Snipe-IT Lite` | Navbar/login header, browser tab title, and the Quotation/Checkout Receipt PDF letterhead |
| `ENABLE_API_DOCS` | `false` | Whether `/docs` (Swagger), `/redoc`, and `/openapi.json` exist at all. Keep `false` on anything internet-reachable unless you specifically need them |
| `LOG_LEVEL` | `INFO` | Structured logging verbosity: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |

### Login security

| Variable | Default | What it changes |
|---|---|---|
| `LOGIN_RATE_LIMIT_MAX` | `5` | Max `POST /auth/login` attempts per `LOGIN_RATE_LIMIT_WINDOW_SECONDS` from the same client IP before HTTP 429 |
| `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | `60` | The window (seconds) the limit above applies over |
| `ACCOUNT_LOCKOUT_MAX_ATTEMPTS` | `5` | Consecutive wrong-password attempts against the *same account* (any IP) before it locks |
| `ACCOUNT_LOCKOUT_DURATION_MINUTES` | `15` | How long that per-account lockout lasts |

### Root admin identity

| Variable | Default | What it changes |
|---|---|---|
| `SUPER_ADMIN_USERNAME` | `superadmin` | The root account's login username, read once by the `migrate` Job's bootstrap migration |
| `SUPER_ADMIN_NAME` | `Super Admin` | The root account's display name |

Note: the root admin's *password* is `ROOT_ADMIN_BOOTSTRAP_PASSWORD` — that
one **is** sensitive and stays a per-environment Secret (see
[DEPLOYMENT.md](DEPLOYMENT.md)'s setup table), not a Variable here.

### SMTP transport (pairs with the Secrets in [section 1](#1-smtp-email-notifications) above)

| Variable | Default | What it changes |
|---|---|---|
| `SMTP_PORT` | `587` | SMTP port — `587` for STARTTLS, or `465` for implicit SSL |
| `SMTP_USE_TLS` | `true` | Use STARTTLS on `SMTP_PORT` |
| `SMTP_USE_SSL` | `false` | Use implicit SSL instead (takes priority over `SMTP_USE_TLS` if both are `true`) — set this `true` together with `SMTP_PORT=465` |

### Notification timing

| Variable | Default | What it changes |
|---|---|---|
| `OVERDUE_DIGEST_HOURS_UTC` | `8` | Comma-separated hours of day (UTC, each 0-23) the worker checks for overdue checkouts and emails the admin/manager digest, e.g. `8` or `8,20` — same syntax as `BACKUP_HOURS_UTC` |
| `DUE_SOON_REMINDER_DAYS` | `2` | How many days ahead of its due date a checkout counts as "due soon" (dashboard banner, My Items badge, and the reminder email) |
| `DUE_SOON_DIGEST_HOURS_UTC` | `8` | Comma-separated hours of day (UTC, each 0-23) the worker checks for checkouts about to go overdue |
| `SEND_INDIVIDUAL_HOLDER_REMINDERS` | `true` | Whether the "your item is overdue/due soon" reminder also goes to the checkout's own holder, in addition to the admin/manager digest |

### Pending-approval SLA nudges

| Variable | Default | What it changes |
|---|---|---|
| `EXTENSION_REQUEST_SLA_HOURS` | `24` | How many hours a `pending` Extension Request can go without a Manager/Admin/Super Admin decision before the SLA-nudge digest escalates it |
| `QUOTATION_SLA_HOURS` | `24` | Same idea for a `submitted` Quotation waiting on an Admin/Manager's approve/adjust decision — its own independent threshold |
| `APPROVAL_SLA_CHECK_INTERVAL_MINUTES` | `60` | How often (in minutes) the worker checks both queues above for anything past its SLA threshold |
| `APPROVAL_SLA_ESCALATION_REPEAT_HOURS` | `24` | Once a pending request/quote has been escalated, how many hours before it's eligible to be escalated again if still undecided |

### Quotation notifications

| Variable | Default | What it changes |
|---|---|---|
| `SEND_QUOTATION_RECIPIENT_EMAILS` | `true` | Whether a Quotation's own recipient gets emailed on every change (line items, notes, discount, assignment, approval, fulfillment). The in-app bell notification is always created regardless of this setting — it only gates the extra email |

### Locale & catalog

| Variable | Default | What it changes |
|---|---|---|
| `DISPLAY_TIMEZONE` | `Africa/Lagos` | IANA timezone name used to render CSV/PDF export timestamps (data itself is always stored as UTC) |
| `CURRENCY_CODE` | `NGN` | ISO 4217 currency code shown wherever a price is displayed or exported |
| `CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER` | `false` | Whether staff/customer accounts can see a pool's available quantity + in-stock/out-of-stock status in the self-service Quotation Catalog |

### Backup schedule

| Variable | Default | What it changes |
|---|---|---|
| `BACKUP_HOURS_UTC` | `3` | Comma-separated hours of day (UTC, each 0-23) the in-process gzip `pg_dump` job runs at, e.g. `3` or `3,15,21` |
| `BACKUP_RETENTION_COUNT` | `7` | How many local backup files to keep before deleting the oldest |

---

---

## Troubleshooting: "Backup Now" fails with a `pg_dump`/password error

If clicking **Backup Now** shows something like:

```
Backup failed: pg_dump failed (exit 1): pg_dump: error:
connection to server at "...postgres.database.azure.com" ... failed:
FATAL:  password authentication failed for user "snipeit"
connection to server at "...postgres.database.azure.com" ... failed:
FATAL:  no pg_hba.conf entry for host "...", user "snipeit",
database "asset_db", no encryption
```

...even though the password has never actually been changed — that's two
independent bugs in `backend/services/backup_service.py`'s
`_db_connection_kwargs()`, both now fixed, neither one caused by password
drift:

- **The "password authentication failed" line** was the real one: Python's
  `urlparse()` does not percent-decode the username/password portion of a
  URL. `DATABASE_URL`'s password is correctly percent-encoded by
  `infra/main.bicep` (has to be — a raw `+`/`/`/`@`/`:`/... would otherwise
  be parsed as URL syntax) — and this guide's own `openssl rand -base64 24`
  suggestion for `POSTGRES_PASSWORD` routinely produces exactly those
  characters. Every other consumer of `DATABASE_URL` (SQLAlchemy, i.e. the
  actual running app) decodes the URL properly and connects fine, which is
  why login/migrations/normal use all worked while only backups failed —
  this one hand-rolled `pg_dump`/`psql` invocation was the sole place still
  sending the raw, still-percent-encoded string as `PGPASSWORD`. Fixed by
  decoding it with `unquote()` before handing it to `pg_dump`/`psql`.

- **The "no encryption"/pg_hba line** was `?sslmode=require` being silently
  dropped entirely when building `pg_dump`/`psql`'s discrete
  `--host`/`--port`/... flags, so libpq fell back to its own default
  (`prefer`), whose plaintext-fallback attempt is exactly what Azure's
  Flexible Server (which enforces SSL) rejects. Fixed by setting
  `PGSSLMODE` explicitly from that same query parameter.

Nothing to do on your end once you're running a build that includes both
fixes — no password reset, no secret rotation, no redeploy of
`postgresServer` needed. If backups still fail after updating, that's the
point where it's actually worth checking whether the live server's
password genuinely has drifted from `POSTGRES_PASSWORD` (e.g. someone
changed it by hand in the Portal) — in that case,
`az postgres flexible-server update --name <server> --resource-group <rg>
--admin-password '<value>'` forces it back in sync, followed by
re-running `infra-deploy.yml`.

---

## Reference: where each setting actually lives

If you'd rather change these by hand instead of via GitHub config +
`infra-deploy.yml` (e.g. a quick one-off test), all four sections live in
`infra/main.bicep`:

- SMTP: the `smtpHost`/`smtpUsername`/`smtpPassword`/`smtpFromEmail`/
  `notificationsEnabled`/`adminNotificationEmails`/`smtpPort`/
  `smtpUseTls`/`smtpUseSsl` parameters, wired into
  `sharedEnv`/`sharedSecrets` just above the `backend` Container App
  resource.
- Google Drive: the `gdriveBackupEnabled`/`gdriveOauthClientId`/
  `gdriveOauthClientSecret`/`gdriveOauthRefreshToken`/`gdriveFolderId`
  parameters, same location.
- Custom domain: the `customDomain` parameter, referenced by
  `frontendApp`'s `ingress.customDomains` and by the `publicOrigin`
  variable.
- Section 4 (branding/security/timing/locale/backup): `siteName`,
  `logLevel`, `enableApiDocs`, `loginRateLimitMax`,
  `loginRateLimitWindowSeconds`, `accountLockoutMaxAttempts`,
  `accountLockoutDurationMinutes`, `superAdminUsername`, `superAdminName`,
  `overdueDigestHoursUtc`, `dueSoonReminderDays`,
  `dueSoonDigestHoursUtc`, `sendIndividualHolderReminders`,
  `displayTimezone`, `currencyCode`, `catalogShowStockToStaffCustomer`,
  `backupHoursUtc`, `backupRetentionCount` parameters, all wired into
  `sharedEnv` right alongside the SMTP/Google Drive ones above.

A direct `az deployment group create ... --parameters smtpHost=...` works
fine too — just remember any value set that way is **not** reflected back
into GitHub secrets/variables, so the next `infra-deploy.yml` run
(triggered by someone else, or by you forgetting) will silently revert it
to whatever GitHub currently says. Prefer the secrets/variables + workflow
path above for anything you want to stick.
