// =============================================================================
// infra/main.bicep
// -----------------------------------------------------------------------------
// COST-OPTIMIZED Azure Container Apps deployment for Snipe-IT Lite --
// THREE Container Apps: `frontend`, `backend`, `redis`, all inside ONE
// Container Apps Managed Environment (see "SINGLE MANAGED ENVIRONMENT" below
// for why this file uses one, not three), PLUS a managed Azure Database for
// PostgreSQL Flexible Server (`postgresServer` below) -- NOT a fourth
// Container App. See "WHY POSTGRES IS A MANAGED SERVICE, NOT A CONTAINER
// APP" immediately below for why that one piece can't be a Container App at
// all, regardless of budget.
//
// WHY POSTGRES IS A MANAGED SERVICE, NOT A CONTAINER APP
// ---------------------------------------------------------------------------
// An earlier version of this file ran Postgres as a fourth Container App
// (`db`, official postgres:16-alpine image) on a persistent Azure Files
// share, the same pattern still used below for `redis` and for
// `backend`'s `backup-data`/`export-data` volumes. That is NOT a sizing
// problem you can fix with a bigger container -- it fails at container
// *start*, before Postgres ever gets to serve a single query:
//
//   F chmod: /var/lib/postgresql/data/pgdata: Operation not permitted
//   F initdb: error: could not change permissions of directory
//     "/var/lib/postgresql/data/pgdata": Operation not permitted
//
// Azure Files (an SMB/NFS share) does not implement real POSIX ownership
// and permission bits the way a local/managed block-storage disk does --
// `chmod`/`chown` on a mounted Azure Files share are either silently
// ignored or rejected, depending on protocol and mount options. Postgres's
// own `initdb` unconditionally `chmod 700`s its data directory as a
// hard-coded safety check (refuses to run as an unprivileged process
// otherwise) -- there is no Postgres config flag, entrypoint env var, or
// Container Apps CPU/memory setting that makes that call succeed against
// Azure Files. This is a documented, permanent incompatibility between
// Azure Files and any database engine that needs POSIX file permissions on
// its data directory (Postgres, MySQL, etc.) -- not something this app was
// doing wrong. Every Container Apps persistent-volume option
// (`AzureFile`, and the newer `NfsAzureFile`) is backed by Azure Files
// under the hood, so there is no volume type inside Container Apps that
// fixes this -- the storage layer itself is the blocker, not the
// container.
//
// `redis` and `backend`'s `backup-data`/`export-data` volumes don't hit
// this: Redis's own data files don't need `chmod`-on-boot the way
// `initdb` does (and this app already runs `redis` with `--appendonly no`,
// no persistence at all), and `backend`'s own files are written by the app
// itself post-boot, not by a startup routine that hard-fails on a
// permission-bit mismatch. Postgres is the one piece of this stack Azure
// Files structurally cannot host.
//
// The fix: Azure Database for PostgreSQL Flexible Server
// (`postgresServer` below) -- Postgres running on Microsoft-managed,
// Postgres-aware storage (not Azure Files), so `initdb`'s `chmod` succeeds
// the normal way. This also removes an entire category of self-inflicted
// ops work this file used to take on for a single-instance, single-writer
// database it was never actually a good idea to hand-roll: patching minor
// versions, taking your own backups as the *only* backup story (this
// app's own `pg_dump`-based backups, still available via
// `ENABLE_AUTO_BACKUP`, are now a *convenience* layer on top of the
// managed service's own automated backups/point-in-time-restore, not the
// only line of defense), and reasoning about `chmod`/ownership edge cases
// on a shared filesystem a database was never designed to run on. The
// added cost is small (smallest Burstable SKU, see the `postgresSkuName`
// parameter below) and, unlike the old `db` Container App, buys you
// automated backups with point-in-time restore, engine patching, and a
// supported upgrade path -- for a stateful single-writer database, that
// trade is worth taking even in a cost-optimized design. See
// DEPLOYMENT.md's Cost section for the updated numbers.
//
// This is the split-services evolution of an earlier, even leaner version of
// this file that ran ONE combined `app` container (backend + frontend +
// embedded Celery worker/beat, via Dockerfile.render -- the same image used
// for the Render free-tier deploy) alongside `db`/`redis`. That version is
// still the cheapest possible shape and is a perfectly reasonable choice --
// see git history / DEPLOYMENT.md for the reasoning -- but it couples
// frontend and backend to the SAME scaling unit: a burst of pure
// asset-browsing traffic scales up backend replicas (and their embedded
// Celery workers) even if no API calls are actually happening, and vice
// versa. This version decouples them.
//
// WHY `redis` IS STILL A CONTAINER APP, NOT JUST `frontend`/`backend`
// ---------------------------------------------------------------------------
// `backend` runs with RUN_EMBEDDED_WORKER=true (same as before) and now
// genuinely autoscales 0-N on its own, independent of `frontend`. Once
// `backend` can run more than one replica, Redis stops being optional:
//   - it's the Celery broker every replica's embedded worker shares (one
//     task queue, not N independent ones)
//   - it backs the cross-replica login rate limiter (see this doc's
//     "Load Balancing & Scaling For Peak Use" section above)
//   - it backs the scheduled-backup leader lock (so N replicas don't all
//     run pg_dump at 3am simultaneously)
// Cutting Redis would mean pinning `backend` to exactly 1 replica forever
// (no real autoscaling) or silently breaking all three of the above the
// first time it scales past 1. Unlike Postgres (see above), Redis here
// runs with `--appendonly no` -- no on-disk persistence at all, so it
// never touches Azure Files and never hits the `chmod`/`initdb` problem --
// keeping it as a small Container App (not a managed Azure Cache instance)
// is a safe, cheap trade (0.25 vCPU/0.5 GiB, same acceptable "resets on
// restart" trade as before).
//
// WHY `frontend` IS ITS OWN CONTAINER APP, NOT FOLDED INTO `backend`
// ---------------------------------------------------------------------------
// `frontend/js/api.js` hardcodes API_URL = '/api' as a RELATIVE path and
// every request sends credentials:'include' (cookie auth) -- both only work
// same-origin. Rather than rewrite that (and every cookie-auth code path)
// to support a cross-origin split, `frontend` reuses frontend/Dockerfile
// UNMODIFIED -- the exact same image that already does this for local
// Docker Compose: it serves the static build itself AND reverse-proxies
// /api/* to `backend` over the Container Apps environment's internal DNS
// (nginx/default.conf.template's BACKEND_HOST/BACKEND_PORT env vars, no
// hardcoded platform assumptions, resolver IP auto-detected at boot -- see
// nginx/docker-entrypoint.d/15-detect-resolver-ip.sh). Zero frontend code
// changes; the browser still only ever talks to ONE origin (`frontend`'s),
// exactly like before. `backend` becomes internal-only ingress now -- only
// `frontend` and `migrate` ever reach it, a small security improvement over
// the previous combined `app`, which was directly internet-facing.
//
// COST IMPACT OF THE SPLIT (vs. the single-`app` version)
// ---------------------------------------------------------------------------
// Roughly a wash, sometimes a net win: `frontend` adds one small
// scale-to-zero container, but `frontend` and `backend` now each scale to
// their OWN actual load instead of both being sized for whichever is
// busier. See DEPLOYMENT.md's Cost section for the full breakdown table.
//
// SINGLE MANAGED ENVIRONMENT (was: three, one per trust tier)
// ---------------------------------------------------------------------------
// This file previously gave `frontend`, `backend`, and `db`/`redis` each
// their OWN Container Apps Managed Environment (own delegated subnet, own
// NSG) purely for network segmentation -- see the git history for that
// version's "SECURITY FIX" comment. In practice that hit a hard wall:
// `Microsoft.App/managedEnvironments` is capped by a per-subscription
// quota, and on Free Trial/starter subscriptions that cap is exactly 1 --
// and it's GLOBAL across every region, not per-region: the 2nd environment
// fails with `MaxNumberOfRegionalEnvironmentsInSubExceeded` if you land in
// a region that already has one, and the 3rd still fails with
// `MaxNumberOfGlobalEnvironmentsInSubExceeded` even in a brand-new region,
// because the limit counts environments subscription-wide. Switching
// regions cannot work around this. Three environments is a deploy-time
// failure on those subscriptions, not just a theoretical concern -- this
// is the fix for exactly that failure.
//
// Consolidated back to ONE environment (`env` below), one subnet, one NSG.
// `frontend`, `backend`, `redis`, and the `migrate` Job all share it (Postgres
// is a separate managed service outside this environment entirely -- see
// "WHY POSTGRES IS A MANAGED SERVICE" above -- so it isn't part of this
// quota/subnet discussion at all). The trade-off: an NSG applies at the
// SUBNET boundary, and every app inside one Container Apps Environment
// shares that environment's one subnet -- Azure does not let you attach
// per-app network rules within an environment (see
// https://learn.microsoft.com/azure/container-apps/firewall-integration),
// so the lateral-movement protection a three-subnet design would buy (a
// compromised `frontend` literally cannot resolve/reach `redis` on the
// wire, regardless of application logic) isn't present here. What's still
// in place, unchanged, at the APPLICATION layer:
//   - `redis`/`backend` both still set `ingress.external: false` -- neither
//     ever gets a public FQDN, only `frontend` does. `postgresServer`
//     likewise has no Container Apps ingress at all (it isn't a Container
//     App); its own firewall rules gate who can reach it -- see that
//     resource's comment.
//   - `backend`'s API still only trusts requests proxied through
//     `frontend` in practice (same-origin cookie auth, see the comment
//     above), and `redis`/`postgresServer` still require their own
//     passwords.
// What's gone is defense-in-depth against a compromised container
// port-scanning `redis` directly -- if that risk matters more to you than
// the quota/cost trade, either request an environment-quota increase from
// Azure support and restore the three-environment version from git
// history, or self-host a reverse proxy/service mesh inside this one
// environment instead. For most early-stage deployments, the small
// standing risk is an acceptable trade for "the deploy actually succeeds."
//
// WHAT'S UNCHANGED FROM THE COMBINED-`app` VERSION (see that version's
// original comment, preserved in git history, for the full reasoning)
// ---------------------------------------------------------------------------
//   - No Azure Container Registry -- images pulled from Docker Hub (now
//     TWO images: <dockerHubUsername>/snipeit-lite-backend and
//     .../snipeit-lite-frontend; a Docker Hub free account only includes
//     ONE private repo, so if you want both private you'll need a paid
//     Docker Hub plan or to keep one of the two public -- default is BOTH
//     public, zero registry cost/credentials either way)
//   - No Key Vault -- plain Container Apps secrets
//   - No managed identity, no Application Insights BY DEFAULT -- Application
//     Insights is now available as an opt-in (see `otelAzureMonitorEnabled`
//     param) for OpenTelemetry distributed tracing; see "WHAT WAS REMOVED...
//     AND WHY IT'S SAFE HERE" below for why turning it on no longer
//     conflicts with this file's cost-optimized design
//   - `redis` unchanged: official Docker Hub image, internal-only, pinned
//     to exactly 1 replica, no persistent volume
// =============================================================================
// This replaces an earlier version of this file that ran Postgres as a
// fourth Container App (`db`) on a persistent Azure Files share -- which
// does not work, full stop, regardless of CPU/memory sizing (see "WHY
// POSTGRES IS A MANAGED SERVICE" above). It also replaces the version
// before THAT, which used Azure Database for PostgreSQL Flexible Server +
// Azure Cache for Redis + Azure Container Registry + Key Vault + a
// User-Assigned Managed Identity + Application Insights + 4 Container Apps
// (backend/worker/beat/frontend) -- a solid *scaling* story, but several of
// those managed extras (Azure Cache, ACR Basic, Key Vault) have their own
// fixed monthly floor and never scale to zero regardless of traffic. This
// version keeps Flexible Server (the one piece that has no working
// Container-Apps-only substitute) and drops the rest of that list in favor
// of Container Apps' own free/scale-to-zero equivalents.
//
// WHAT THIS PROVISIONS
// ---------------------------------------------------------------------------
//   - Log Analytics workspace                    (Container Apps console/system logs)
//   - Storage Account + 2 Azure Files shares      (backup_data, export_data --
//                                                   billed by GB actually used, not provisioned)
//   - VNet (1 delegated subnet + NSG)              (see SINGLE MANAGED ENVIRONMENT comment above; no fixed floor)
//   - 1 Container Apps Environment                 (Consumption plan, shared by every app below -- no fixed floor; see SINGLE MANAGED ENVIRONMENT comment above for why this is 1, not 3)
//   - 1 Azure Database for PostgreSQL Flexible Server (`postgresServer`) -- smallest Burstable
//                                                   SKU by default (see `postgresSkuName`), NOT inside
//                                                   the Container Apps environment -- its own managed
//                                                   resource, own storage, own automated backups
//   - 3 Container Apps:
//       `redis`    -- redis:7-alpine, official Docker Hub image, internal-only, 1 replica always
//       `backend`  -- FastAPI + embedded Celery worker/beat (backend/Dockerfile),
//                     internal-only ingress, scales 0-N on its own
//       `frontend` -- static frontend + reverse proxy to `backend` (frontend/Dockerfile,
//                     UNMODIFIED from local Docker Compose), the ONLY public-facing app,
//                     scales 0-N independent of `backend`
//   - 1 Container Apps Job: `migrate`             (runs `alembic upgrade head` against `backend`'s image, only when triggered)
//
// WHAT WAS REMOVED FROM THE ORIGINAL MANAGED-SERVICES DESIGN, AND WHY IT'S
// SAFE HERE
// ---------------------------------------------------------------------------
//   - Azure Cache for Redis -> `redis` container app, no persistent volume.
//     Still just the Celery broker/result backend + rate-limiter/lock store
//     (see this file's top comment) -- losing state on a restart is an
//     acceptable trade for the cost savings, and (unlike Postgres) Redis
//     here never touches Azure Files in the first place.
//   - Azure Container Registry -> Docker Hub (two images now: backend and
//     frontend -- see top comment on the free-plan private-repo limit).
//   - Key Vault -> plain Container Apps secrets.
//   - User-assigned managed identity -> removed (nothing left to authenticate
//     once ACR and Key Vault are both gone, assuming public Docker Hub repos).
//   - Application Insights -> removed by default (its own ingestion cost on
//     top of Log Analytics) -- now available as an OPT-IN via
//     `otelAzureMonitorEnabled` (default false, so nothing changes unless
//     you ask for it) for OpenTelemetry distributed tracing
//     (backend/telemetry.py). Workspace-based on the SAME `logAnalytics`
//     below rather than a second standalone resource, so turning it on
//     adds usage-based cost only (Application Insights' first 5GB/month is
//     free per billing account -- see that param's own @description and
//     README.md's "Distributed Tracing" section), not a second fixed
//     floor.
//   Postgres itself was NOT removed/downgraded -- see "WHY POSTGRES IS A
//   MANAGED SERVICE" at the top of this file for why that one piece stays
//   a managed service even in an otherwise cost-optimized design.
//
// REALISTIC MONTHLY COST -- see DEPLOYMENT.md's Cost section for the full
// breakdown table. The Flexible Server is the one component here that
// can't scale to zero and has a real fixed floor (smallest Burstable SKU,
// ~US$12-15/mo before storage); everything else keeps the prior design's
// scale-to-zero/Consumption-plan cost profile.
//
// USAGE
// ---------------------------------------------------------------------------
//   az deployment group create \
//     --resource-group rg-snipeit-lite-prod \
//     --template-file infra/main.bicep \
//     --parameters environmentName=prod \
//                  dockerHubBackendImage=yourdockerhubusername/snipeit-lite-backend \
//                  dockerHubFrontendImage=yourdockerhubusername/snipeit-lite-frontend \
//                  postgresPassword=$(openssl rand -base64 24) \
//                  redisPassword=$(openssl rand -hex 16) \
//                  jwtSecretKey=$(openssl rand -hex 32) \
//                  rootAdminBootstrapPassword=$(openssl rand -base64 24)
//                  # ^ postgresPassword MUST satisfy Azure Database for
//                  # PostgreSQL Flexible Server's password complexity rule
//                  # (8-128 chars, at least 3 of: uppercase, lowercase,
//                  # digit, symbol) -- `openssl rand -base64 24` reliably
//                  # produces all four; `openssl rand -hex ...` (all this
//                  # file used pre-managed-Postgres) does NOT, since hex
//                  # output is only digits + a-f. rootAdminBootstrapPassword
//                  # is optional -- omit (or leave "") to let the migrate
//                  # Job generate one instead and print it to stderr once
//                  # (see DEPLOYMENT.md's Monitoring section for how to read
//                  # that back out of Log Analytics if you go that route).
//                  # Passing it explicitly here, as above, means you already
//                  # have it in your own shell instead. Either way it's a
//                  # no-op on every deploy after the first -- the migrate
//                  # Job only ever bootstraps the root admin row once.
//
// Re-run the same command any time to update the environment idempotently --
// this file does NOT set `backend`/`frontend`/`migrate`'s image tags on
// every run (that's the CI/CD pipeline's job via `az containerapp update
// --image`), so deploying new code never requires a full infra re-deploy.
// =============================================================================

@description('Short environment name: "prod" or "staging". Prefixes every resource name.')
@allowed(['prod', 'staging'])
param environmentName string = 'prod'

@description('Azure region for every resource.')
param location string = resourceGroup().location

@description('Base name used to derive resource names, e.g. "snipeit-lite".')
param appBaseName string = 'snipeit-lite'

@description('Docker Hub repository for the backend image (FastAPI + embedded Celery worker/beat), e.g. "yourusername/snipeit-lite-backend", built from backend/Dockerfile. Public repo by default -- no registry credentials needed.')
param dockerHubBackendImage string

@description('Docker Hub repository for the frontend image (static frontend + reverse proxy to `backend`), e.g. "yourusername/snipeit-lite-frontend", built from frontend/Dockerfile UNCHANGED from local Docker Compose. Public repo by default.')
param dockerHubFrontendImage string

@description('Image tag to deploy on first create, applied to BOTH images. The CI/CD pipeline overwrites this on every push via `az containerapp update --image` (backend and frontend are updated independently -- see deploy-azure-*.yml).')
param initialImageTag string = 'latest'

@description('Set only if dockerHubBackendImage/dockerHubFrontendImage are PRIVATE Docker Hub repositories (same account for both). Leave empty if both are public (recommended -- zero credential management). NOTE: Docker Hub free plan includes only ONE private repo -- if you need both private, either upgrade your Docker Hub plan or keep one of the two public.')
param dockerHubUsername string = ''

@description('Docker Hub Personal Access Token, only required if dockerHubUsername is set.')
@secure()
param dockerHubToken string = ''

@description('Administrator password for the Azure Database for PostgreSQL Flexible Server. MUST satisfy Azure\'s complexity rule: 8-128 characters, at least 3 of {uppercase, lowercase, digit, symbol}. Generate with `openssl rand -base64 24`, NOT `openssl rand -hex ...` (hex output is only digits + a-f -- 2 categories -- and Flexible Server will reject it).')
@secure()
param postgresPassword string

@description('Administrator username for the Flexible Server. Avoid reserved/disallowed names (azure_superuser, azuresu, admin, administrator, root, guest, public, or anything starting with pg_).')
param postgresUsername string = 'snipeit'

@description('Flexible Server compute SKU. Standard_B1ms (1 vCore/2GiB, Burstable) is the smallest generally-available tier and the default here for cost. Standard_B2s (2 vCore/4GiB) is the next step up if B1ms\'s burst credits get exhausted under sustained load (see DEPLOYMENT.md\'s Cost section).')
param postgresSkuName string = 'Standard_B1ms'

@description('Flexible Server compute tier matching `postgresSkuName`. Keep this "Burstable" if you change the SKU to another B-series size; only change to "GeneralPurpose"/"MemoryOptimized" if you also change the SKU to a matching D/E-series name.')
@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
param postgresSkuTier string = 'Burstable'

@description('Flexible Server storage size in GiB. 32 is the smallest size Azure currently offers for this SKU family. Storage can only be INCREASED later, never decreased, so don\'t over-provision "just in case" -- start at the minimum and grow if you actually need to.')
@minValue(32)
param postgresStorageGb int = 32

@description('PostgreSQL major version to provision.')
param postgresVersion string = '16'

@description('Flexible Server automated backup retention, in days (7-35). These are Azure-managed backups with point-in-time restore, separate from and in addition to this app\'s own pg_dump-based ENABLE_AUTO_BACKUP job.')
@minValue(7)
@maxValue(35)
param postgresBackupRetentionDays int = 7

@description('Geo-redundant Flexible Server backups. Off by default to keep cost down (geo-redundancy roughly doubles backup storage cost); turn on if your recovery plan needs to survive a full regional outage, not just a single zone/server failure.')
param postgresGeoRedundantBackup bool = false

@description('OPTIONAL. Your own IP address (e.g. from `curl ifconfig.me`), added as an extra Flexible Server firewall rule so you can `psql`/pgAdmin/etc. directly against it from your machine for debugging. Leave empty (default) to skip -- `backend`/`migrate` reach the server regardless via the "Allow Azure services" firewall rule below, which this parameter does not affect.')
param postgresAdminClientIp string = ''

@description('Redis password for the `redis` container app (used with --requirepass).')
@secure()
param redisPassword string

@description('JWT signing secret. Generate with: openssl rand -hex 32')
@secure()
param jwtSecretKey string

@description('OPTIONAL. One-time root admin bootstrap password, read directly by the migrate Job the first time it runs (see backend/alembic/versions/0002_bootstrap_root_admin.py). Never read by the running backend/frontend apps. Leave empty to have that migration generate and print a random password to the Job\'s logs exactly once instead.')
@secure()
param rootAdminBootstrapPassword string = ''

@description('Minimum `backend` replicas. 0 = scale-to-zero (cold start after idle, cheapest). 1 = always warm, small extra cost, no cold start. Independent of `frontend` -- that is the whole point of the split.')
param backendMinReplicas int = 0

@description('Maximum `backend` replicas under load. NOTE: `backend` embeds Celery worker+beat in-process (see RUN_EMBEDDED_WORKER below) since there is no separate worker/beat Container App in this cost-optimized layout. That is safe at any replica count: celery_app.py configures RedBeat as the Beat scheduler, which keeps a distributed lock in Redis so only one replica is ever the active scheduler at a time (automatic failover if that replica dies) -- no per-replica configuration needed here.')
param backendMaxReplicas int = 3

@description('Minimum `frontend` replicas. 0 = scale-to-zero (cold start on first request after idle -- static-file + proxy responses are fast, so it\'s much shorter than `backend`\'s, but not zero). 1 = always warm, no cold start, small extra cost. `infra-deploy.yml` passes 1 here for production and 0 for staging -- see that workflow\'s "Resolve replica floors" step -- so this parameter\'s own default only applies to a manual/direct bicep deploy that skips the pipeline.')
param frontendMinReplicas int = 0

@description('Maximum `frontend` replicas under load.')
param frontendMaxReplicas int = 3

@description('Custom domain for `frontend`, the public entry point (leave empty to use the generated *.azurecontainerapps.io FQDN only).')
param customDomain string = ''

@description('Notification / SMTP settings -- optional, off by default, matching .env.example.')
param notificationsEnabled bool = false
param smtpHost string = ''
param smtpUsername string = ''
@secure()
param smtpPassword string = ''
param smtpFromEmail string = ''
param adminNotificationEmails string = ''

@description('Google Drive backup upload -- optional, off by default, matching .env.example. OAuth mode only (a personal Google account\'s own Drive quota) -- see backend/scripts/gdrive_oauth_setup.py for the one-time script that produces gdriveOauthClientId/gdriveOauthClientSecret/gdriveOauthRefreshToken, and backend/config.py\'s BACKUP_GDRIVE_* docstring for why the service-account mode that script\'s docstring also describes is deliberately NOT exposed as a bicep param here (it requires a Google Workspace Shared Drive, not applicable to this app\'s typical personal-Drive use case). Leave gdriveBackupEnabled false (the default) to keep local-disk-only backups, same as before this parameter existed.')
param gdriveBackupEnabled bool = false
param gdriveOauthClientId string = ''
@secure()
param gdriveOauthClientSecret string = ''
@secure()
param gdriveOauthRefreshToken string = ''
param gdriveFolderId string = ''

@description('Gate for FastAPI\'s interactive API docs (Swagger/ReDoc) AND nginx\'s matching passthrough route -- see nginx/default.conf.template. Keep false in any environment reachable from the public internet unless you specifically need it.')
param enableApiDocs bool = false

@description('Email address to page on the three Azure Monitor scheduled query alerts below (backend error-rate spike, /readyz failing, daily backup missing) -- see SRE_STRATEGY.md section 2. Leave empty (the default) to skip creating the action group/alert rules entirely -- no alerting, no extra cost, same as before this parameter existed. IMPORTANT ordering requirement: leave this EMPTY on the very first deploy of a brand-new environment. The three alert rules below query the `ContainerAppConsoleLogs_CL` table, which Azure only materializes the first time a log line actually lands in it -- on a fresh Log Analytics workspace that table does not exist yet, and Microsoft.Insights/scheduledQueryRules validates its KQL against the workspace schema at deploy time, so creating the rules before any logs have been ingested fails deployment with "Failed to resolve table or column expression named \'ContainerAppConsoleLogs_CL\'". Deploy once with this empty, let `backend`/`frontend` serve at least one request (or just sit running for a few minutes) so the table gets created, confirm it under the Log Analytics workspace\'s Logs > Tables blade, THEN set this and re-run infra-deploy.yml for the same environment to add the alert rules on top of the already-running infra.')
param alertEmailAddress string = ''

// -----------------------------------------------------------------------------
// Previously hardcoded literals in `sharedEnv` below -- promoted to params so
// infra-deploy.yml can set them per-environment from GitHub Variables, without
// editing this file. All non-sensitive (no passwords/tokens among them), so
// they're read as `vars.X` in infra-deploy.yml, the same pattern already used
// for postgresSkuName/postgresStorageGb above -- not `secrets.X`.
// -----------------------------------------------------------------------------

@description('Brand name shown in the navbar/login header, browser tab title (GET /config/public), and the Quotation/Checkout Receipt PDF letterhead. Matches .env.example\'s SITE_NAME.')
param siteName string = 'Snipe-IT Lite'

@description('Structured logging level: DEBUG | INFO | WARNING | ERROR | CRITICAL. Matches .env.example\'s LOG_LEVEL.')
param logLevel string = 'INFO'

@description('POST /auth/login: max attempts allowed per loginRateLimitWindowSeconds from the same client IP before HTTP 429. Matches .env.example\'s LOGIN_RATE_LIMIT_MAX.')
param loginRateLimitMax int = 5

@description('POST /auth/login rate-limit window, in seconds. Matches .env.example\'s LOGIN_RATE_LIMIT_WINDOW_SECONDS.')
param loginRateLimitWindowSeconds int = 60

@description('Per-account brute-force lockout: consecutive wrong-password attempts against the SAME account before it is locked, regardless of IP. Matches .env.example\'s ACCOUNT_LOCKOUT_MAX_ATTEMPTS.')
param accountLockoutMaxAttempts int = 5

@description('Per-account lockout duration, in minutes. Matches .env.example\'s ACCOUNT_LOCKOUT_DURATION_MINUTES.')
param accountLockoutDurationMinutes int = 15

@description('Root admin account\'s username, bootstrapped once by the `migrate` Job (see backend/alembic/versions/0002_bootstrap_root_admin.py). Matches .env.example\'s SUPER_ADMIN_USERNAME.')
param superAdminUsername string = 'superadmin'

@description('Root admin account\'s display name. Matches .env.example\'s SUPER_ADMIN_NAME.')
param superAdminName string = 'Super Admin'

@description('SMTP port -- 587 for STARTTLS (pair with smtpUseTls=true) or 465 for implicit SSL (pair with smtpUseSsl=true). Matches .env.example\'s SMTP_PORT.')
param smtpPort int = 587

@description('Use STARTTLS on smtpPort. Matches .env.example\'s SMTP_USE_TLS.')
param smtpUseTls bool = true

@description('Use implicit SSL instead of STARTTLS -- takes priority over smtpUseTls if both are true; pair with smtpPort=465. Matches .env.example\'s SMTP_USE_SSL.')
param smtpUseSsl bool = false

@description('How often (in hours) the worker checks for overdue checkouts and emails the admin/manager digest. Matches .env.example\'s OVERDUE_NOTIFICATION_INTERVAL_HOURS. Typed as string, not int -- ARM/Bicep has no decimal parameter type, and this value supports fractional hours (e.g. "0.05" for a 3-minute interval while testing), which int would reject.')
param overdueNotificationIntervalHours string = '24'

@description('How many days ahead of its due_date an active checkout counts as "due soon" -- drives the dashboard banner, the My Items badge, and the due-soon reminder email. Matches .env.example\'s DUE_SOON_REMINDER_DAYS.')
param dueSoonReminderDays int = 2

@description('How often (in hours) the worker checks for checkouts about to go overdue. Matches .env.example\'s DUE_SOON_NOTIFICATION_INTERVAL_HOURS. Typed as string, not int -- ARM/Bicep has no decimal parameter type, and this value supports fractional hours (e.g. "0.05" for a 3-minute interval while testing), which int would reject.')
param dueSoonNotificationIntervalHours string = '24'

@description('Whether the individual "your item is overdue/due soon" reminder also goes to the checkout\'s own holder, in addition to the admin/manager digest. Matches .env.example\'s SEND_INDIVIDUAL_HOLDER_REMINDERS.')
param sendIndividualHolderReminders bool = true

@description('IANA timezone name (e.g. "Africa/Lagos") used to render CSV/PDF export timestamps -- data itself is always stored as UTC. Matches .env.example\'s DISPLAY_TIMEZONE.')
param displayTimezone string = 'Africa/Lagos'

@description('ISO 4217 currency code applied everywhere a price is shown or exported. Matches .env.example\'s CURRENCY_CODE.')
param currencyCode string = 'NGN'

@description('Whether a staff/customer account browsing the self-service Quotation Catalog can see each pool\'s available quantity + in-stock/out-of-stock status. Matches .env.example\'s CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER.')
param catalogShowStockToStaffCustomer bool = false

@description('Comma-separated hours of day (UTC, each 0-23) the in-process gzip pg_dump backup job runs at, e.g. "3" or "3,15,21". Matches .env.example\'s BACKUP_HOURS_UTC.')
param backupHoursUtc string = '3'

@description('How many local backup files to keep before deleting the oldest. Matches .env.example\'s BACKUP_RETENTION_COUNT.')
param backupRetentionCount int = 7

// --- Distributed tracing (OpenTelemetry -- Operations & Observability
// requirement #4; see backend/telemetry.py's module docstring) ------------
@description('Master switch for OpenTelemetry distributed tracing on `backend` (and its embedded Celery worker/beat). Off by default -- zero cost, zero behavior change, matching every other opt-in flag in this file. Does NOT by itself provision anything in Azure -- see otelAzureMonitorEnabled below for that. Turning this on with no exporter destination configured (otelAzureMonitorEnabled=false AND otelExporterOtlpEndpoint empty) just means spans are created and immediately discarded -- harmless, but pointless. Matches .env.azure.example\'s OTEL_ENABLED.')
param otelEnabled bool = false

@description('service.name resource attribute every span from `backend` carries -- what identifies this app in your tracing backend\'s UI. Matches .env.example\'s OTEL_SERVICE_NAME.')
param otelServiceName string = 'snipeit-lite-backend'

@description('Generic OTLP/HTTP collector endpoint (self-hosted otel-collector, Grafana Cloud, Honeycomb, ...) spans are exported to, e.g. "https://otel-collector.example.com". Leave empty (the default) if you\'re using otelAzureMonitorEnabled below instead, or if otelEnabled is false. Matches .env.azure.example\'s OTEL_EXPORTER_OTLP_ENDPOINT.')
param otelExporterOtlpEndpoint string = ''

@secure()
@description('Comma-separated key=value auth headers sent with every OTLP export request to otelExporterOtlpEndpoint above (e.g. an API key some SaaS tracing backends require). Stored as a Container Apps secret, never a plain env var, since it commonly carries a credential. Matches .env.azure.example\'s OTEL_EXPORTER_OTLP_HEADERS.')
param otelExporterOtlpHeaders string = ''

@description('Fraction (0.0-1.0) of traces actually sampled/exported. 1.0 (the default) traces everything -- fine at this app\'s scale; lower it if trace export volume/cost ever becomes a concern. Bicep has no native float param type, so this is a string parsed by backend/config.py\'s OTEL_TRACES_SAMPLE_RATIO. Matches .env.azure.example\'s OTEL_TRACES_SAMPLE_RATIO.')
param otelTracesSampleRatio string = '1.0'

@description('Provisions a `Microsoft.Insights/components` (Application Insights) resource, workspace-based on the SAME `logAnalytics` this file already provisions for container console logs (see that resource\'s own comment) -- no second fixed-cost resource, purely usage-based billing on top of it. Off by default, consistent with this file\'s cost-optimized design (see top-of-file comment) -- but reasonable to turn on even for a small deployment: Application Insights\' first 5GB of data ingested per month is free per BILLING ACCOUNT (shared across everything else in that account using Log Analytics/Application Insights too, not exclusive to this app -- see README.md\'s "Distributed Tracing" section for the full cost picture and a link to Microsoft\'s current pricing page), and 90 days of that data\'s retention is included at no extra charge. When true, this also wires the resulting connection string onto `backend` as the APPLICATIONINSIGHTS_CONNECTION_STRING secret (see `sharedSecrets` below) -- you still need otelEnabled=true too for anything to actually be sent there (see that param\'s own description).')
param otelAzureMonitorEnabled bool = false

var namePrefix = '${appBaseName}-${environmentName}'
var suffix = uniqueString(resourceGroup().id, environmentName)
var storageAccountName = take(replace('${appBaseName}${environmentName}st${take(suffix, 6)}', '-', ''), 24)

var usePrivateDockerHubRepo = !empty(dockerHubUsername)
var backendImage = '${dockerHubBackendImage}:${initialImageTag}'
var frontendImage = '${dockerHubFrontendImage}:${initialImageTag}'

// ---------------------------------------------------------------------------
// Monitoring -- one Log Analytics workspace for every container app's
// console/system logs. Application Insights is OPT-IN (see
// `otelAzureMonitorEnabled` param above) rather than always-on -- see that
// param's own @description for the reasoning this file's original
// "No Application Insights" design (see top-of-file comment) no longer
// fully applies now that distributed tracing (backend/telemetry.py) is a
// real Operations & Observability requirement, not a hypothetical.
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30 // shortest retention Log Analytics allows -- cheapest option; console logs are also always live-streamable via `az containerapp logs show` regardless of this setting
  }
}

// Workspace-based (points WorkspaceResourceId at `logAnalytics` above,
// rather than the older "classic" standalone mode) -- this is what lets
// Application Insights ride on that SAME Log Analytics workspace's
// pay-for-what-you-ingest billing instead of provisioning a second
// separate resource with its own cost floor. Only deployed at all when
// `otelAzureMonitorEnabled` is true; leave that at its default `false` and
// this section creates nothing and costs nothing, same as before it
// existed (identical opt-in pattern to `alertingEnabled`/
// `alertActionGroup` immediately below).
resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (otelAzureMonitorEnabled) {
  name: '${namePrefix}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    // This app's `backend` (a FastAPI/Celery service, not a browser page)
    // never loads Application Insights' JS snippet -- nothing here would
    // ever use it regardless of this setting, but disabling it explicitly
    // avoids Azure defaulting a public-web-facing setting on for a
    // service that has no public web frontend of its own (`frontend` is
    // the public-facing app, and it isn't the one instrumented with
    // OpenTelemetry -- see backend/telemetry.py's module docstring).
    DisableIpMasking: false
  }
}

// ---------------------------------------------------------------------------
// Alerting -- closes the gap SRE_STRATEGY.md section 2 originally flagged:
// `logAnalytics` above collects console logs, but nothing was watching them
// and paging anyone. These three Azure Monitor scheduled query alerts are
// the exact three failure modes that document calls out, as code instead of
// portal clicks. Billed per-rule (cents/month), no Application Insights
// required -- consistent with this file's cost-optimized design elsewhere.
//
// Entirely OPT-IN: every resource below only deploys if `alertEmailAddress`
// is set (see that param's description). Leave it empty and this section
// costs nothing and creates nothing, same as before it existed.
//
// ORDERING REQUIREMENT -- read before setting ALERT_EMAIL_ADDRESS on a new
// environment: alertBackendErrorRate/alertReadyzFailing/alertBackupMissing
// below all query `ContainerAppConsoleLogs_CL`, a table Azure only creates
// once the FIRST log line is actually ingested into it. On a brand-new
// `logAnalytics` workspace that table doesn't exist yet, and
// scheduledQueryRules validates its KQL against the live workspace schema
// at deploy time -- so deploying these three rules before `backend`/
// `frontend` have produced any console output fails with "Failed to
// resolve table or column expression named 'ContainerAppConsoleLogs_CL'".
// Leave `alertEmailAddress` empty on an environment's first-ever deploy,
// let the apps run for a few minutes (or serve one request) so the table
// materializes, then set it and re-run this workflow to layer the alert
// rules on top of the already-running infra. See `alertEmailAddress`'s
// @description above for the same note.
// ---------------------------------------------------------------------------
var alertingEnabled = !empty(alertEmailAddress)

resource alertActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (alertingEnabled) {
  name: '${namePrefix}-alerts'
  location: 'global' // action groups are always global, regardless of the alert rules' own region
  properties: {
    groupShortName: take('${environmentName}alerts', 12) // Azure caps this at 12 chars
    enabled: true
    emailReceivers: [
      {
        name: 'primary'
        emailAddress: alertEmailAddress
        useCommonAlertSchema: true
      }
    ]
  }
}

// a) Backend error-rate spike -- more than 10 ERROR/5xx lines in a 5-minute
// window. Same KQL as SRE_STRATEGY.md section 2a; the alert rule itself
// just fires when the query returns any rows, since the >10 threshold is
// already baked into the query.
resource alertBackendErrorRate 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = if (alertingEnabled) {
  name: '${namePrefix}-alert-backend-error-rate'
  location: location
  properties: {
    displayName: 'Backend error-rate spike'
    description: 'More than 10 ERROR/5xx log lines from `backend` in a 5-minute window.'
    severity: 2
    enabled: true
    scopes: [logAnalytics.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      allOf: [
        {
          query: '''
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "backend"
| where Log_s has "ERROR" or Log_s has "\"status_code\":5"
| summarize ErrorCount = count() by bin(TimeGenerated, 5m)
| where ErrorCount > 10
'''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [alertActionGroup.id]
    }
    autoMitigate: true
  }
}

// b) `/readyz` failing -- schema/code mismatch or DB unreachable; the case
// a liveness-only check would miss. Same KQL as SRE_STRATEGY.md section 2b.
resource alertReadyzFailing 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = if (alertingEnabled) {
  name: '${namePrefix}-alert-readyz-failing'
  location: location
  properties: {
    displayName: '/readyz failing'
    description: 'backend reported {"ready": false} at least once in a 5-minute window -- schema/code mismatch or DB unreachable.'
    severity: 1
    enabled: true
    scopes: [logAnalytics.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      allOf: [
        {
          query: '''
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "backend"
| where Log_s has "readyz" and Log_s has "\"ready\": false"
| summarize count() by bin(TimeGenerated, 5m)
'''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [alertActionGroup.id]
    }
    autoMitigate: true
  }
}

// c) Daily backup didn't run -- an absence-of-signal alert: fires if the
// success log line is MISSING in a 26-hour window, not if a failure line
// appears. Same KQL as SRE_STRATEGY.md section 2c. windowSize is 48h (wider
// than the 26h the query itself checks) so the query's own `max(TimeGenerated)`
// can actually see a success line from up to ~26h ago -- scheduledQueryRules
// only ever hands the query data from within its own windowSize, so a
// windowSize equal to or narrower than 26h would make this alert fire
// constantly regardless of whether a backup actually ran.
resource alertBackupMissing 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = if (alertingEnabled) {
  name: '${namePrefix}-alert-backup-missing'
  location: location
  properties: {
    displayName: 'Daily backup did not run'
    description: 'No successful backup log line from `backend` in the last 26 hours.'
    severity: 1
    enabled: true
    scopes: [logAnalytics.id]
    evaluationFrequency: 'PT1H'
    windowSize: 'PT48H'
    criteria: {
      allOf: [
        {
          query: '''
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "backend"
| where Log_s has "backup" and Log_s has "success"
| summarize LastSuccess = max(TimeGenerated)
| extend HoursSinceSuccess = datetime_diff('hour', now(), LastSuccess)
| where HoursSinceSuccess > 26
'''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [alertActionGroup.id]
    }
    autoMitigate: true
  }
}

// ---------------------------------------------------------------------------
// Storage Account + Azure Files -- the app's backup_data/export_data
// shares (backend/worker CSV/PDF exports and pg_dump-based backups).
// Postgres's OWN data directory lives on `postgresServer`'s managed
// storage below, NOT here -- see this file's top-of-file "WHY POSTGRES IS
// A MANAGED SERVICE" comment for why Azure Files cannot host it at all.
// Standard_LRS, classic pay-as-you-go share billing: you pay for GB
// actually stored, the `shareQuota` below is just a ceiling, not a
// reservation.
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource backupShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: 'backup-data'
  properties: { shareQuota: 10 }
}

resource exportShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: 'export-data'
  properties: { shareQuota: 10 }
}

// ---------------------------------------------------------------------------
// SINGLE SUBNET, SINGLE NSG (was: three subnets/NSGs, one per trust tier)
// ---------------------------------------------------------------------------
// See the "SINGLE MANAGED ENVIRONMENT" comment at the top of this file for
// the full story: three Managed Environments blew through this
// subscription's per-region environment quota
// (`MaxNumberOfRegionalEnvironmentsInSubExceeded`), so `frontend`/`backend`/
// `redis` + the `migrate` Job now share ONE environment, and therefore one
// delegated subnet (`postgresServer` isn't part of this at all -- it's a
// standalone managed resource outside `env`, see its own comment). An NSG
// can only filter traffic AT a subnet boundary, so with everything on one
// subnet there is no NSG rule that can allow `frontend -> backend:8000`
// while denying `frontend -> redis:6379` -- that distinction no longer
// exists at the network layer. This one NSG instead covers what's still
// true regardless of subnet layout: only the public internet -> `frontend`
// path needs to be open at all, everything else (`redis`/`backend`'s own
// `ingress.external: false`) already never gets a public IP, so it's
// unreachable from outside the VNet no matter what this NSG says.
// ---------------------------------------------------------------------------

var subnetPrefix = '10.0.0.0/23'

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${namePrefix}-nsg'
  location: location
  properties: {
    securityRules: [
      {
        // The only public entry point into the whole environment --
        // `frontend`'s external ingress. `backend`/`redis` both set
        // `ingress.external: false`, so this rule being broad (whole
        // subnet, not just frontend's IP) doesn't expose them: they simply
        // never get a public FQDN/IP for the internet to reach in the
        // first place, regardless of what this NSG allows. `postgresServer`
        // isn't on this subnet at all -- see its own resource comment.
        name: 'Allow-Internet-HTTPS-Inbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRanges: ['443', '80']
        }
      }
      {
        name: 'Allow-AzureLoadBalancer-Inbound'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: '${namePrefix}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.0.0.0/16'] }
    subnets: [
      {
        name: 'app-subnet'
        properties: {
          addressPrefix: subnetPrefix
          networkSecurityGroup: { id: nsg.id }
          delegations: [
            { name: 'Microsoft.App.environments', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// ONE Container Apps Environment shared by `frontend`, `backend`, `redis`,
// and the `migrate` Job -- see the "SINGLE MANAGED ENVIRONMENT" comment at
// the top of this file. `postgresServer` deliberately is NOT in this
// environment (it's a standalone managed resource -- see that resource's
// comment). Still Consumption plan: no fixed monthly floor, same billing
// model as before. `internal: false` because `frontend` needs a public
// FQDN; `redis`/`backend` opt out of public ingress individually via their
// own `ingress.external: false`, same as when they had a dedicated
// internal environment each.
// ---------------------------------------------------------------------------
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: vnet.properties.subnets[0].id
      internal: false // must stay externally reachable -- `frontend` is the one public entry point
    }
  }
}

resource backupStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'backup-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: backupShare.name
      accessMode: 'ReadWrite'
    }
  }
}

resource exportStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'export-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: exportShare.name
      accessMode: 'ReadWrite'
    }
  }
}

// ---------------------------------------------------------------------------
// `postgresServer` -- Azure Database for PostgreSQL Flexible Server. NOT a
// Container App, NOT inside `env`/the VNet's delegated subnet -- its own
// standalone managed resource with its own Microsoft-managed storage (see
// this file's top-of-file "WHY POSTGRES IS A MANAGED SERVICE" comment for
// why a Container-App-plus-Azure-Files `db` cannot host Postgres at all,
// regardless of sizing).
//
// Public network access + firewall rules (below), not VNet-injected
// private access: the simpler of Flexible Server's two networking modes,
// and the one that doesn't require subnet delegation/private DNS zone
// wiring on top of everything `env` already needs. Every byte still
// travels over TLS (`sslmode=require` is baked into `databaseUrl` below,
// and Flexible Server enforces SSL by default), and the firewall closes
// the server to everything except Azure's own backbone (for
// `backend`/`migrate`) plus, optionally, your own IP (`postgresAdminClientIp`,
// for direct `psql` debugging) -- nothing else can reach it regardless of
// password strength. If you later want to remove even that Azure-backbone
// exposure, Flexible Server also supports VNet-integrated private access;
// that's a larger change (delegated subnet + private DNS zone) intentionally
// left out of this cost-optimized baseline -- see Microsoft's docs on
// "Networking with private access" if you need it.
// ---------------------------------------------------------------------------
resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  // Flexible Server names must be globally unique across ALL of Azure (its
  // FQDN is `<name>.postgres.database.azure.com`) -- same constraint as
  // `storageAccountName` above, same fix: append the same per-resource-group
  // `suffix` so a plain `${namePrefix}-pg` (which WILL collide the moment
  // two different people deploy this template with the same
  // `appBaseName`/`environmentName`) doesn't cause a deployment-time naming
  // conflict.
  name: take('${namePrefix}-pg-${suffix}', 63)
  location: location
  sku: {
    name: postgresSkuName
    tier: postgresSkuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: postgresUsername
    administratorLoginPassword: postgresPassword
    storage: {
      storageSizeGB: postgresStorageGb
    }
    backup: {
      backupRetentionDays: postgresBackupRetentionDays
      geoRedundantBackup: postgresGeoRedundantBackup ? 'Enabled' : 'Disabled'
    }
    // No high availability -- ZoneRedundant/SameZone HA roughly doubles
    // compute cost (a hot standby replica billed the same as the primary).
    // Single-writer, no automatic failover, matching the same trade-off
    // the old `db` Container App made (pinned to 1 replica, no HA) -- the
    // difference is you now get automated backups/PITR either way. Revisit
    // if uptime requirements grow past what a cost-optimized deployment
    // targets.
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

// "Allow public access from Azure services" -- the well-known 0.0.0.0/0.0.0.0
// magic range Azure recognizes specifically for this purpose (it does NOT
// open the server to the public internet at large; only to Azure's own
// backbone, which is what `backend`/`migrate` connect over as Container
// Apps). This is the simplest way for `env`'s Container Apps -- which do
// NOT have static outbound IPs on the Consumption plan -- to reach a
// publicly-networked Flexible Server without VNet integration.
resource postgresFirewallAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgresServer
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// Optional extra firewall rule for direct `psql`/pgAdmin/etc. access from
// your own machine -- see `postgresAdminClientIp`'s param description.
// Skipped entirely (no resource created) when that parameter is left empty.
resource postgresFirewallAllowAdminIp 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!empty(postgresAdminClientIp)) {
  parent: postgresServer
  name: 'AllowAdminClientIp'
  properties: {
    startIpAddress: postgresAdminClientIp
    endIpAddress: postgresAdminClientIp
  }
}

// The actual application database. Flexible Server provisions a default
// `postgres` database on create, but this app's `DATABASE_URL` (below)
// always points at `asset_db` specifically -- same name the old `db`
// Container App used, and same name local Docker Compose/Render use, so
// no application code needed to change.
resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: 'asset_db'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// "Allow public access from Azure services" -- the well-known 0.0.0.0/0.0.0.0
// magic range Azure recognizes specifically for this purpose (it does NOT
// open the server to the public internet at large; only to Azure's own
// backbone, which is what `backend`/`migrate` connect over as Container
// Apps). This is the simplest way for `env`'s Container Apps -- which do
// NOT have static outbound IPs on the Consumption plan -- to reach a
// publicly-networked Flexible Server without VNet integration.
resource postgresFirewallAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgresServer
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// Optional extra firewall rule for direct `psql`/pgAdmin/etc. access from
// your own machine -- see `postgresAdminClientIp`'s param description.
// Skipped entirely (no resource created) when that parameter is left empty.
resource postgresFirewallAllowAdminIp 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!empty(postgresAdminClientIp)) {
  parent: postgresServer
  name: 'AllowAdminClientIp'
  properties: {
    startIpAddress: postgresAdminClientIp
    endIpAddress: postgresAdminClientIp
  }
}

// The actual application database. Flexible Server provisions a default
// `postgres` database on create, but this app's `DATABASE_URL` (below)
// always points at `asset_db` specifically -- same name the old `db`
// Container App used, and same name local Docker Compose/Render use, so
// no application code needed to change.
resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: 'asset_db'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// ---------------------------------------------------------------------------
// `redis` -- official image from Docker Hub. Internal-only TCP ingress
// (`ingress.external: false`), in the shared `env` -- never gets a public
// FQDN, so it's unreachable from the internet regardless of the NSG.
// `postgresServer` is deliberately NOT in this environment (see this
// file's "WHY POSTGRES IS A MANAGED SERVICE" comment) -- `redis` is the
// only stateful piece still running as a Container App here. No persistent
// volume (see top-of-file comment on why that's an
// acceptable trade for this app's Redis usage) -- an in-memory cache/broker
// that resets on restart, exactly like Render's free Key Value tier.
// ---------------------------------------------------------------------------
resource redisApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'redis'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: [
        { name: 'redis-password', value: redisPassword }
      ]
      ingress: {
        external: false
        transport: 'tcp'
        targetPort: 6379
        exposedPort: 6379
      }
    }
    template: {
      containers: [
        {
          name: 'redis'
          image: 'redis:7-alpine'
          command: ['sh', '-c']
          args: ['redis-server --requirepass "$REDIS_PASSWORD" --appendonly no --maxmemory 200mb --maxmemory-policy allkeys-lru']
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'REDIS_PASSWORD', secretRef: 'redis-password' }
          ]
          probes: [
            {
              type: 'Liveness'
              tcpSocket: { port: 6379 }
              initialDelaySeconds: 5
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1 // NEVER raise this -- Celery beat schedule assumes one broker instance, and there's no clustering/persistence here anyway
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Shared env vars -- reused by `backend` (the live service) and `migrate`
// (the one-shot alembic job, same image as `backend`).
// ---------------------------------------------------------------------------
// `redis`/`backend`/`frontend` all live in the same shared `env` (see
// "SINGLE MANAGED ENVIRONMENT" above), so the short in-environment DNS
// name (just the app name, e.g. "redis") resolves fine for them.
// `postgresServer` is NOT in `env` (it's a standalone managed resource --
// see "WHY POSTGRES IS A MANAGED SERVICE" above), so it needs its own
// public FQDN instead, and `sslmode=require` since that FQDN is reached
// over the public internet/Azure backbone, not an internal-only network --
// Flexible Server also enforces SSL server-side regardless of this flag.
// `uriComponent()` percent-encodes the password so any of the symbol
// characters Flexible Server's complexity rule expects (see
// `postgresPassword`'s param description) can't break the URL's syntax.
var databaseUrl = 'postgresql://${postgresUsername}:${uriComponent(postgresPassword)}@${postgresServer.properties.fullyQualifiedDomainName}:5432/asset_db?sslmode=require'
var redisUrl = 'redis://:${redisPassword}@redis:6379/0'
var frontendFqdn = 'frontend.${env.properties.defaultDomain}'
var publicOrigin = empty(customDomain) ? 'https://${frontendFqdn}' : 'https://${customDomain}'
// Only a valid expression to evaluate when `appInsights` actually exists
// (otelAzureMonitorEnabled=true) -- the ternary's false branch never
// touches the conditionally-deployed resource, which is what makes this
// safe to reference even when it wasn't provisioned this deploy.
var appInsightsConnectionString = otelAzureMonitorEnabled ? appInsights.properties.ConnectionString : ''

var sharedEnv = [
  { name: 'ENVIRONMENT', value: 'production' }
  { name: 'EXPORT_RESULT_DIR', value: '/app/export_results' }
  { name: 'JWT_ALGORITHM', value: 'HS256' }
  { name: 'JWT_EXPIRY_HOURS', value: '12' }
  { name: 'SITE_NAME', value: siteName }
  { name: 'AUTO_INIT_DB', value: 'false' }
  { name: 'AUTO_SEED_DEMO_DATA', value: 'false' }
  { name: 'LOG_LEVEL', value: logLevel }
  { name: 'LOG_FORMAT', value: 'json' }
  { name: 'LOGIN_RATE_LIMIT_MAX', value: string(loginRateLimitMax) }
  { name: 'LOGIN_RATE_LIMIT_WINDOW_SECONDS', value: string(loginRateLimitWindowSeconds) }
  { name: 'ACCOUNT_LOCKOUT_MAX_ATTEMPTS', value: string(accountLockoutMaxAttempts) }
  { name: 'ACCOUNT_LOCKOUT_DURATION_MINUTES', value: string(accountLockoutDurationMinutes) }
  { name: 'ENABLE_API_DOCS', value: string(enableApiDocs) }
  { name: 'SUPER_ADMIN_USERNAME', value: superAdminUsername }
  { name: 'SUPER_ADMIN_NAME', value: superAdminName }
  { name: 'NOTIFICATIONS_ENABLED', value: string(notificationsEnabled) }
  { name: 'SMTP_HOST', value: smtpHost }
  { name: 'SMTP_PORT', value: string(smtpPort) }
  { name: 'SMTP_USERNAME', value: smtpUsername }
  { name: 'SMTP_USE_TLS', value: string(smtpUseTls) }
  { name: 'SMTP_USE_SSL', value: string(smtpUseSsl) }
  { name: 'SMTP_FROM_EMAIL', value: smtpFromEmail }
  { name: 'ADMIN_NOTIFICATION_EMAILS', value: adminNotificationEmails }
  { name: 'OVERDUE_NOTIFICATION_INTERVAL_HOURS', value: overdueNotificationIntervalHours }
  { name: 'DUE_SOON_REMINDER_DAYS', value: string(dueSoonReminderDays) }
  { name: 'DUE_SOON_NOTIFICATION_INTERVAL_HOURS', value: dueSoonNotificationIntervalHours }
  { name: 'SEND_INDIVIDUAL_HOLDER_REMINDERS', value: string(sendIndividualHolderReminders) }
  { name: 'DISPLAY_TIMEZONE', value: displayTimezone }
  { name: 'CURRENCY_CODE', value: currencyCode }
  { name: 'CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER', value: string(catalogShowStockToStaffCustomer) }
  { name: 'ENABLE_AUTO_BACKUP', value: 'true' }
  { name: 'BACKUP_HOURS_UTC', value: backupHoursUtc }
  { name: 'BACKUP_DIR', value: '/app/backups' }
  { name: 'BACKUP_RETENTION_COUNT', value: string(backupRetentionCount) }
  { name: 'BACKUP_GDRIVE_ENABLED', value: string(gdriveBackupEnabled) }
  { name: 'BACKUP_GDRIVE_OAUTH_CLIENT_ID', value: gdriveOauthClientId }
  { name: 'BACKUP_GDRIVE_FOLDER_ID', value: gdriveFolderId }
  // Operations & Observability requirement #4: distributed tracing -- see
  // backend/telemetry.py's module docstring and the `otelEnabled`/
  // `otelAzureMonitorEnabled` params above. OTEL_EXPORTER_OTLP_HEADERS and
  // APPLICATIONINSIGHTS_CONNECTION_STRING are NOT here -- both commonly
  // carry a credential, so they're Container Apps secrets instead (see
  // `sharedSecrets`/`sharedSecretEnvRefs` below).
  { name: 'OTEL_ENABLED', value: string(otelEnabled) }
  { name: 'OTEL_SERVICE_NAME', value: otelServiceName }
  { name: 'OTEL_EXPORTER_OTLP_ENDPOINT', value: otelExporterOtlpEndpoint }
  { name: 'OTEL_TRACES_SAMPLE_RATIO', value: otelTracesSampleRatio }
]

var sharedSecrets = concat([
  { name: 'jwt-secret-key', value: jwtSecretKey }
  { name: 'root-admin-bootstrap-password', value: rootAdminBootstrapPassword }
  { name: 'database-url', value: databaseUrl }
  { name: 'redis-url', value: redisUrl }
  { name: 'smtp-password', value: empty(smtpPassword) ? 'unset' : smtpPassword }
  { name: 'gdrive-oauth-client-secret', value: empty(gdriveOauthClientSecret) ? 'unset' : gdriveOauthClientSecret }
  { name: 'gdrive-oauth-refresh-token', value: empty(gdriveOauthRefreshToken) ? 'unset' : gdriveOauthRefreshToken }
  { name: 'otel-exporter-otlp-headers', value: empty(otelExporterOtlpHeaders) ? 'unset' : otelExporterOtlpHeaders }
  { name: 'applicationinsights-connection-string', value: empty(appInsightsConnectionString) ? 'unset' : appInsightsConnectionString }
], usePrivateDockerHubRepo ? [
  { name: 'dockerhub-token', value: dockerHubToken }
] : [])

var sharedSecretEnvRefs = [
  { name: 'JWT_SECRET_KEY', secretRef: 'jwt-secret-key' }
  // Only ever read by the `migrate` Job below, and only the very first
  // time it runs (see backend/alembic/versions/0002_bootstrap_root_admin.py)
  // -- harmless to also hand to backend/frontend/worker/beat, which simply
  // never read it, same as several other env vars in this shared list.
  { name: 'ROOT_ADMIN_BOOTSTRAP_PASSWORD', secretRef: 'root-admin-bootstrap-password' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'REDIS_URL', secretRef: 'redis-url' }
  { name: 'SMTP_PASSWORD', secretRef: 'smtp-password' }
  { name: 'BACKUP_GDRIVE_OAUTH_CLIENT_SECRET', secretRef: 'gdrive-oauth-client-secret' }
  { name: 'BACKUP_GDRIVE_OAUTH_REFRESH_TOKEN', secretRef: 'gdrive-oauth-refresh-token' }
  { name: 'OTEL_EXPORTER_OTLP_HEADERS', secretRef: 'otel-exporter-otlp-headers' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'applicationinsights-connection-string' }
]

var registries = usePrivateDockerHubRepo ? [
  { server: 'index.docker.io', username: dockerHubUsername, passwordSecretRef: 'dockerhub-token' }
] : []

// ---------------------------------------------------------------------------
// `backend` -- FastAPI + embedded Celery worker/beat (backend/Dockerfile,
// RUN_EMBEDDED_WORKER=true). INTERNAL-ONLY ingress (`ingress.external:
// false`), in the shared `env` -- never gets a public FQDN, so the public
// internet never talks to it directly; only `frontend`'s reverse proxy and
// the `migrate` Job ever call it (see "SINGLE MANAGED ENVIRONMENT" above
// for why this is app-layer isolation now, not a subnet/NSG wall). Scales
// 0-N independent of `frontend` -- see top-of-file comment for why Redis is
// what makes this safe past 1 replica.
// ---------------------------------------------------------------------------
resource backendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'backend'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      secrets: sharedSecrets
      ingress: {
        external: false
        targetPort: 8000
        transport: 'auto'
        // BUG FIX -- root cause of "Roll out new revisions" smoke-test
        // failures ("backend could not be resolved (3: Host not found)"
        // in nginx's error log, HTTP 502 on /api/*): this was previously
        // left unset, which defaults to `false`. With `allowInsecure:
        // false`, Container Apps' shared Envoy proxy layer answers plain
        // HTTP requests to `backend`'s internal FQDN/app-name with a
        // redirect to HTTPS instead of actually proxying them -- but that
        // was never even the request that failed, because nginx couldn't
        // resolve the hostname AT ALL before getting that far (see
        // `frontendApp` below for the actual DNS half of this bug and why
        // both halves have to be fixed together). Once DNS resolution is
        // fixed there, nginx's `proxy_pass http://$backend_upstream...`
        // (see nginx/default.conf.template) still speaks plain HTTP, not
        // HTTPS -- so `backend` also needs to accept plain HTTP through
        // the proxy for that connection to actually succeed end-to-end.
        // This only affects traffic between apps INSIDE the environment
        // (`backend` still has `external: false`, so it's never reachable
        // from the public internet either way) -- true e2e TLS between
        // `frontend` and `backend` isn't in scope for this fix.
        allowInsecure: true
      }
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(sharedEnv, sharedSecretEnvRefs, [
            // BUG FIX: this was previously a no-op -- backend/start.sh
            // didn't read RUN_EMBEDDED_WORKER at all, so audit exports
            // queued into Redis with nothing consuming them and the
            // notification digest never fired. start.sh now actually
            // launches the embedded worker (see its own comments and
            // celery_app.py's RedBeat config, which is what makes this
            // safe even as `backend` scales to more than one replica).
            { name: 'RUN_EMBEDDED_WORKER', value: 'true' }
            // No SERVE_FRONTEND here -- `frontend` serves the static build
            // now, `backend` is API-only. CORS_ORIGINS still set (defense
            // in depth / anything that ever calls `backend` directly), even
            // though normal browser traffic never leaves `frontend`'s origin.
            { name: 'CORS_ORIGINS', value: publicOrigin }
          ])
          volumeMounts: [
            { volumeName: 'backup-data', mountPath: '/app/backups' }
            { volumeName: 'export-data', mountPath: '/app/export_results' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/readyz', port: 8000 }, initialDelaySeconds: 5, periodSeconds: 10 }
          ]
        }
      ]
      volumes: [
        { name: 'backup-data', storageType: 'AzureFile', storageName: 'backup-data' }
        { name: 'export-data', storageType: 'AzureFile', storageName: 'export-data' }
      ]
      scale: {
        minReplicas: backendMinReplicas
        maxReplicas: backendMaxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: { metadata: { concurrentRequests: '50' } }
          }
        ]
      }
    }
  }
  // `postgresDatabase`/`redisApp` because `backend` needs them reachable
  // at boot (Bicep already infers a dependency on `postgresServer` itself
  // through `sharedSecrets`' symbolic reference to it inside `databaseUrl`,
  // but `postgresDatabase` -- the `asset_db` database specifically -- is a
  // separate child resource with no symbolic reference anywhere in
  // `backend`'s own properties, so it needs to be listed explicitly here
  // too). The volumes above have a similar missing-implicit-dependency
  // issue -- `backupStorage`/`exportStorage` are referenced by plain
  // string, so Bicep won't otherwise wait for them before creating
  // `backend`.
  dependsOn: [
    postgresDatabase
    redisApp
    backupStorage
    exportStorage
  ]
}

// ---------------------------------------------------------------------------
// `frontend` -- frontend/Dockerfile, UNMODIFIED from local Docker Compose:
// serves the static frontend build AND reverse-proxies /api/* to `backend`
// over the shared environment's internal DNS
// (nginx/default.conf.template's BACKEND_HOST/BACKEND_PORT env vars --
// resolver auto-detected at boot, see
// nginx/docker-entrypoint.d/15-detect-resolver-ip.sh). In the shared `env`
// -- the ONLY app here with `ingress.external: true`, so it's the sole
// externally-reachable app (see "SINGLE MANAGED ENVIRONMENT" above for why
// that app-layer setting, not a subnet/NSG wall, is what now enforces
// this). Scales 0-N independent of `backend`.
// ---------------------------------------------------------------------------
resource frontendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'frontend'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      // `frontend` never touches the database, Redis, JWTs, or SMTP, so it
      // gets none of `sharedSecrets` -- but Container Apps still requires
      // any secret referenced by `registries[].passwordSecretRef` (here,
      // 'dockerhub-token') to be declared in THIS app's own `secrets` list,
      // not just somewhere else in the template. Without this, deploying
      // with a private Docker Hub repo (dockerHubUsername set) fails with
      // "ContainerAppRegistriesPasswordSecretRefNotFound: PasswordSecretRef
      // 'dockerhub-token' defined for registry server 'index.docker.io' not
      // found" on this app specifically -- `backendApp`/`migrateJob` never
      // hit this because they already pass the full `sharedSecrets` (which
      // conditionally includes 'dockerhub-token') here.
      secrets: usePrivateDockerHubRepo ? [
        { name: 'dockerhub-token', value: dockerHubToken }
      ] : []
      ingress: {
        external: true
        targetPort: 80
        transport: 'auto'
        allowInsecure: false
        customDomains: empty(customDomain) ? [] : [
          { name: customDomain, bindingType: 'SniEnabled' }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'PORT', value: '80' }
            // BUG FIX -- root cause of "Roll out new revisions" smoke-test
            // failures: nginx logged "backend could not be resolved (3:
            // Host not found)" and every /api/* request 502'd, even
            // though `frontend` and `backend` share one environment and
            // Azure's own docs say the bare app name (`http://backend`)
            // "just resolves". It does -- for plain OS-level DNS clients
            // (curl, this app's own smoke test, Python's requests, etc.),
            // which all consult /etc/resolv.conf's `search` domain list
            // and silently qualify an unqualified single-label name like
            // "backend" before querying (see resolv.conf(5), "ndots"/
            // domain search path). nginx's `resolver` directive
            // (nginx/default.conf.template -- needed here so a stale IP
            // from a `backend` redeploy doesn't get cached forever, see
            // that file's own comment) does its OWN raw DNS queries and
            // deliberately bypasses glibc entirely, so it NEVER reads or
            // applies that search-domain list -- it queries exactly the
            // literal string it's given, "backend", which doesn't exist
            // as its own top-level DNS record and so comes back NXDOMAIN.
            // This is a known, documented nginx-specific gotcha on every
            // platform that relies on search-domain expansion for
            // short-name service discovery (Kubernetes, DigitalOcean App
            // Platform, and Azure Container Apps alike) -- the fix
            // everywhere is the same: hand nginx the FULLY QUALIFIED name
            // instead, which needs no search-list expansion at all.
            // `backendApp.properties.configuration.ingress.fqdn` is
            // exactly that -- `backend.internal.<env-id>.<region>.
            // azurecontainerapps.io` -- so this also creates an explicit
            // Bicep dependency on `backendApp` (on top of the `dependsOn`
            // it already has for other reasons), guaranteeing `backend`'s
            // ingress FQDN is known before `frontend` deploys.
            { name: 'BACKEND_HOST', value: backendApp.properties.configuration.ingress.fqdn }
            // Calls between container apps in the same environment --
            // whether by bare app name or by FQDN -- go through the
            // environment's shared Envoy proxy on the STANDARD web port,
            // not the backend container's own `targetPort: 8000` (that
            // port is an implementation detail Envoy forwards to
            // internally; it's never exposed as a literal port number to
            // other apps calling in). Port 8000 here was doubly wrong:
            // even once DNS resolution above is fixed, connecting to
            // "<backend FQDN>:8000" would still fail (nothing listens on
            // 8000 at that address) or connect to the wrong thing.
            // Plain port 80 (not 443) because nginx's proxy_pass speaks
            // plain HTTP -- see `backendApp`'s `allowInsecure: true` above,
            // which is what makes plain HTTP on this port actually work
            // instead of getting redirected to HTTPS.
            { name: 'BACKEND_PORT', value: '80' }
            { name: 'ENABLE_API_DOCS', value: string(enableApiDocs) } // must match backend's own value -- see nginx/default.conf.template's /docs passthrough gating
            // RESOLVER_IP deliberately NOT set -- nginx/docker-entrypoint.d/15-detect-resolver-ip.sh
            // reads it from Container Apps' own /etc/resolv.conf at boot.
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/', port: 80 }, initialDelaySeconds: 5, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/', port: 80 }, initialDelaySeconds: 5, periodSeconds: 10 }
          ]
        }
      ]
      scale: {
        minReplicas: frontendMinReplicas
        maxReplicas: frontendMaxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: { metadata: { concurrentRequests: '100' } } // higher than backend's -- static/proxy responses are cheap, one replica handles more concurrent requests before needing to scale
          }
        ]
      }
    }
  }
  dependsOn: [
    backendApp
  ]
}

// ---------------------------------------------------------------------------
// `migrate` -- Container Apps Job running `alembic upgrade head` against
// `backend`'s own image, as an explicit, one-shot step, triggered by the
// CI/CD pipeline BEFORE `backend`'s new image is rolled out. Jobs only bill
// for the seconds they actually run -- zero standing cost.
// ---------------------------------------------------------------------------
resource migrateJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'migrate'
  location: location
  properties: {
    // Runs from the same shared `env` as `backend` (for its own short
    // in-environment DNS resolution needs). `postgresServer` is reached
    // over its public FQDN regardless of which environment this job runs
    // in -- see `databaseUrl` above.
    environmentId: env.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 1
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: registries
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: backendImage
          command: ['alembic', 'upgrade', 'head']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(sharedEnv, sharedSecretEnvRefs)
        }
      ]
    }
  }
  dependsOn: [
    postgresDatabase
  ]
}

// ---------------------------------------------------------------------------
// Outputs -- consumed by the GitHub Actions deploy workflows.
// ---------------------------------------------------------------------------
output envName string = env.name
output frontendFqdn string = frontendApp.properties.configuration.ingress.fqdn
output frontendAppName string = frontendApp.name
output backendAppName string = backendApp.name
output postgresServerName string = postgresServer.name
output postgresServerFqdn string = postgresServer.properties.fullyQualifiedDomainName
output redisAppName string = redisApp.name
output migrateJobName string = migrateJob.name
output logAnalyticsWorkspaceId string = logAnalytics.id
// Empty string (not an error) when otelAzureMonitorEnabled=false -- the
// resource simply wasn't provisioned this deploy. Deliberately NOT
// outputting the connection string itself here -- `az deployment group
// show` outputs land in plain-text GitHub Actions logs/local shell
// history, the exact thing `sharedSecrets` above avoids for every other
// credential in this file. Look it up instead with:
//   az monitor app-insights component show --app <output below> \
//     --resource-group <your-rg> --query connectionString -o tsv
output appInsightsName string = otelAzureMonitorEnabled ? appInsights.name : ''
