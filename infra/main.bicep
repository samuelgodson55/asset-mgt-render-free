// =============================================================================
// infra/main.bicep
// -----------------------------------------------------------------------------
// Cost-optimized Azure Container Apps deployment for Snipe-IT Lite.
//
// PROVISIONS
//   - Log Analytics workspace (Container Apps logs)
//   - Storage Account + 2 Azure Files shares (backup_data, export_data)
//   - VNet (1 delegated subnet + NSG) + 1 Container Apps Environment (Consumption plan)
//   - Azure Database for PostgreSQL Flexible Server (`postgresServer`) -- a
//     managed service, not a Container App, because Azure Files (the volume
//     type Container Apps uses for persistent storage) doesn't support the
//     POSIX permissions Postgres's `initdb` requires on its data directory.
//     Small General Purpose SKU by default so Azure-managed PgBouncer is available (see `postgresSkuName`).
//   - 4 Container Apps, all in ONE environment (multiple environments hit a
//     per-subscription quota on Free Trial/starter subscriptions):
//       `redis`    -- redis:7-alpine, internal-only, 1 replica, no persistence
//       `backend`  -- FastAPI + embedded Celery worker/beat, internal-only,
//                     scales 0-N
//       `frontend`  -- static frontend + reverse proxy to `backend`, the ONLY
//                     public-facing app, scales 0-N independently of `backend`
//       `errorbeacon` -- isolated internal ErrorBeacon relay, not part of
//                     backend/frontend blue-green traffic, min 1 replica
//   - 1 Container Apps Job: `migrate` (runs `alembic upgrade head`, triggered
//     by CI/CD before each backend rollout)
//
// No Azure Container Registry (images pulled from Docker Hub -- a free
// account includes one private repo, so keep at least one of
// backend/frontend public unless you're on a paid plan), no Key Vault (plain
// Container Apps secrets), no managed identity, no Application Insights by
// default (opt in via `otelAzureMonitorEnabled` for OpenTelemetry tracing --
// see README.md's "Distributed Tracing" section).
//
// Network isolation is at the application layer, not the network layer:
// `redis`/`backend` set `ingress.external: false` (no public FQDN, only
// `frontend` gets one) and `postgresServer` gates access via its own
// firewall rules. All apps in the environment share one subnet, so there's
// no per-app network segmentation -- acceptable for most early-stage
// deployments; request an Azure quota increase and split environments
// yourself if you need it.
//
// See DEPLOYMENT.md's Cost section for the full monthly cost breakdown.
//
// USAGE
// ---------------------------------------------------------------------------
//   az stack group create \
//     --name snipeit-lite-prod \
//     --resource-group rg-snipeit-lite-prod \
//     --template-file infra/main.bicep \
//     --parameters environmentName=prod \
//                  dockerHubBackendImage=yourdockerhubusername/snipeit-lite-backend \
//                  dockerHubFrontendImage=yourdockerhubusername/snipeit-lite-frontend \
//                  postgresPassword=$(openssl rand -base64 24) \
//                  redisPassword=$(openssl rand -hex 16) \
//                  jwtSecretKey=$(openssl rand -hex 32) \
//                  rootAdminBootstrapPassword=$(openssl rand -base64 24)
//                  # postgresPassword MUST satisfy Flexible Server's password
//                  # complexity rule (8-128 chars, at least 3 of: uppercase,
//                  # lowercase, digit, symbol) -- use base64, not hex, since
//                  # hex output is only digits + a-f.
//                  # rootAdminBootstrapPassword is optional -- omit to let
//                  # the migrate Job generate one and print it to stderr once
//                  # (see DEPLOYMENT.md's Monitoring section for how to read
//                  # it back out of Log Analytics).
//
// Re-run the same stack command any time to update the environment idempotently --
// this file does NOT set `backend`/`frontend`/`migrate`'s image tags on every
// run (that's the CI/CD pipeline's job via `az containerapp update --image`),
// so deploying new code never requires a full infra re-deploy.
// =============================================================================

@description('Short environment name: "prod" or "staging". Prefixes every resource name, and drives the ENVIRONMENT runtime env var below (sharedEnv) -- "prod" -> "production", "staging" -> "development". Defaults to "prod" -- the infrastructure default across this whole repo (mirrors infra-vm/variables.tf\'s environment_name default, deploy-azure-vm.yml\'s/deploy-azure-aca.yml\'s dropdown defaults, and docker-compose.vm.yml\'s own ENVIRONMENT fallback -- see each of their own comments). .github/workflows/infra-deploy.yml always passes this explicitly (`required: true`, no `default:` on that workflow_dispatch input), so this default only matters for a manual/local `az deployment group create` run that forgets to pass -p environmentName=... -- it provisions prod-named resources rather than silently standing up a staging environment nobody asked for.')
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

@description('Docker Hub repository for the isolated ErrorBeacon monitoring image, e.g. "yourusername/errorbeacon-lite". The monitor is deployed as its own Container App and is not part of backend/frontend blue-green traffic. Defaults to the project\'s public ErrorBeacon image.')
param dockerHubErrorBeaconImage string = 'samuelgodson55/errorbeacon-lite'

@description('Enable the isolated ErrorBeacon Container App. Production workflows enable this by default. Set false only when intentionally disabling monitoring.')
param errorBeaconEnabled bool = true

@description('Identity string backend/worker/beat report themselves as via the ERRORBEACON_APP env var -- shows up as the "app" field on every event ErrorBeacon receives. Matches docker-compose.yml/docker-compose.vm.yml\'s own ERRORBEACON_APP default so all three deployment paths report under the same identity unless intentionally overridden.')
param errorBeaconAppName string = 'asset-inventory-quotes'

@secure()
param errorBeaconIngestApiKey string = ''
@secure()
param errorBeaconAdminApiKey string = ''

@secure()
param errorBeaconTelegramBotToken string = ''
@secure()
param errorBeaconTelegramChatId string = ''
param errorBeaconTelegramThreadId string = ''
@secure()
param errorBeaconGeminiApiKey string = ''
param errorBeaconGeminiModel string = 'gemini-2.5-flash-lite'
param errorBeaconGeminiFallbackModel string = 'gemini-2.5-flash'
@secure()
param errorBeaconGroqApiKey string = ''
param errorBeaconGroqModel string = 'llama-3.1-8b-instant'
@secure()
param errorBeaconOpenRouterApiKey string = ''
param errorBeaconOpenRouterModel string = 'openrouter/free'
// NOTE: no separate errorBeaconOpenRouterSiteUrl param -- redundant with
// `customDomain` (below), which is already the public origin of this exact
// deployment. `publicOrigin` (computed from customDomain, defined further
// down) is what the errorbeacon Container App's OPENROUTER_SITE_URL env var
// uses directly instead.

@description('Backward-compatible common image tag. Used only when initialBackendImageTag/initialFrontendImageTag are omitted.')
param initialImageTag string = 'latest'

@description('Image tag to use for the backend during infrastructure creation/refresh. On refresh, the infra workflow passes the exact tag currently deployed instead of falling back to latest.')
param initialBackendImageTag string = ''

@description('Image tag to use for the frontend during infrastructure creation/refresh. On refresh, the infra workflow passes the exact tag currently deployed instead of falling back to latest.')
param initialFrontendImageTag string = ''

@description('Internal-ingress FQDN of the currently-deployed `backend` Container App, resolved by infra-deploy.yml via `az containerapp show` BEFORE this deployment runs (see that workflow\'s "Resolve current ACA images" step). Lets ErrorBeacon\'s Telegram /apphealth command reach backend\'s /healthz, /readyz and /health/dependencies. This is a plain string parameter, not a symbolic `backendApp.properties...` reference, because `backendApp` already depends on `errorBeaconApp` (through errorBeaconUrl below) -- a reference back from `errorBeaconApp` to `backendApp` would be a circular resource dependency. Empty on the very first deploy of a new environment (backend does not exist yet); backendInternalUrl below and ErrorBeacon\'s own BACKEND_URL handling both treat empty as "not configured" rather than erroring.')
param existingBackendFqdn string = ''

@description('ACA backend ingress traffic to preserve during infrastructure deployment. The deployment workflow resolves latestRevision routing to explicit revision names before passing this value, so an infra refresh cannot redirect production traffic to an infra-created revision.')
param backendTraffic array = [
  {
    latestRevision: true
    weight: 100
  }
]

@description('ACA frontend ingress traffic to preserve during infrastructure deployment. The deployment workflow resolves latestRevision routing to explicit revision names before passing this value, so an infra refresh cannot redirect production traffic to an infra-created revision.')
param frontendTraffic array = [
  {
    latestRevision: true
    weight: 100
  }
]

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

@description('Flexible Server compute SKU. Standard_D2s_v3 (2 vCores/8GiB, General Purpose) is the default because Azure managed PgBouncer is supported on General Purpose and Memory Optimized tiers, not Burstable. Change this deliberately through the GitHub POSTGRES_SKU_NAME variable if your environment requires another supported SKU.')
param postgresSkuName string = 'Standard_D2s_v3'

@description('Flexible Server compute tier matching `postgresSkuName`. PgBouncer requires GeneralPurpose or MemoryOptimized when USE_PGBOUNCER=true.')
@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
param postgresSkuTier string = 'GeneralPurpose'

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
param backendMinReplicas int = environmentName == 'prod' ? 1 : 0

@description('Maximum `backend` replicas under load. NOTE: `backend` embeds Celery worker+beat in-process (see RUN_EMBEDDED_WORKER below) since there is no separate worker/beat Container App in this cost-optimized layout. That is safe at any replica count: celery_app.py configures RedBeat as the Beat scheduler, which keeps a distributed lock in Redis so only one replica is ever the active scheduler at a time (automatic failover if that replica dies) -- no per-replica configuration needed here.')
param backendMaxReplicas int = 3
@description('Route application DB traffic through the supported pooler for this deployment. For ACA with Azure Flexible Server this enables Azure managed PgBouncer on port 6432; for local/VM Compose the service-level pooler is used. The default is true; set false only as a deliberate break-glass fallback.')
param usePgbouncer bool = true

@description('Optional Azure Managed PgBouncer server-side pool size per user/database pair. Set to 0 to auto-derive a conservative value from the Azure PostgreSQL compute SKU (4x vCores, within Azure\'s recommended 2-5x-vCore starting range). The application also applies a separate safety margin and live max_connections cap. Set an explicit value only when deliberately tuning PgBouncer.')
@minValue(0)
param pgbouncerServerPoolSize int = 0

// Azure's own guidance recommends starting PgBouncer conservatively at roughly
// 2-5x vCores and then tuning from real workload metrics. Keep the default at
// 4x vCores, rather than assuming Azure's generic default_pool_size=50. This
// mapping covers the supported SKUs used by this deployment; an unknown SKU
// deliberately falls back to 2 vCores so a new/renamed SKU cannot silently
// create an oversized pool.
var postgresSkuVcores = {
  Standard_B1ms: 1
  Standard_B2s: 2
  Standard_B2ms: 2
  Standard_B4ms: 4
  Standard_B8ms: 8
  Standard_D2s_v3: 2
  Standard_D4s_v3: 4
  Standard_D8s_v3: 8
  Standard_D16s_v3: 16
  Standard_D32s_v3: 32
  Standard_D48s_v3: 48
  Standard_D64s_v3: 64
  Standard_E2s_v3: 2
  Standard_E4s_v3: 4
  Standard_E8s_v3: 8
  Standard_E16s_v3: 16
  Standard_E20s_v3: 20
  Standard_E32s_v3: 32
  Standard_E48s_v3: 48
  Standard_E64s_v3: 64
}
var detectedPostgresVcores = contains(postgresSkuVcores, postgresSkuName) ? int(postgresSkuVcores[postgresSkuName]) : 2
var autoPgbouncerServerPoolSize = max(1, detectedPostgresVcores * 4)
var effectivePgbouncerServerPoolSize = pgbouncerServerPoolSize > 0 ? pgbouncerServerPoolSize : autoPgbouncerServerPoolSize

@description('Minimum `frontend` replicas. 0 = scale-to-zero (cold start on first request after idle -- static-file + proxy responses are fast, so it\'s much shorter than `backend`\'s, but not zero). 1 = always warm, no cold start, small extra cost. `infra-deploy.yml` passes 1 here for production and 0 for staging -- see that workflow\'s "Resolve replica floors" step -- so this parameter\'s own default only applies to a manual/direct bicep deploy that skips the pipeline.')
param frontendMinReplicas int = environmentName == 'prod' ? 1 : 0

@description('Maximum `frontend` replicas under load.')
param frontendMaxReplicas int = 3

@description('Custom domain for `frontend`, the public entry point (leave empty to use the generated *.azurecontainerapps.io FQDN only).')
param customDomain string = ''

@description('Resource ID of an EXISTING `Microsoft.App/managedEnvironments/managedCertificates` (or Key Vault-backed certificate) resource that already covers `customDomain`, e.g. from `az containerapp env certificate list`. This template does not provision the certificate itself -- a managed certificate requires the domain\'s CNAME/TXT ownership validation to already be in place, which only exists once the domain is live, so it has to be created out-of-band (Portal "Add custom domain" wizard, or `az containerapp hostname bind` / `az containerapp env certificate create`) the FIRST time a given customDomain goes live, same as before this parameter existed. Once that one-time step is done, pass the resulting certificate resource ID here (infra-deploy.yml\'s "Resolve existing managed certificate" step does this lookup automatically on every run) so repeat `main.bicep` deployments stay idempotent instead of trying to rebind `customDomain` with no `certificateId`, which Azure rejects with "CertificateId property is missing for customDomain". Leave empty (the default) while `customDomain` has no certificate yet -- `frontend` simply keeps serving off its default *.azurecontainerapps.io FQDN until this is set, same as leaving `customDomain` itself empty.')
param customDomainCertificateId string = ''

@description('Notification / SMTP settings -- enabled by default; configure the mail transport credentials for delivery.')
param notificationsEnabled bool = true
param smtpHost string = ''
param smtpUsername string = ''
@secure()
param smtpPassword string = ''
param smtpFromEmail string = ''
param adminNotificationEmails string = ''

@description('Which transport send_email() uses: "smtp" (default), or an HTTP-API provider ("brevo"/"resend") for when outbound SMTP ports are blocked. Matches .env.example\'s EMAIL_PROVIDER and backend/config.py\'s EMAIL_PROVIDER docstring.')
param emailProvider string = 'smtp'
@description('https://app.brevo.com/settings/keys/api -- only read when emailProvider is "brevo". Matches .env.example\'s BREVO_API_KEY.')
@secure()
param brevoApiKey string = ''
@description('https://resend.com/api-keys -- only read when emailProvider is "resend". Matches .env.example\'s RESEND_API_KEY.')
@secure()
param resendApiKey string = ''

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

// Non-sensitive config below is read as `vars.X` (not `secrets.X`) in
// infra-deploy.yml, same pattern as postgresSkuName/postgresStorageGb above.

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

@description('Comma-separated hours of day (UTC, each 0-23) the worker checks for overdue checkouts and emails the admin/manager digest, e.g. "8" or "8,20". Matches .env.example\'s OVERDUE_DIGEST_HOURS_UTC.')
param overdueDigestHoursUtc string = '8'

@description('How many days ahead of its due_date an active checkout counts as "due soon" -- drives the dashboard banner, the My Items badge, and the due-soon reminder email. Matches .env.example\'s DUE_SOON_REMINDER_DAYS.')
param dueSoonReminderDays int = 2

@description('Comma-separated hours of day (UTC, each 0-23) the worker checks for checkouts about to go overdue, e.g. "8" or "8,20". Matches .env.example\'s DUE_SOON_DIGEST_HOURS_UTC.')
param dueSoonDigestHoursUtc string = '8'

@description('Whether the individual "your item is overdue/due soon" reminder also goes to the checkout\'s own holder, in addition to the admin/manager digest. Matches .env.example\'s SEND_INDIVIDUAL_HOLDER_REMINDERS.')
param sendIndividualHolderReminders bool = true

@description('How many hours a pending ExtensionRequest can wait for a Manager/Admin decision before the SLA escalation task sends a reminder. Matches .env.example\'s EXTENSION_REQUEST_SLA_HOURS.')
param extensionRequestSlaHours string = '24'
@description('How many hours a submitted Quotation can wait for approval before the SLA escalation task sends a reminder. Matches .env.example\'s QUOTATION_SLA_HOURS.')
param quotationSlaHours string = '24'
@description('How often, in minutes, the embedded worker checks pending ExtensionRequest and Quotation queues for SLA breaches. Matches .env.example\'s APPROVAL_SLA_CHECK_INTERVAL_MINUTES.')
param approvalSlaCheckIntervalMinutes int = 60
@description('How many hours after an SLA escalation before the same still-pending item can be escalated again. Matches .env.example\'s APPROVAL_SLA_ESCALATION_REPEAT_HOURS.')
param approvalSlaEscalationRepeatHours string = '24'
@description('Whether quotation changes also send email notifications to the quotation recipient, in addition to the in-app notification. Matches .env.example\'s SEND_QUOTATION_RECIPIENT_EMAILS.')
param sendQuotationRecipientEmails bool = true


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
var backendImage = '${dockerHubBackendImage}:${empty(initialBackendImageTag) ? initialImageTag : initialBackendImageTag}'
var frontendImage = '${dockerHubFrontendImage}:${empty(initialFrontendImageTag) ? initialImageTag : initialFrontendImageTag}'
var errorBeaconImage = '${dockerHubErrorBeaconImage}:${initialImageTag}'

// One Log Analytics workspace for every container app's console/system
// logs. Application Insights is opt-in (see `otelAzureMonitorEnabled` above).
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30 // shortest retention Log Analytics allows -- cheapest option; console logs are also always live-streamable via `az containerapp logs show` regardless of this setting
  }
}

// Rides the same `logAnalytics` workspace's billing rather than a second
// standalone resource. Only deployed when `otelAzureMonitorEnabled` is true.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (otelAzureMonitorEnabled) {
  name: '${namePrefix}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    // backend is an API service, not a browser page, so it never loads the
    // JS snippet -- disabled explicitly rather than left at Azure's default.
    DisableIpMasking: false
  }
}

// Three Azure Monitor scheduled query alerts (backend error-rate spike,
// /readyz failing, daily backup missing -- see SRE_STRATEGY.md section 2).
// Billed per-rule (cents/month), no Application Insights required. Entirely
// opt-in: only deploys if `alertEmailAddress` is set.
//
// ORDERING: leave `alertEmailAddress` empty on a brand-new environment's
// first deploy. These rules query `ContainerAppConsoleLogs_CL`, which Azure
// only creates once a log line has actually been ingested -- deploying them
// against an empty workspace fails with "Failed to resolve table or column
// expression named 'ContainerAppConsoleLogs_CL'". Let the apps run for a few
// minutes first, then set the email and re-run to add alerting on top.
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

resource errorBeaconShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: 'errorbeacon-data'
  properties: { shareQuota: 5 }
}

// Live blue-green rollout status (status.json/checks.log/.htpasswd) --
// the ACA equivalent of the VM path's /mnt/docker-data/volumes/deploy_status
// directory. Written to by .github/workflows/deploy-azure-aca.yml (via `az
// storage blob upload`, using the run's own OIDC login -- see that
// workflow's own comments) and read LIVE, per-request, by `frontend`'s
// nginx below (proxy_pass, not a mount -- see nginx/default.conf.template's
// own /_deploy/ comment and blue-green.md's "Monitoring" section for the
// full mechanics).
//
// BUG FIX: this used to be an `AzureFile` share (`deployStatusShare`)
// mounted read-only into `frontend`, with `mountOptions: 'actimeo=1'` to
// keep ACA's default 30s CIFS attribute cache from hiding writes made by
// the GitHub Actions runner (a totally different client than the one doing
// the reading -- REST vs SMB gives no cross-protocol cache coherency
// guarantee). ACA rejects that mount option outright --
// ContainerAppVolumeMountOptionsNotSupported: "MountOptions 'actimeo' for
// volume 'deploy-status' are not supported by azure file share" -- and ACA
// doesn't support mounting Blob Storage as a volume AT ALL (see
// https://learn.microsoft.com/azure/container-apps/storage-mounts: "Azure
// Container Apps doesn't support mounting file shares from Azure NetApp
// Files or Azure Blob Storage"), so this moved off volume mounts entirely.
// nginx now proxy_pass'es straight through to this container on every
// request instead -- zero caching layer anywhere in the path, which
// actually solves the original staleness problem outright rather than
// just shrinking its window.
resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deployStatusContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: 'deploy-status'
  properties: {
    // Never publicly readable -- `storage` above already sets
    // `allowBlobPublicAccess: false` at the account level (which would
    // reject anything other than 'None' here anyway); the ONLY reads
    // allowed are via the scoped, expiring SAS token below, used
    // server-side by nginx's proxy_pass, never exposed to a browser.
    publicAccess: 'None'
  }
}

// `deployStatusSas` -- a read-only (`rl`), HTTPS-only, Blob-service-scoped
// account SAS token that `frontend`'s nginx appends server-side to every
// proxy_pass request against `deployStatusContainer` above (see
// nginx/default.conf.template's own /_deploy/ comment). Generated here with
// Bicep's built-in `listAccountSas` -- the same
// Microsoft.Storage/storageAccounts/listKeys/action permission this
// deployment's identity already needs for `storage.listKeys()` just below
// (backupStorage/exportStorage) covers this too, so no extra manual step,
// secret, or CI credential has to be minted outside this template.
//
// `deployTimestamp` exists ONLY so `dateTimeAdd` below has a base to add
// 10 years to -- `utcNow()` can only be used as a param's default value,
// never inline in a variable, so it has to be threaded through a
// parameter first. Don't set it manually; every deployment run picks up
// "now" automatically.
//
// Expiry is deliberately far out: this token is NEVER sent to or visible
// from a browser (only used on the nginx <-> Blob Storage hop), so unlike
// a browser-exposed token there's no security upside to a short rotation
// window here -- and re-running this deployment before it expires mints a
// fresh one with a new 10-year window anyway.
param deployTimestamp string = utcNow()

var deployStatusSasExpiry = dateTimeAdd(deployTimestamp, 'P10Y')

var deployStatusSasProperties = {
  signedServices: 'b'
  signedResourceTypes: 'co'
  signedPermission: 'rl'
  signedExpiry: deployStatusSasExpiry
  signedProtocol: 'https'
}

// `listAccountSas`'s `accountSasToken` comes back WITHOUT a leading '?' --
// prepending it here once means every consumer (just the `frontend`
// container's env below, today) can append it directly after a path with
// no extra string-building of its own.
var deployStatusSas = '?${storage.listAccountSas('2023-05-01', deployStatusSasProperties).accountSasToken}'

// Single subnet, single NSG: `frontend`/`backend`/`redis`/`migrate` share
// one Container Apps Environment (`postgresServer` is a standalone managed
// resource outside it). An NSG only filters at the subnet boundary, so it
// can't distinguish `frontend -> backend` from `frontend -> redis` traffic
// on a shared subnet. What it does cover: only the public internet ->
// `frontend` path needs to be open -- `redis`/`backend` already have
// `ingress.external: false` and are unreachable from outside the VNet
// regardless of this NSG.

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

resource errorBeaconStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'errorbeacon-data'
  properties: { azureFile: { accountName: storage.name, accountKey: storage.listKeys().keys[0].value, shareName: errorBeaconShare.name, accessMode: 'ReadWrite' } }
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

// Azure-managed PgBouncer. It runs with the Flexible Server and exposes the
// same hostname on port 6432; there is intentionally no ACA PgBouncer sidecar.
// The resource is only created when the application switch is enabled.
resource postgresPgBouncerEnabled 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2022-12-01' = if (usePgbouncer) {
  name: 'pgbouncer.enabled'
  parent: postgresServer
  properties: {
    source: 'user-override'
    value: 'true'
  }
}

// Keep psycopg2 startup behavior compatible with Azure PgBouncer's transaction
// pooling. PostgreSQL clients commonly send extra_float_digits in their startup
// packet; Azure supports explicitly allowing PgBouncer to ignore it.
// Make the managed PgBouncer budget explicit rather than pretending Azure has
// the self-hosted reserve-pool model used by docker-compose. The backend receives
// the same value through PGBOUNCER_SERVER_POOL_SIZE.
resource postgresPgBouncerDefaultPoolSize 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2022-12-01' = if (usePgbouncer) {
  name: 'pgbouncer.default_pool_size'
  parent: postgresServer
  dependsOn: [ postgresPgBouncerEnabled ]
  properties: {
    source: 'user-override'
    value: string(effectivePgbouncerServerPoolSize)
  }
}

resource postgresPgBouncerIgnoreStartup 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2022-12-01' = if (usePgbouncer) {
  name: 'pgbouncer.ignore_startup_parameters'
  parent: postgresServer
  dependsOn: [ postgresPgBouncerEnabled ]
  properties: {
    source: 'user-override'
    value: 'extra_float_digits'
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
// ErrorBeacon is an internal-ingress Container App. Do not assume Docker-style
// service DNS (`errorbeacon:8000`) works in ACA: the live ACA environment routes
// reliably through the Container App's internal ingress FQDN instead. Resolve the
// actual FQDN from the ErrorBeacon resource rather than reconstructing ACA's
// internal DNS naming convention. The FQDN addresses ACA ingress, so there is no
// `:8000` suffix here; ACA forwards ingress traffic to targetPort 8000.
// When monitoring is deliberately disabled, leave the URL empty rather than
// pointing the backend at a nonexistent ErrorBeacon app.
var errorBeaconUrl = errorBeaconEnabled
  ? 'http://${errorBeaconApp.properties.configuration.ingress.fqdn}'
  : ''
// Reverse direction of errorBeaconUrl above -- lets ErrorBeacon's Telegram
// /apphealth command poll backend's /healthz, /readyz and /health/dependencies.
// Sourced from the existingBackendFqdn parameter (resolved by infra-deploy.yml via
// `az containerapp show` before this deployment runs) rather than a symbolic
// `backendApp.properties...` reference: `backendApp` already depends on
// `errorBeaconApp` through errorBeaconUrl's symbolic reference inside `sharedEnv`,
// so a symbolic reference back from `errorBeaconApp` to `backendApp` here would be a
// circular resource dependency. It's also deliberately NOT reconstructed from
// env.properties.defaultDomain the way that might look tempting -- see
// errorBeaconUrl's own comment above on why this template resolves real FQDNs from
// their live resources instead of guessing ACA's internal DNS naming convention.
// Empty when the parameter is empty (a brand-new environment, before `backend`
// exists yet) -- BACKEND_URL is then left unset below, and ErrorBeacon's own
// /apphealth handling already reports "not configured" rather than erroring on
// that, the same way OPENROUTER_SITE_URL and others already tolerate a
// not-yet-provisioned dependency elsewhere in this template.
var backendInternalUrl = empty(existingBackendFqdn) ? '' : 'http://${existingBackendFqdn}'
var publicOrigin = empty(customDomain) ? 'https://${frontendFqdn}' : 'https://${customDomain}'
// Only a valid expression to evaluate when `appInsights` actually exists
// (otelAzureMonitorEnabled=true) -- the ternary's false branch never
// touches the conditionally-deployed resource, which is what makes this
// safe to reference even when it wasn't provisioned this deploy.
var appInsightsConnectionString = otelAzureMonitorEnabled ? appInsights.properties.ConnectionString : ''

// BUG FIX: this used to hardcode 'production' unconditionally, so a
// `environmentName: 'staging'` deploy still ran backend/worker/beat with
// ENVIRONMENT=production -- every production-only behavior in
// backend/config.py (ENABLE_API_DOCS/AUTO_INIT_DB/etc.'s defaults, the JWT
// secret strength check, secure-cookie/CORS strictness, etc.) silently
// applied to staging too.
// Now driven by the same environmentName param that already picks
// staging vs prod resource names/RG -- "prod" -> "production", "staging"
// -> "development" (config.py's own vocabulary; see its
// apply_environment_defaults()).
var runtimeEnvironment = environmentName == 'prod' ? 'production' : 'development'

var sharedEnv = [
  { name: 'ENVIRONMENT', value: runtimeEnvironment }
  { name: 'EXPORT_RESULT_DIR', value: '/app/export_results' }
  { name: 'ERRORBEACON_URL', value: errorBeaconUrl }
  // Keep the same observability identity and timeout used by both Compose paths.
  { name: 'ERRORBEACON_APP', value: errorBeaconAppName }
  { name: 'APP_RELEASE', value: empty(initialBackendImageTag) ? initialImageTag : initialBackendImageTag }
  { name: 'ERRORBEACON_TIMEOUT', value: '0.75' }
  // Keep the ErrorBeacon-related runtime keys accepted consistently across
  // Compose, VM Compose, and ACA. These are consumed by the standalone
  // ErrorBeacon image when applicable; harmless defaults here keep the shared
  // backend image contract identical across deployment paths.
  { name: 'ERRORBEACON_ENABLE_DOCS', value: 'false' }
  { name: 'ERRORBEACON_MAX_REQUEST_BODY_BYTES', value: '131072' }
  { name: 'ERRORBEACON_ADMIN_AUTH_FAILURES_PER_MINUTE', value: '10' }
  // The ACA ErrorBeacon service itself enables this separately below because
  // ACA ingress is its controlled proxy boundary. The backend must not trust
  // forwarded headers merely because it shares the same image contract.
  { name: 'ERRORBEACON_TRUST_PROXY_HEADERS', value: 'false' }
  // Embedded Celery worker/beat uses this to avoid broker connection retry loops.
  { name: 'CELERY_BROKER_CONNECTION_MAX_RETRIES', value: 'none' }
  // Reserve one deployment-wide PgBouncer connection for background Celery/Beat DB work.
  { name: 'DB_BACKGROUND_CONNECTION_RESERVE', value: '1' }
  { name: 'DB_BACKGROUND_CONCURRENCY_LIMIT', value: '1' }
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
  { name: 'EMAIL_PROVIDER', value: emailProvider }
  { name: 'ADMIN_NOTIFICATION_EMAILS', value: adminNotificationEmails }
  { name: 'OVERDUE_DIGEST_HOURS_UTC', value: overdueDigestHoursUtc }
  { name: 'DUE_SOON_REMINDER_DAYS', value: string(dueSoonReminderDays) }
  { name: 'DUE_SOON_DIGEST_HOURS_UTC', value: dueSoonDigestHoursUtc }
  { name: 'SEND_INDIVIDUAL_HOLDER_REMINDERS', value: string(sendIndividualHolderReminders) }
  { name: 'EXTENSION_REQUEST_SLA_HOURS', value: extensionRequestSlaHours }
  { name: 'QUOTATION_SLA_HOURS', value: quotationSlaHours }
  { name: 'APPROVAL_SLA_CHECK_INTERVAL_MINUTES', value: string(approvalSlaCheckIntervalMinutes) }
  { name: 'APPROVAL_SLA_ESCALATION_REPEAT_HOURS', value: approvalSlaEscalationRepeatHours }
  { name: 'SEND_QUOTATION_RECIPIENT_EMAILS', value: string(sendQuotationRecipientEmails) }
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
  { name: 'errorbeacon-ingest-api-key', value: empty(errorBeaconIngestApiKey) ? 'unset' : errorBeaconIngestApiKey }
  { name: 'errorbeacon-admin-api-key', value: empty(errorBeaconAdminApiKey) ? 'unset' : errorBeaconAdminApiKey }
  { name: 'root-admin-bootstrap-password', value: rootAdminBootstrapPassword }
  { name: 'database-url', value: databaseUrl }
  { name: 'redis-url', value: redisUrl }
  { name: 'smtp-password', value: empty(smtpPassword) ? 'unset' : smtpPassword }
  { name: 'brevo-api-key', value: empty(brevoApiKey) ? 'unset' : brevoApiKey }
  { name: 'resend-api-key', value: empty(resendApiKey) ? 'unset' : resendApiKey }
  { name: 'gdrive-oauth-client-secret', value: empty(gdriveOauthClientSecret) ? 'unset' : gdriveOauthClientSecret }
  { name: 'gdrive-oauth-refresh-token', value: empty(gdriveOauthRefreshToken) ? 'unset' : gdriveOauthRefreshToken }
  { name: 'otel-exporter-otlp-headers', value: empty(otelExporterOtlpHeaders) ? 'unset' : otelExporterOtlpHeaders }
  { name: 'applicationinsights-connection-string', value: empty(appInsightsConnectionString) ? 'unset' : appInsightsConnectionString }
], usePrivateDockerHubRepo ? [
  { name: 'dockerhub-token', value: dockerHubToken }
] : [])

var sharedSecretEnvRefs = [
  { name: 'JWT_SECRET_KEY', secretRef: 'jwt-secret-key' }
  { name: 'ERRORBEACON_INGEST_API_KEY', secretRef: 'errorbeacon-ingest-api-key' }
  { name: 'ERRORBEACON_ADMIN_API_KEY', secretRef: 'errorbeacon-admin-api-key' }
  // Only ever read by the `migrate` Job below, and only the very first
  // time it runs (see backend/alembic/versions/0002_bootstrap_root_admin.py)
  // -- harmless to also hand to backend/frontend/worker/beat, which simply
  // never read it, same as several other env vars in this shared list.
  { name: 'ROOT_ADMIN_BOOTSTRAP_PASSWORD', secretRef: 'root-admin-bootstrap-password' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'REDIS_URL', secretRef: 'redis-url' }
  { name: 'SMTP_PASSWORD', secretRef: 'smtp-password' }
  { name: 'BREVO_API_KEY', secretRef: 'brevo-api-key' }
  { name: 'RESEND_API_KEY', secretRef: 'resend-api-key' }
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
      // 'Multiple' (not 'Single') -- this is what makes zero-downtime
      // blue-green possible at all: the OLD and NEW revisions run side by
      // side as two independent, individually addressable "slots," and
      // traffic between them is a weight the deploy pipeline controls
      // explicitly, decoupled from "which one is newest." See
      // .github/scripts/aca-blue-green.sh's top-of-file comment for the
      // full rollout/finalize/rollback mechanics this enables, and
      // DEPLOYMENT.md's "Zero-downtime rollout mechanics" section.
      activeRevisionsMode: 'Multiple'
      registries: registries
      secrets: sharedSecrets
      ingress: {
        external: false
        targetPort: 8000
        transport: 'auto'
        // Required: nginx's `proxy_pass http://$backend_upstream...` (see
        // nginx/default.conf.template) speaks plain HTTP, and Container
        // Apps' Envoy proxy redirects plain HTTP to HTTPS unless this is
        // set. Only affects traffic inside the environment -- `backend`
        // still has `external: false`, so it's never internet-reachable.
        allowInsecure: true
        // Traffic is an explicit input, not an implicit "latest revision"
        // rule. On refresh, infra-deploy.yml resolves any existing
        // latestRevision routing to the concrete revision that is currently
        // carrying traffic before this resource is PUT. That keeps Bicep
        // from handing production traffic to a revision created by an
        // infrastructure change. The application deployment workflow remains
        // the sole owner of blue-green traffic changes.
        traffic: backendTraffic
      }
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(sharedEnv, sharedSecretEnvRefs, [
            // Launches the embedded Celery worker/beat (see start.sh and
            // celery_app.py's RedBeat config, which keeps this safe even
            // as `backend` scales to more than one replica).
            { name: 'RUN_EMBEDDED_WORKER', value: 'true' }
            { name: 'USE_PGBOUNCER', value: string(usePgbouncer) }
            { name: 'PGBOUNCER_SERVER_POOL_SIZE', value: string(effectivePgbouncerServerPoolSize) }
            { name: 'PGBOUNCER_SAFETY_MARGIN_PERCENT', value: '10' }
            // Azure Managed PgBouncer does not expose the self-hosted
            // reserve-pool abstraction. The explicit server-pool value above
            // is the single source of truth for DB pool/admission sizing.
            // The managed pool's authoritative server-side budget is
            // passed explicitly above. The self-hosted default/reserve
            // settings remain unused on ACA because Azure does not expose
            // the same reserve-pool abstraction.
            // No SERVE_FRONTEND -- `frontend` serves the static build,
            // `backend` is API-only. CORS_ORIGINS kept as defense in depth.
            { name: 'CORS_ORIGINS', value: publicOrigin }
            // Lets database.py's adaptive connection-pool sizing
            // (_compute_pool_sizing(), see that module) know the actual
            // worst-case replica fan-out it needs to divide the target
            // Postgres server's connection budget across, straight from
            // this same `backendMaxReplicas` param below -- so the two
            // can never silently drift apart, and pool sizing keeps
            // itself correct automatically if this param is ever
            // changed, with no matching code/config edit required.
            { name: 'BACKEND_MAX_REPLICAS', value: string(backendMaxReplicas) }
            { name: 'ERRORBEACON_INGEST_API_KEY', secretRef: 'errorbeacon-ingest-api-key' }
            { name: 'ERRORBEACON_ADMIN_API_KEY', secretRef: 'errorbeacon-admin-api-key' }
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
    postgresPgBouncerEnabled
    postgresPgBouncerIgnoreStartup
  ]
}

// ---------------------------------------------------------------------------
// `errorbeacon` -- isolated exception monitor. It has its own revision
// lifecycle and is deliberately NOT part of backend/frontend blue-green
// traffic. Keeping minReplicas=1 means a backend rollout, scale event, or
// revision replacement does not remove the monitoring endpoint.
// ---------------------------------------------------------------------------
resource errorBeaconApp 'Microsoft.App/containerApps@2024-03-01' = if (errorBeaconEnabled) {
  name: 'errorbeacon'
  location: location
  dependsOn: [errorBeaconStorage]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      secrets: concat(usePrivateDockerHubRepo ? [
        { name: 'dockerhub-token', value: dockerHubToken }
      ] : [], [
        { name: 'errorbeacon-ingest-api-key', value: empty(errorBeaconIngestApiKey) ? 'unset' : errorBeaconIngestApiKey }
        { name: 'errorbeacon-admin-api-key', value: empty(errorBeaconAdminApiKey) ? 'unset' : errorBeaconAdminApiKey }
        { name: 'telegram-bot-token', value: errorBeaconTelegramBotToken }
        { name: 'telegram-chat-id', value: errorBeaconTelegramChatId }
        { name: 'gemini-api-key', value: empty(errorBeaconGeminiApiKey) ? 'unset' : errorBeaconGeminiApiKey }
        { name: 'groq-api-key', value: empty(errorBeaconGroqApiKey) ? 'unset' : errorBeaconGroqApiKey }
        { name: 'openrouter-api-key', value: empty(errorBeaconOpenRouterApiKey) ? 'unset' : errorBeaconOpenRouterApiKey }
        { name: 'smtp-password', value: empty(smtpPassword) ? 'unset' : smtpPassword }
        { name: 'brevo-api-key', value: empty(brevoApiKey) ? 'unset' : brevoApiKey }
        { name: 'resend-api-key', value: empty(resendApiKey) ? 'unset' : resendApiKey }
      ])
      ingress: {
        external: false
        targetPort: 8000
        transport: 'auto'
        allowInsecure: true
      }
    }
    template: {
      containers: [
        {
          name: 'errorbeacon'
          image: errorBeaconImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          volumeMounts: [ { volumeName: 'errorbeacon-data', mountPath: '/data' } ]
          env: [
            // Without this, the container falls back to its code default
            // ('development'), so the /v1/test manual-alert endpoint (which
            // stamps its synthetic event with this service's OWN
            // ENVIRONMENT var, not the backend's) always reports
            // "development" here even on a prod ACA deploy -- real errors
            // reported by `backend` were unaffected since backend gets
            // ENVIRONMENT correctly via `sharedEnv` above; only this
            // service's copy was missing it.
            { name: 'ENVIRONMENT', value: runtimeEnvironment }
            { name: 'ERRORBEACON_INGEST_API_KEY', secretRef: 'errorbeacon-ingest-api-key' }
            { name: 'ERRORBEACON_ADMIN_API_KEY', secretRef: 'errorbeacon-admin-api-key' }
            // Lets the Telegram /apphealth command poll backend's own health
            // endpoints -- see backendInternalUrl's declaration above for why this
            // is a hand-built FQDN rather than a symbolic `backendApp` reference.
            { name: 'BACKEND_URL', value: backendInternalUrl }
            // ACA ingress is a controlled proxy boundary; allow ErrorBeacon to use the forwarded client IP.
            { name: 'ERRORBEACON_TRUST_PROXY_HEADERS', value: 'true' }
            { name: 'TELEGRAM_BOT_TOKEN', secretRef: 'telegram-bot-token' }
            { name: 'TELEGRAM_CHAT_ID', secretRef: 'telegram-chat-id' }
            { name: 'TELEGRAM_THREAD_ID', value: errorBeaconTelegramThreadId }
            { name: 'AI_ENABLED', value: 'true' }
            { name: 'GROQ_API_KEY', secretRef: 'groq-api-key' }
            { name: 'GROQ_MODEL', value: errorBeaconGroqModel }
            { name: 'GEMINI_API_KEY', secretRef: 'gemini-api-key' }
            { name: 'GEMINI_MODEL', value: errorBeaconGeminiModel }
            { name: 'GEMINI_FALLBACK_MODEL', value: errorBeaconGeminiFallbackModel }
            { name: 'OPENROUTER_API_KEY', secretRef: 'openrouter-api-key' }
            { name: 'OPENROUTER_MODEL', value: errorBeaconOpenRouterModel }
            // Reuses `publicOrigin` (customDomain if set, else the frontend's
            // default *.azurecontainerapps.io FQDN) instead of a separate
            // errorBeaconOpenRouterSiteUrl param -- see that param's removal
            // comment above.
            { name: 'OPENROUTER_SITE_URL', value: publicOrigin }
            { name: 'NOTIFICATIONS_ENABLED', value: string(notificationsEnabled) }
            { name: 'SMTP_HOST', value: smtpHost }
            { name: 'SMTP_PORT', value: string(smtpPort) }
            { name: 'SMTP_USERNAME', value: smtpUsername }
            { name: 'SMTP_USE_TLS', value: string(smtpUseTls) }
            { name: 'SMTP_USE_SSL', value: string(smtpUseSsl) }
            { name: 'SMTP_FROM_EMAIL', value: smtpFromEmail }
            { name: 'EMAIL_PROVIDER', value: emailProvider }
            { name: 'ADMIN_NOTIFICATION_EMAILS', value: adminNotificationEmails }
            { name: 'SMTP_PASSWORD', secretRef: 'smtp-password' }
            { name: 'BREVO_API_KEY', secretRef: 'brevo-api-key' }
            { name: 'RESEND_API_KEY', secretRef: 'resend-api-key' }
            { name: 'ERRORBEACON_EMAIL_FALLBACK_ENABLED', value: 'true' }
            { name: 'ERRORBEACON_EMAIL_FALLBACK_AFTER_ATTEMPTS', value: '3' }
            { name: 'ERRORBEACON_EMAIL_FALLBACK_AFTER_SECONDS', value: '300' }
            { name: 'ERRORBEACON_RETENTION_DAYS', value: '90' }
            { name: 'ERRORBEACON_DB_WARN_MB', value: '4096' }
            { name: 'ERRORBEACON_AUTO_STALE_DAYS', value: '30' }
            { name: 'ERRORBEACON_CLEAN_MAX_DEPTH', value: '10' }
            { name: 'ERRORBEACON_CLEAN_MAX_ITEMS', value: '100' }
            { name: 'ERRORBEACON_DIGEST_ENABLED', value: 'true' }
            { name: 'ERRORBEACON_DIGEST_INTERVAL_HOURS', value: '24' }
            { name: 'ERRORBEACON_STARTUP_SELF_TEST', value: 'false' }
            { name: 'DATA_DIR', value: '/data' }
            // '/data' here is the errorbeacon-data Azure Files (SMB) share (see
            // errorBeaconStorage above), not local disk. SQLite's default WAL journal
            // mode needs a shared-memory mmap that Azure Files doesn't support, so
            // PRAGMA journal_mode=WAL fails with "database is locked" on every startup
            // even with a single replica/worker. DELETE (plain rollback journal) needs
            // no shared memory and works correctly over the file share. Local deploy
            // paths (docker-compose.yml, render.yaml, infra-vm) use local disk and are
            // unaffected -- they keep the app's WAL default.
            { name: 'SQLITE_JOURNAL_MODE', value: 'DELETE' }
            { name: 'DEDUP_SECONDS', value: '60' }
            { name: 'SPIKE_THRESHOLD', value: '10' }
            { name: 'SPIKE_WINDOW_SECONDS', value: '300' }
            { name: 'ALERT_QUEUE_SIZE', value: '1000' }
            { name: 'ALERT_WORKERS', value: '3' }
            { name: 'TELEGRAM_POLLING', value: 'true' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30 }
          ]
        }
      ]
      volumes: [
        {
          name: 'errorbeacon-data'
          storageType: 'AzureFile'
          storageName: 'errorbeacon-data'
          // 'nobrl' = don't send POSIX byte-range lock requests to the SMB
          // server. Azure Files' SMB implementation doesn't reliably honor
          // the byte-range locks SQLite depends on for its own write
          // locking, so even with journal_mode=DELETE, the write-lock
          // SQLite takes internally before CREATE TABLE/INSERT/UPDATE fails
          // with "database is locked" -- this isn't a concurrency problem
          // (minReplicas/maxReplicas are both 1, one Uvicorn worker), it's
          // the SMB client silently failing to grant the lock at all. This
          // is Microsoft's own documented mountOptions recommendation for
          // apps like this that rely on POSIX locks:
          // https://learn.microsoft.com/troubleshoot/azure/azure-kubernetes/storage/mountoptions-settings-azure-files
          // A single-writer app like this doesn't need real cross-host byte
          // -range locking anyway -- init_db()'s own fcntl.flock() plus the
          // single-process/single-replica topology already serialize access.
          mountOptions: 'nobrl'
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
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
      // 'Multiple' -- same reasoning as `backendApp` above. `frontend`
      // being externally reachable additionally means every revision gets
      // its OWN public FQDN once this is set (Azure's standard
      // `<app>---<revision-suffix>.<default-domain>` pattern), which is
      // what lets aca-blue-green.sh smoke test the new revision directly
      // -- real HTTP, real proxy-to-backend chain -- before it receives
      // any share of production traffic. `backend`'s internal-only
      // ingress gets this too, but a GitHub-hosted runner can't reach an
      // internal FQDN, so only `frontend`'s rollout uses it (see that
      // script's `--public` argument).
      activeRevisionsMode: 'Multiple'
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
      secrets: concat(usePrivateDockerHubRepo ? [
        { name: 'dockerhub-token', value: dockerHubToken }
      ] : [], [
        { name: 'deploy-status-sas', value: deployStatusSas }
      ])
      ingress: {
        external: true
        targetPort: 80
        transport: 'auto'
        allowInsecure: false
        // BUG FIX: this used to be `empty(customDomain) ? [] : [{ name:
        // customDomain, bindingType: 'SniEnabled' }]` -- no `certificateId`
        // at all. `bindingType: 'SniEnabled'` REQUIRES a certificateId;
        // Azure only ever accepted that with no certificateId the very
        // first time (before this template had ever heard of the domain,
        // Azure silently treated the binding as pending/incomplete), and
        // rejects it on every deployment after a certificate exists with
        // "CertificateId property is missing for customDomain" --
        // `customDomainCertificateId` (see its own param comment) is what
        // was missing to make this idempotent. Until a certificate has
        // been provisioned and its ID supplied, `frontend` simply keeps
        // serving off its default *.azurecontainerapps.io FQDN -- exactly
        // the pre-custom-domain behavior -- rather than attempting a
        // binding that would fail.
        customDomains: (empty(customDomain) || empty(customDomainCertificateId)) ? [] : [
          { name: customDomain, bindingType: 'SniEnabled', certificateId: customDomainCertificateId }
        ]
        // Keep frontend routing explicit for the same reason as backend:
        // infra refreshes must preserve the currently traffic-bearing
        // revision instead of selecting the newly-created revision.
        traffic: frontendTraffic
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
            // The fully-qualified FQDN, not the bare app name -- nginx's
            // `resolver` directive does its own raw DNS queries and bypasses
            // glibc's search-domain expansion, so a bare "backend" comes
            // back NXDOMAIN even though curl/Python resolve it fine. Also
            // creates an explicit Bicep dependency on `backendApp`.
            { name: 'BACKEND_HOST', value: backendApp.properties.configuration.ingress.fqdn }
            // Calls between apps in the same environment go through the
            // environment's shared Envoy proxy on the standard web port, not
            // the container's own `targetPort: 8000`. Plain port 80 (not
            // 443) because nginx's proxy_pass speaks plain HTTP -- see
            // `backendApp`'s `allowInsecure: true` above.
            { name: 'BACKEND_PORT', value: '80' }
            { name: 'ENABLE_API_DOCS', value: string(enableApiDocs) } // must match backend's own value -- see nginx/default.conf.template's /docs passthrough gating
            // RESOLVER_IP deliberately NOT set -- nginx/docker-entrypoint.d/15-detect-resolver-ip.sh
            // reads it from Container Apps' own /etc/resolv.conf at boot.
            // Lets nginx proxy_pass /_deploy/status.json + /_deploy/checks.log
            // straight through to `deployStatusContainer` on Blob Storage,
            // live, per-request -- see that resource's own comment (above,
            // near `storage`) for why this replaced an Azure Files mount, and
            // nginx/default.conf.template's /_deploy/ comment for how these
            // two are actually used.
            { name: 'DEPLOY_STATUS_ACCOUNT', value: storage.name }
            { name: 'DEPLOY_STATUS_SAS', secretRef: 'deploy-status-sas' }
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
          env: concat(sharedEnv, sharedSecretEnvRefs, [ { name: 'USE_PGBOUNCER', value: 'false' } ])
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
output storageAccountName string = storage.name
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
