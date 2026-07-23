// =============================================================================
// infra/main.bicep
// -----------------------------------------------------------------------------
// COST-OPTIMIZED Azure Container Apps deployment for Snipe-IT Lite --
// FOUR Container Apps: `frontend`, `backend`, `db`, `redis`, all inside ONE
// Container Apps Managed Environment (see "SINGLE MANAGED ENVIRONMENT" below
// for why this file no longer uses three).
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
// SINGLE MANAGED ENVIRONMENT (was: three, one per trust tier)
// ---------------------------------------------------------------------------
// This file previously gave `frontend`, `backend`, and `db`/`redis` each
// their OWN Container Apps Managed Environment (own delegated subnet, own
// NSG) purely for network segmentation -- see the git history for that
// version's "SECURITY FIX" comment. In practice that hit a hard wall:
// `Microsoft.App/managedEnvironments` is capped by a per-region,
// per-subscription quota (`MaxNumberOfRegionalEnvironmentsInSubExceeded`),
// and on most subscription tiers that quota is far too low to spend three
// of it on one app. Three environments is a deploy-time failure on those
// subscriptions, not just a theoretical concern -- this is the fix for
// exactly that failure.
//
// Consolidated back to ONE environment (`env` below), one subnet, one NSG.
// All four Container Apps + the `migrate` Job now share it. The trade-off:
// an NSG applies at the SUBNET boundary, and every app inside one Container
// Apps Environment shares that environment's one subnet -- Azure does not
// let you attach per-app network rules within an environment (see
// https://learn.microsoft.com/azure/container-apps/firewall-integration),
// so the lateral-movement protection the three-subnet design bought (a
// compromised `frontend` literally cannot resolve/reach `db`/`redis` on the
// wire, regardless of application logic) is gone. What's still in place,
// unchanged, at the APPLICATION layer:
//   - `db`/`redis`/`backend` all still set `ingress.external: false` --
//     none of them ever get a public FQDN, only `frontend` does.
//   - `backend`'s API still only trusts requests proxied through
//     `frontend` in practice (same-origin cookie auth, see the comment
//     above), and `db`/`redis` still require their own passwords.
// What's gone is defense-in-depth against a compromised container
// port-scanning its neighbors directly -- if that risk matters more to you
// than the quota/cost trade, either request an environment-quota increase
// from Azure support and restore the three-environment version from git
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
//   - VNet (1 delegated subnet + NSG)              (see SINGLE MANAGED ENVIRONMENT comment above; no fixed floor)
//   - 1 Container Apps Environment                 (Consumption plan, shared by every app below -- no fixed floor; see SINGLE MANAGED ENVIRONMENT comment above for why this is 1, not 3)
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

@description('Gate for FastAPI\'s interactive API docs (Swagger/ReDoc) AND nginx\'s matching passthrough route -- see nginx/default.conf.template. Keep false in any environment reachable from the public internet unless you specifically need it.')
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
// SINGLE SUBNET, SINGLE NSG (was: three subnets/NSGs, one per trust tier)
// ---------------------------------------------------------------------------
// See the "SINGLE MANAGED ENVIRONMENT" comment at the top of this file for
// the full story: three Managed Environments blew through this
// subscription's per-region environment quota
// (`MaxNumberOfRegionalEnvironmentsInSubExceeded`), so all four Container
// Apps + the `migrate` Job now share ONE environment, and therefore one
// delegated subnet. An NSG can only filter traffic AT a subnet boundary, so
// with everything on one subnet there is no NSG rule that can allow
// `frontend -> backend:8000` while denying `frontend -> db:5432` -- that
// distinction no longer exists at the network layer. This one NSG instead
// covers what's still true regardless of subnet layout: only the public
// internet -> `frontend` path needs to be open at all, everything else
// (`db`/`redis`/`backend`'s own `ingress.external: false`) already never
// gets a public IP, so it's unreachable from outside the VNet no matter
// what this NSG says.
// ---------------------------------------------------------------------------

var subnetPrefix = '10.0.0.0/23'

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${namePrefix}-nsg'
  location: location
  properties: {
    securityRules: [
      {
        // The only public entry point into the whole environment --
        // `frontend`'s external ingress. `backend`/`db`/`redis` all set
        // `ingress.external: false`, so this rule being broad (whole
        // subnet, not just frontend's IP) doesn't expose them: they simply
        // never get a public FQDN/IP for the internet to reach in the
        // first place, regardless of what this NSG allows.
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
// ONE Container Apps Environment shared by `frontend`, `backend`, `db`,
// `redis`, and the `migrate` Job -- see the "SINGLE MANAGED ENVIRONMENT"
// comment at the top of this file. Still Consumption plan: no fixed
// monthly floor, same billing model as before. `internal: false` because
// `frontend` needs a public FQDN; `db`/`redis`/`backend` opt out of public
// ingress individually via their own `ingress.external: false`, same as
// when they had a dedicated internal environment each.
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

resource postgresStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
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
// `db` -- Postgres 16, official image straight from Docker Hub (no registry
// of your own needed for this one). Internal-only TCP ingress (`ingress.
// external: false`), in the shared `env` -- never gets a public FQDN, so
// it's unreachable from the internet regardless of the NSG (see "SINGLE
// MANAGED ENVIRONMENT"/"SINGLE SUBNET, SINGLE NSG" comments above for why
// there's no longer a network-layer wall between `db` and `frontend`
// specifically). Pinned to EXACTLY 1 replica always: a stateful
// single-writer database must never be scaled out, and Consumption plan
// TCP-ingress apps don't support HTTP-style autoscale rules anyway.
// ---------------------------------------------------------------------------
resource dbApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'db'
  location: location
  properties: {
    managedEnvironmentId: env.id
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
  // `volumes[].storageName` above is a plain string, not a symbolic
  // reference to `postgresStorage` -- Bicep only infers dependencies from
  // symbolic references, so without this explicit dependsOn, ARM has no
  // reason to wait for the `postgres-data` Managed Environment storage
  // resource to finish provisioning before creating `db`, and can (and did)
  // race them: "ManagedEnvironment Storage 'postgres-data' was not found."
  dependsOn: [
    postgresStorage
  ]
}

// ---------------------------------------------------------------------------
// `redis` -- official image from Docker Hub. Internal-only TCP ingress
// (`ingress.external: false`), in the shared `env` alongside `db` -- never
// gets a public FQDN, so it's unreachable from the internet regardless of
// the NSG. No persistent volume (see top-of-file comment on why that's an
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
// `db`/`redis`/`backend`/`frontend` all now live in the same shared `env`
// (see "SINGLE MANAGED ENVIRONMENT" above), so the short in-environment DNS
// name (just the app name, e.g. "db") resolves fine for all of them -- no
// need for the longer cross-environment FQDN form this used when `db`/
// `redis` lived in a separate `dataEnv`.
var databaseUrl = 'postgresql://${postgresUsername}:${postgresPassword}@db:5432/asset_db'
var redisUrl = 'redis://:${redisPassword}@redis:6379/0'
var frontendFqdn = 'frontend.${env.properties.defaultDomain}'
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
  // `dbApp`/`redisApp` because `backend` needs them reachable at boot; the
  // volumes above have the same missing-implicit-dependency issue as
  // `dbApp`'s `postgres-data` volume (see that resource's comment) --
  // `backupStorage`/`exportStorage` are referenced by plain string, so
  // Bicep won't otherwise wait for them before creating `backend`.
  dependsOn: [
    dbApp
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
            // `frontend` and `backend` now share the same environment (see
            // "SINGLE MANAGED ENVIRONMENT" above), so the short
            // in-environment DNS name resolves directly -- no FQDN needed.
            { name: 'BACKEND_HOST', value: 'backend' }
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
    // Runs from the same shared `env` as `db`/`backend` so the short
    // in-environment DNS name (used inside `databaseUrl` above) resolves.
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
    dbApp
  ]
}

// ---------------------------------------------------------------------------
// Outputs -- consumed by the GitHub Actions deploy workflows.
// ---------------------------------------------------------------------------
output envName string = env.name
output frontendFqdn string = frontendApp.properties.configuration.ingress.fqdn
output frontendAppName string = frontendApp.name
output backendAppName string = backendApp.name
output dbAppName string = dbApp.name
output redisAppName string = redisApp.name
output migrateJobName string = migrateJob.name
output logAnalyticsWorkspaceId string = logAnalytics.id
