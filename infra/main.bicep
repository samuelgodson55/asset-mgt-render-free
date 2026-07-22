// =============================================================================
// infra/main.bicep
// -----------------------------------------------------------------------------
// COST-OPTIMIZED Azure Container Apps deployment for Snipe-IT Lite --
// FOUR Container Apps: `frontend`, `backend`, `db`, `redis`.
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
// WHY `redis` IS STILL HERE, NOT JUST `frontend`/`backend`/`db`
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
// first time it scales past 1. Keeping Redis as a small 4th container is
// what makes `backend`'s autoscaling actually SAFE, not just theoretically
// possible -- and it's cheap (same 0.25 vCPU/0.5 GiB as before, no
// persistent volume, same acceptable "resets on restart" trade as before).
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
//   - No managed identity, no Application Insights
//   - `db`/`redis` unchanged: official Docker Hub images, internal-only,
//     pinned to exactly 1 replica, `db` on a persistent Azure Files volume
// =============================================================================
// This replaces an earlier version of this file that used Azure Database for
// PostgreSQL Flexible Server + Azure Cache for Redis + Azure Container
// Registry + Key Vault + a User-Assigned Managed Identity + Application
// Insights + 4 Container Apps (backend/worker/beat/frontend). That shape is
// a solid *scaling* story but every one of those managed extras has its own
// monthly floor, and several of them (Flexible Server, Azure Cache, ACR
// Basic, Key Vault) never scale to zero -- they bill 24/7 whether or not
// anyone is using the app. For an early-stage startup that's the wrong
// trade.
//
// WHAT THIS PROVISIONS
// ---------------------------------------------------------------------------
//   - Log Analytics workspace                    (Container Apps console/system logs)
//   - Storage Account + 3 Azure Files shares      (Postgres data dir, backup_data, export_data --
//                                                   billed by GB actually used, not provisioned)
//   - VNet (3 delegated subnets + NSGs)            (frontend/backend/data isolation -- see SECURITY FIX comment below; no fixed floor)
//   - 3 Container Apps Environments                (Consumption plan, one per subnet/trust tier -- no fixed floor)
//   - 4 Container Apps:
//       `db`       -- postgres:16-alpine, official Docker Hub image, internal-only, 1 replica always
//       `redis`    -- redis:7-alpine, official Docker Hub image, internal-only, 1 replica always
//       `backend`  -- FastAPI + embedded Celery worker/beat (backend/Dockerfile),
//                     internal-only ingress, scales 0-N on its own
//       `frontend` -- static frontend + reverse proxy to `backend` (frontend/Dockerfile,
//                     UNMODIFIED from local Docker Compose), the ONLY public-facing app,
//                     scales 0-N independent of `backend`
//   - 1 Container Apps Job: `migrate`             (runs `alembic upgrade head` against `backend`'s image, only when triggered)
//
// WHAT WAS REMOVED FROM THE ORIGINAL MANAGED-SERVICES DESIGN, AND WHY IT'S
// SAFE HERE (unchanged from the combined-`app` version of this file)
// ---------------------------------------------------------------------------
//   - Azure Database for PostgreSQL Flexible Server -> `db` container app.
//     You lose: automatic point-in-time restore, engine-managed HA/failover.
//     You keep: the app's own pg_dump-based backup job (ENABLE_AUTO_BACKUP,
//     already in this codebase) now writing onto a persistent Azure Files
//     share instead of ephemeral disk, so backups survive a container
//     restart. Turn on BACKUP_GDRIVE_ENABLED for true off-box backups.
//   - Azure Cache for Redis -> `redis` container app, no persistent volume.
//     Still just the Celery broker/result backend + rate-limiter/lock store
//     (see this file's top comment) -- losing state on a restart is an
//     acceptable trade for the cost savings.
//   - Azure Container Registry -> Docker Hub (two images now: backend and
//     frontend -- see top comment on the free-plan private-repo limit).
//   - Key Vault -> plain Container Apps secrets.
//   - User-assigned managed identity -> removed (nothing left to authenticate
//     once ACR and Key Vault are both gone, assuming public Docker Hub repos).
//   - Application Insights -> removed (its own ingestion cost on top of Log
//     Analytics).
//
// REALISTIC MONTHLY COST -- see DEPLOYMENT.md's Cost section for the full
// breakdown table (this split's cost is close to the single-`app` version's
// ~US$10-20/mo floor, since `db`/`redis` -- the two components that can't
// scale to zero -- are unchanged; `frontend` adds one more scale-to-zero
// container, not another always-on one).
//
// USAGE
// ---------------------------------------------------------------------------
//   az deployment group create \
//     --resource-group rg-snipeit-lite-prod \
//     --template-file infra/main.bicep \
//     --parameters environmentName=prod \
//                  dockerHubBackendImage=yourdockerhubusername/snipeit-lite-backend \
//                  dockerHubFrontendImage=yourdockerhubusername/snipeit-lite-frontend \
//                  postgresPassword=$(openssl rand -hex 16) \
//                  redisPassword=$(openssl rand -hex 16) \
//                  jwtSecretKey=$(openssl rand -hex 32) \
//                  rootAdminBootstrapPassword=$(openssl rand -base64 24)
//                  # ^ optional -- omit (or leave "") to let the migrate Job
//                  # generate one instead and print it to stderr once (see
//                  # DEPLOYMENT.md's Monitoring section for how to read that
//                  # back out of Log Analytics if you go that route). Passing
//                  # it explicitly here, as above, means you already have it
//                  # in your own shell instead. Either way it's a no-op on
//                  # every deploy after the first -- the migrate Job only
//                  # ever bootstraps the root admin row once.
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

@description('Postgres password for the `db` container app.')
@secure()
param postgresPassword string

@description('Postgres username.')
param postgresUsername string = 'snipeit'

@description('Redis password for the `redis` container app (used with --requirepass).')
@secure()
param redisPassword string

@description('JWT signing secret. Generate with: openssl rand -hex 32')
@secure()
param jwtSecretKey string

@description('OPTIONAL. One-time root admin bootstrap password, read directly by the migrate Job the first time it runs (see backend/alembic/versions/0002_bootstrap_root_admin.py). Never read by the running backend/frontend apps. Leave empty to have that migration generate and print a random password to the Job''s logs exactly once instead.')
@secure()
param rootAdminBootstrapPassword string = ''

@description('Minimum `backend` replicas. 0 = scale-to-zero (cold start after idle, cheapest). 1 = always warm, small extra cost, no cold start. Independent of `frontend` -- that is the whole point of the split.')
param backendMinReplicas int = 0

@description('Maximum `backend` replicas under load. NOTE: `backend` embeds Celery worker+beat in-process (see RUN_EMBEDDED_WORKER below) since there is no separate worker/beat Container App in this cost-optimized layout. That is safe at any replica count: celery_app.py configures RedBeat as the Beat scheduler, which keeps a distributed lock in Redis so only one replica is ever the active scheduler at a time (automatic failover if that replica dies) -- no per-replica configuration needed here.')
param backendMaxReplicas int = 3

@description('Minimum `frontend` replicas. 0 = scale-to-zero. Usually safe to leave at 0 even in production -- static-file + proxy responses are fast, so a cold start here is much shorter than `backend`''s.')
param frontendMinReplicas int = 0

@description('Maximum `frontend` replicas under load.')
param frontendMaxReplicas int = 3

@description('Postgres data volume size in GB (billed by GB actually used, this is just the ceiling).')
param postgresVolumeQuotaGb int = 20

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

@description('Gate for FastAPI''s interactive API docs (Swagger/ReDoc) AND nginx''s matching passthrough route -- see nginx/default.conf.template. Keep false in any environment reachable from the public internet unless you specifically need it.')
param enableApiDocs bool = false

@description('Email address to page on the three Azure Monitor scheduled query alerts below (backend error-rate spike, /readyz failing, daily backup missing) -- see SRE_STRATEGY.md section 2. Leave empty (the default) to skip creating the action group/alert rules entirely -- no alerting, no extra cost, same as before this parameter existed.')
param alertEmailAddress string = ''

var namePrefix = '${appBaseName}-${environmentName}'
var suffix = uniqueString(resourceGroup().id, environmentName)
var storageAccountName = take(replace('${appBaseName}${environmentName}st${take(suffix, 6)}', '-', ''), 24)

var usePrivateDockerHubRepo = !empty(dockerHubUsername)
var backendImage = '${dockerHubBackendImage}:${initialImageTag}'
var frontendImage = '${dockerHubFrontendImage}:${initialImageTag}'

// ---------------------------------------------------------------------------
// Monitoring -- one Log Analytics workspace for every container app's
// console/system logs. No Application Insights (see top-of-file comment).
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30 // shortest retention Log Analytics allows -- cheapest option; console logs are also always live-streamable via `az containerapp logs show` regardless of this setting
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
// Storage Account + Azure Files -- ONE share for Postgres's data directory
// (this is what makes `db` safe to restart/redeploy without losing data),
// plus the app's existing backup_data/export_data shares. Standard_LRS,
// classic pay-as-you-go share billing: you pay for GB actually stored, the
// `shareQuota` below is just a ceiling, not a reservation.
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

resource postgresShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: 'postgres-data'
  properties: { shareQuota: postgresVolumeQuotaGb }
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
// SECURITY FIX -- Strict VNet isolation (was: no VNet at all)
// ---------------------------------------------------------------------------
// Before this change, all four container apps (`frontend`, `backend`, `db`,
// `redis`) shared ONE Container Apps Environment on Azure's auto-generated,
// unmanaged network. Internal-only ingress on `db`/`backend` stops the
// PUBLIC internet from reaching them directly, but it does nothing to stop
// LATERAL movement: any app in that same environment can already resolve
// and reach any other app's internal DNS name on any port -- there was no
// NSG, because there was no customer-owned subnet to attach one to. A
// compromised `frontend` (e.g. via a dependency RCE) could port-scan and
// query `db:5432` / `redis:6379` directly, completely bypassing `backend`.
//
// A single shared environment can't fix this on its own: an NSG applies at
// the SUBNET boundary, and every app inside one Container Apps Environment
// shares that environment's one subnet -- Azure does not let you attach
// per-app network rules within an environment (see
// https://learn.microsoft.com/azure/container-apps/firewall-integration).
// The only way to get real network segmentation between these apps is to
// give each trust tier its OWN environment (own dedicated subnet), and
// filter the traffic *between* those subnets with NSGs. That's what this
// section does -- three environments instead of one, still Consumption
// plan (no extra always-on cost; environments themselves are free, you
// only pay for app usage), all inside one VNet so internal DNS still
// resolves environment-to-environment:
//
//   frontend-subnet (10.0.0.0/23)  -> `frontendEnv`  (external ingress, public)
//   backend-subnet  (10.0.2.0/23)  -> `backendEnv`   (internal, + `migrate` job)
//   data-subnet     (10.0.4.0/23)  -> `dataEnv`      (internal: `db`, `redis`)
//
// NSG allow-list (default-deny for everything else moving BETWEEN subnets):
//   Internet         -> frontend-subnet : 443/80          (public ingress)
//   frontend-subnet  -> backend-subnet  : 8000            (frontend's nginx -> backend API)
//   backend-subnet   -> data-subnet     : 5432, 6379      (backend -> db, redis)
//   (everything else inbound from VirtualNetwork is explicitly denied)
//
// Net effect: a compromised `frontend` container can reach `backend:8000`
// and nothing else -- it can no longer see `db`/`redis` at all, on any
// port, because they're on a different subnet with an NSG that doesn't
// allow frontend-subnet traffic in. A compromised `backend` container is
// similarly capped at 5432/6379 into data-subnet; it cannot be used to
// pivot into frontend-subnet (nothing allows backend -> frontend inbound).
// Required Azure platform/management traffic (health probes, image pulls,
// Log Analytics, Azure Files mounts, DNS, etc.) is left open via the
// `AzureCloud`/`AzureLoadBalancer`/`Storage` service tags per Microsoft's
// documented NSG requirements for VNet-injected Consumption environments --
// double-check that list against the current docs before tightening
// further, since Azure has occasionally added new required ports there.
// ---------------------------------------------------------------------------

var frontendSubnetPrefix = '10.0.0.0/23'
var backendSubnetPrefix = '10.0.2.0/23'
var dataSubnetPrefix = '10.0.4.0/23'

resource nsgFrontend 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${namePrefix}-nsg-frontend'
  location: location
  properties: {
    securityRules: [
      {
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
      {
        // Explicit default-deny for lateral movement FROM other subnets in
        // this VNet into frontend-subnet -- nothing (backend, data, or a
        // future subnet) should ever need to reach `frontend` directly.
        name: 'Deny-VirtualNetwork-Inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource nsgBackend 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${namePrefix}-nsg-backend'
  location: location
  properties: {
    securityRules: [
      {
        // Only `frontend`'s nginx reverse proxy may call `backend`'s API port.
        name: 'Allow-Frontend-To-Backend-Inbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: frontendSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '8000'
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
      {
        // Blocks port-scanning/lateral movement from a compromised
        // `frontend` (or anything else in the VNet) against backend-subnet
        // on anything other than the one allowed port above.
        name: 'Deny-VirtualNetwork-Inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource nsgData 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${namePrefix}-nsg-data'
  location: location
  properties: {
    securityRules: [
      {
        // Only `backend` (embedded Celery worker/beat included) may reach
        // `db`/`redis` -- never `frontend`, closing the exact gap described
        // in the threat model (compromised container port-scanning peers).
        name: 'Allow-Backend-To-Data-Inbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: backendSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRanges: ['5432', '6379']
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
      {
        // Default-deny: `frontend` (or anything else) cannot reach `db`/
        // `redis` at all, on any port -- this is the core of the fix.
        name: 'Deny-VirtualNetwork-Inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
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
        name: 'frontend-subnet'
        properties: {
          addressPrefix: frontendSubnetPrefix
          networkSecurityGroup: { id: nsgFrontend.id }
          delegations: [
            { name: 'Microsoft.App.environments', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
        }
      }
      {
        name: 'backend-subnet'
        properties: {
          addressPrefix: backendSubnetPrefix
          networkSecurityGroup: { id: nsgBackend.id }
          delegations: [
            { name: 'Microsoft.App.environments', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
        }
      }
      {
        name: 'data-subnet'
        properties: {
          addressPrefix: dataSubnetPrefix
          networkSecurityGroup: { id: nsgData.id }
          delegations: [
            { name: 'Microsoft.App.environments', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Three Container Apps Environments (one per trust tier -- see the VNet
// comment above for why one shared environment can't be NSG-segmented).
// Each is still Consumption plan: no fixed monthly floor, same billing
// model as before.
// ---------------------------------------------------------------------------
resource frontendEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env-frontend'
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
      internal: false // must stay externally reachable -- this is the one public entry point
    }
  }
}

resource backendEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env-backend'
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
      infrastructureSubnetId: vnet.properties.subnets[1].id
      internal: true // no public ingress needed -- only frontend-subnet may reach it (see nsgBackend)
    }
  }
}

resource dataEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env-data'
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
      infrastructureSubnetId: vnet.properties.subnets[2].id
      internal: true // no public ingress -- only backend-subnet may reach it (see nsgData)
    }
  }
}

resource postgresStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: dataEnv
  name: 'postgres-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: postgresShare.name
      accessMode: 'ReadWrite'
    }
  }
}

resource backupStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: backendEnv
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
  parent: backendEnv
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
// `db` -- Postgres 16, official image straight from Docker Hub (no registry
// of your own needed for this one). Internal-only TCP ingress, in its own
// `dataEnv`/data-subnet -- reachable ONLY from `backend`/`migrate` (both in
// backend-subnet) via `db.${dataEnv...defaultDomain}:5432`, per nsgData's
// allow-list; never from the public internet, and no longer reachable from
// `frontend` either (see the VNet isolation comment above). Pinned to
// EXACTLY 1 replica always: a
// stateful single-writer database must never be scaled out, and Consumption
// plan TCP-ingress apps don't support HTTP-style autoscale rules anyway.
// ---------------------------------------------------------------------------
resource dbApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'db'
  location: location
  properties: {
    managedEnvironmentId: dataEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: [
        { name: 'postgres-password', value: postgresPassword }
      ]
      ingress: {
        external: false
        transport: 'tcp'
        targetPort: 5432
        exposedPort: 5432
      }
    }
    template: {
      containers: [
        {
          name: 'db'
          image: 'postgres:16-alpine'
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'POSTGRES_USER', value: postgresUsername }
            { name: 'POSTGRES_DB', value: 'asset_db' }
            { name: 'POSTGRES_PASSWORD', secretRef: 'postgres-password' }
            // Postgres refuses to initdb directly into a non-empty mount
            // point that also contains the volume's own metadata -- point
            // PGDATA at a subdirectory of the mounted share instead.
            { name: 'PGDATA', value: '/var/lib/postgresql/data/pgdata' }
          ]
          volumeMounts: [
            { volumeName: 'postgres-data', mountPath: '/var/lib/postgresql/data' }
          ]
          probes: [
            {
              type: 'Liveness'
              tcpSocket: { port: 5432 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      volumes: [
        { name: 'postgres-data', storageType: 'AzureFile', storageName: 'postgres-data' }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1 // NEVER raise this -- single-writer stateful database
      }
    }
  }
}

// ---------------------------------------------------------------------------
// `redis` -- official image from Docker Hub. Internal-only TCP ingress, in
// `dataEnv`/data-subnet alongside `db` -- reachable ONLY from `backend` as
// `redis.${dataEnv...defaultDomain}:6379`, per nsgData's allow-list; never
// from `frontend` or the public internet. No persistent volume (see top-of-file comment
// on why that's an acceptable trade for this app's Redis usage) -- an
// in-memory cache/broker that resets on restart, exactly like Render's free
// Key Value tier.
// ---------------------------------------------------------------------------
resource redisApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'redis'
  location: location
  properties: {
    managedEnvironmentId: dataEnv.id
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
// `db`/`redis` now live in `dataEnv`, a DIFFERENT environment than
// `backend` (`backendEnv`) -- the short in-environment name ("db") only
// resolves for apps in the SAME environment, so cross-environment calls
// need the target app's full internal FQDN instead. Both environments'
// private DNS zones are linked to the same VNet, so this still resolves
// fine from `backendEnv`.
var databaseUrl = 'postgresql://${postgresUsername}:${postgresPassword}@db.${dataEnv.properties.defaultDomain}:5432/asset_db'
var redisUrl = 'redis://:${redisPassword}@redis.${dataEnv.properties.defaultDomain}:6379/0'
var frontendFqdn = 'frontend.${frontendEnv.properties.defaultDomain}'
var publicOrigin = empty(customDomain) ? 'https://${frontendFqdn}' : 'https://${customDomain}'

var sharedEnv = [
  { name: 'ENVIRONMENT', value: 'production' }
  { name: 'EXPORT_RESULT_DIR', value: '/app/export_results' }
  { name: 'JWT_ALGORITHM', value: 'HS256' }
  { name: 'JWT_EXPIRY_HOURS', value: '12' }
  { name: 'SITE_NAME', value: 'Snipe-IT Lite' }
  { name: 'AUTO_INIT_DB', value: 'false' }
  { name: 'AUTO_SEED_DEMO_DATA', value: 'false' }
  { name: 'LOG_LEVEL', value: 'INFO' }
  { name: 'LOG_FORMAT', value: 'json' }
  { name: 'LOGIN_RATE_LIMIT_MAX', value: '5' }
  { name: 'LOGIN_RATE_LIMIT_WINDOW_SECONDS', value: '60' }
  { name: 'ACCOUNT_LOCKOUT_MAX_ATTEMPTS', value: '5' }
  { name: 'ACCOUNT_LOCKOUT_DURATION_MINUTES', value: '15' }
  { name: 'ENABLE_API_DOCS', value: string(enableApiDocs) }
  { name: 'SUPER_ADMIN_USERNAME', value: 'superadmin' }
  { name: 'SUPER_ADMIN_NAME', value: 'Super Admin' }
  { name: 'NOTIFICATIONS_ENABLED', value: string(notificationsEnabled) }
  { name: 'SMTP_HOST', value: smtpHost }
  { name: 'SMTP_PORT', value: '587' }
  { name: 'SMTP_USERNAME', value: smtpUsername }
  { name: 'SMTP_USE_TLS', value: 'true' }
  { name: 'SMTP_USE_SSL', value: 'false' }
  { name: 'SMTP_FROM_EMAIL', value: smtpFromEmail }
  { name: 'ADMIN_NOTIFICATION_EMAILS', value: adminNotificationEmails }
  { name: 'OVERDUE_NOTIFICATION_INTERVAL_HOURS', value: '24' }
  { name: 'DUE_SOON_REMINDER_DAYS', value: '2' }
  { name: 'DUE_SOON_NOTIFICATION_INTERVAL_HOURS', value: '24' }
  { name: 'SEND_INDIVIDUAL_HOLDER_REMINDERS', value: 'true' }
  { name: 'DISPLAY_TIMEZONE', value: 'Africa/Lagos' }
  { name: 'CURRENCY_CODE', value: 'NGN' }
  { name: 'CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER', value: 'false' }
  { name: 'ENABLE_AUTO_BACKUP', value: 'true' }
  { name: 'BACKUP_HOURS_UTC', value: '3' }
  { name: 'BACKUP_DIR', value: '/app/backups' }
  { name: 'BACKUP_RETENTION_COUNT', value: '7' }
  { name: 'BACKUP_GDRIVE_ENABLED', value: 'false' }
]

var sharedSecrets = concat([
  { name: 'jwt-secret-key', value: jwtSecretKey }
  { name: 'root-admin-bootstrap-password', value: rootAdminBootstrapPassword }
  { name: 'database-url', value: databaseUrl }
  { name: 'redis-url', value: redisUrl }
  { name: 'smtp-password', value: empty(smtpPassword) ? 'unset' : smtpPassword }
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
]

var registries = usePrivateDockerHubRepo ? [
  { server: 'index.docker.io', username: dockerHubUsername, passwordSecretRef: 'dockerhub-token' }
] : []

// ---------------------------------------------------------------------------
// `backend` -- FastAPI + embedded Celery worker/beat (backend/Dockerfile,
// RUN_EMBEDDED_WORKER=true). INTERNAL-ONLY ingress, in its own `backendEnv`/
// backend-subnet -- nsgBackend only allows inbound from frontend-subnet on
// port 8000, so only `frontend` can ever reach it (the public internet
// never talks to it directly, and `migrate` runs from this same subnet).
// It in turn is the only thing nsgData allows into data-subnet, so it's
// the sole path to `db`/`redis` too. Scales 0-N independent of `frontend` --
// see top-of-file comment for why Redis is what makes this safe past 1 replica.
// ---------------------------------------------------------------------------
resource backendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'backend'
  location: location
  properties: {
    managedEnvironmentId: backendEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      secrets: sharedSecrets
      ingress: {
        external: false
        targetPort: 8000
        transport: 'auto'
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
  dependsOn: [
    dbApp
    redisApp
  ]
}

// ---------------------------------------------------------------------------
// `frontend` -- frontend/Dockerfile, UNMODIFIED from local Docker Compose:
// serves the static frontend build AND reverse-proxies /api/* to `backend`
// over the VNet's shared internal DNS, now cross-environment
// (nginx/default.conf.template's BACKEND_HOST/BACKEND_PORT env vars --
// resolver auto-detected at boot, see
// nginx/docker-entrypoint.d/15-detect-resolver-ip.sh). In its own
// `frontendEnv`/frontend-subnet -- the ONLY externally-reachable app, and
// nsgFrontend denies any inbound from the rest of the VNet, so nothing
// (including a compromised `backend`) can reach frontend-subnet either.
// Scales 0-N independent of `backend`.
// ---------------------------------------------------------------------------
resource frontendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'frontend'
  location: location
  properties: {
    managedEnvironmentId: frontendEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
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
            // `backend` now lives in a DIFFERENT environment (`backendEnv`)
            // than `frontend` (`frontendEnv`) -- the short name "backend"
            // only resolves within the same environment, so this needs
            // backend's full internal FQDN instead. Both environments'
            // private DNS zones are linked to the same VNet, so this
            // resolves fine from `frontendEnv`. The NSG on backend-subnet
            // still only allows traffic FROM frontend-subnet on 8000.
            { name: 'BACKEND_HOST', value: 'backend.${backendEnv.properties.defaultDomain}' }
            { name: 'BACKEND_PORT', value: '8000' }
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
    // Must run from backend-subnet, not frontend-subnet or a standalone
    // environment -- nsgData only allows inbound to data-subnet from
    // backendSubnetPrefix (see the VNet isolation comment above).
    environmentId: backendEnv.id
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
    dbApp
  ]
}

// ---------------------------------------------------------------------------
// Outputs -- consumed by the GitHub Actions deploy workflows.
// ---------------------------------------------------------------------------
output frontendEnvName string = frontendEnv.name
output backendEnvName string = backendEnv.name
output dataEnvName string = dataEnv.name
output frontendFqdn string = frontendApp.properties.configuration.ingress.fqdn
output frontendAppName string = frontendApp.name
output backendAppName string = backendApp.name
output dbAppName string = dbApp.name
output redisAppName string = redisApp.name
output migrateJobName string = migrateJob.name
output logAnalyticsWorkspaceId string = logAnalytics.id
