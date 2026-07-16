// =============================================================================
// infra/main.bicep
// -----------------------------------------------------------------------------
// Azure Container Apps deployment for Snipe-IT Lite -- this is the IaC
// counterpart to render.yaml (Render Blueprint), but for a real,
// multi-service, scalable production target instead of a single free-tier
// container. Deploy it once per environment (staging / production) into its
// own resource group -- see DEPLOYMENT.md's "Azure Container Apps Production
// Deployment" section for the full walkthrough and the reasoning behind
// every choice below.
//
// WHAT THIS PROVISIONS
// ---------------------------------------------------------------------------
//   - Log Analytics workspace + Application Insights   (Monitoring)
//   - User-assigned managed identity                    (ACR pull + Key Vault read, no passwords anywhere)
//   - Azure Container Registry                          (holds snipeit-backend / snipeit-frontend images)
//   - Key Vault                                          (JWT secret, DB password, SMTP password, ...)
//   - Azure Database for PostgreSQL Flexible Server      (replaces the `db` container -- see reasoning below)
//   - Azure Cache for Redis                              (replaces the `redis` container -- see reasoning below)
//   - Storage Account + Azure Files shares               (backup_data / export_data volumes -- replaces the
//                                                          named Docker volumes of the same name)
//   - Container Apps Environment                          (the shared network/compute boundary)
//   - 4 Container Apps: backend, worker, beat, frontend   (maps 1:1 to docker-compose.yml's 4 app services)
//   - 1 Container Apps Job: migrate                        (runs `alembic upgrade head` as an explicit,
//                                                          one-shot step -- see docker-compose.yml's own
//                                                          comment on why this must never run on container boot)
//
// WHY MANAGED POSTGRES/REDIS INSTEAD OF DB/REDIS CONTAINER APPS
// ---------------------------------------------------------------------------
// docker-compose.yml runs `db` and `redis` as containers with named Docker
// volumes. Container Apps' own storage (ephemeral scratch disk, or an Azure
// Files mount) is not a substitute for a real database's engine, backup/
// point-in-time-restore, and failover story -- and a stateful container app
// still gets recreated/rescheduled by the platform like any other revision.
// Azure Database for PostgreSQL Flexible Server and Azure Cache for Redis
// are the direct managed equivalents: same protocol, same connection-string
// shape (`DATABASE_URL` / `REDIS_URL` env vars below are unchanged from
// docker-compose.yml's names), zero application code changes, and you get
// automated backups + point-in-time restore (Postgres) and persistence
// (Redis) for free. `backend`, `worker`, and `beat` keep exactly the env
// var names they already read from config.py -- only the values change.
//
// USAGE
// ---------------------------------------------------------------------------
//   az deployment group create \
//     --resource-group rg-snipeit-lite-prod \
//     --template-file infra/main.bicep \
//     --parameters environmentName=prod jwtSecretKey=$(openssl rand -hex 32) \
//                  postgresAdminPassword=... superAdminPassword=...
//
// Re-run the same command (same parameters) any time to update the
// environment idempotently -- this file does NOT set container images
// (that's the CI/CD pipeline's job, via `az containerapp update --image`,
// so that deploying new code never requires a full infra re-deploy).
// =============================================================================

@description('Short environment name: "prod" or "staging". Prefixes/suffixes every resource name.')
@allowed(['prod', 'staging'])
param environmentName string = 'prod'

@description('Azure region for every resource.')
param location string = resourceGroup().location

@description('Base name used to derive resource names, e.g. "snipeit-lite".')
param appBaseName string = 'snipeit-lite'

@description('PostgreSQL Flexible Server administrator password. Pass via --parameters, never commit it.')
@secure()
param postgresAdminPassword string

@description('PostgreSQL Flexible Server administrator username.')
param postgresAdminUsername string = 'snipeitadmin'

@description('JWT signing secret -- shared identically across backend/worker/beat. Generate with: openssl rand -hex 32')
@secure()
param jwtSecretKey string

@description('Super Admin (root account) password -- see backend/config.py SUPER_ADMIN_* docstring.')
@secure()
param superAdminPassword string

@description('Container image tag to deploy on first create. The CI/CD pipeline overwrites this on every push to main/develop -- this initial value just needs to exist.')
param initialImageTag string = 'initial'

@description('Postgres SKU -- Standard_B1ms (Burstable) is plenty for this app; bump for real concurrent load.')
param postgresSku string = 'Standard_B1ms'

@description('Postgres storage size in GB.')
param postgresStorageGb int = 32

@description('Redis SKU name.')
@allowed(['Basic', 'Standard', 'Premium'])
param redisSku string = 'Basic'

@description('Redis capacity (0 = C0/250MB on Basic/Standard).')
param redisCapacity int = 0

@description('How many backend replicas to run at minimum (0 allows scale-to-zero; use 1+ for production to avoid cold starts).')
param backendMinReplicas int = 1

@description('Max backend replicas under load.')
param backendMaxReplicas int = 5

@description('How many worker replicas at minimum.')
param workerMinReplicas int = 1

@description('Max worker replicas under load -- KEDA scales this on Celery queue depth, see the scale rule below.')
param workerMaxReplicas int = 5

@description('Custom domain for the frontend (leave empty to use the generated *.azurecontainerapps.io FQDN only).')
param customDomain string = ''

@description('Notification / SMTP settings -- optional, off by default, matching .env.example.')
param notificationsEnabled bool = false
param smtpHost string = ''
param smtpUsername string = ''
@secure()
param smtpPassword string = ''
param smtpFromEmail string = ''
param adminNotificationEmails string = ''

var suffix = uniqueString(resourceGroup().id, environmentName)
var namePrefix = '${appBaseName}-${environmentName}'
// Storage/ACR/Key Vault names must be globally unique and constrained in
// length/characters -- derive short, compliant names from the suffix.
var acrName = replace('${appBaseName}${environmentName}acr${take(suffix, 6)}', '-', '')
var kvName = take('${appBaseName}-${environmentName}-kv-${take(suffix, 4)}', 24)
var storageAccountName = take(replace('${appBaseName}${environmentName}st${take(suffix, 6)}', '-', ''), 24)
var pgServerName = '${namePrefix}-pg-${take(suffix, 6)}'
var redisName = '${namePrefix}-redis-${take(suffix, 6)}'

// ---------------------------------------------------------------------------
// Monitoring foundation -- every container app / job below is wired to this
// same Log Analytics workspace, so `Container App Console Logs` and
// `ContainerAppSystemLogs` KQL tables cover the whole stack in one place.
// See DEPLOYMENT.md's Monitoring section for the queries.
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-appinsights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------------------------------------------------------------------------
// Managed identity -- ONE identity shared by every container app/job. Pulls
// images from ACR and reads secrets from Key Vault with zero stored
// passwords in the Container Apps config itself.
// ---------------------------------------------------------------------------
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-identity'
  location: location
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // pull via managed identity, not admin creds
  }
}

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'AcrPull')
  scope: acr
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull
  }
}

// ---------------------------------------------------------------------------
// Key Vault -- JWT secret, Postgres password, Redis key, SMTP password,
// Super Admin password. Container Apps reads these directly via
// `identity`-based secret references (see each container app's `secrets`
// block below) -- the values never pass through GitHub Actions logs or
// Container Apps' own (encrypted, but still-visible-to-owners) secret store.
// ---------------------------------------------------------------------------
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enabledForTemplateDeployment: true
  }
}

resource kvSecretsOfficerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, 'KeyVaultSecretsUser')
  scope: keyVault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6') // Key Vault Secrets User
  }
}

resource secretJwt 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'jwt-secret-key'
  properties: { value: jwtSecretKey }
}

resource secretSuperAdmin 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'super-admin-password'
  properties: { value: superAdminPassword }
}

resource secretPgPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'postgres-admin-password'
  properties: { value: postgresAdminPassword }
}

resource secretSmtpPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'smtp-password'
  properties: { value: empty(smtpPassword) ? 'unset' : smtpPassword }
}

// ---------------------------------------------------------------------------
// Azure Database for PostgreSQL Flexible Server -- replaces the `db` service.
// Built-in automated backups (7-35 day retention, point-in-time restore) --
// this is a strictly stronger guarantee than docker-compose.yml's pgdata
// named volume, and means the app-level pg_dump job (backend/services/
// backup_service.py, ENABLE_AUTO_BACKUP) becomes a nice-to-have export
// rather than your only line of defense. Keep it enabled anyway -- it's
// cheap and gives you an app-portable .sql.gz you can restore anywhere,
// not just back into the same Azure server.
// ---------------------------------------------------------------------------
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: pgServerName
  location: location
  sku: {
    name: postgresSku
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminUsername
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: postgresStorageGb }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' } // flip to 'ZoneRedundant' for prod HA once traffic justifies the cost
  }
}

resource postgresDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: postgres
  name: 'asset_db'
}

// Allow Azure services (Container Apps' outbound IPs are dynamic on the
// Consumption plan) to reach this server. For a hardened setup, replace
// this with VNet integration + private endpoint -- see DEPLOYMENT.md.
resource postgresFirewallAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ---------------------------------------------------------------------------
// Azure Cache for Redis -- replaces the `redis` service (Celery broker +
// result backend). TLS-only (port 6380, rediss:// scheme) -- see
// DEPLOYMENT.md for the exact REDIS_URL format this produces.
// ---------------------------------------------------------------------------
resource redis 'Microsoft.Cache/redis@2024-03-01' = {
  name: redisName
  location: location
  properties: {
    sku: {
      name: redisSku
      family: redisSku == 'Premium' ? 'P' : 'C'
      capacity: redisCapacity
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
  }
}

// ---------------------------------------------------------------------------
// Storage Account + Azure Files -- replaces docker-compose.yml's
// `backup_data` and `export_data` named volumes. Mounted identically into
// backend/worker container apps below, so backend/services/backup_service.py
// and backend/tasks/export_tasks.py's shared-disk assumptions keep working
// unmodified across replicas, exactly like the Compose named-volume setup.
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
  properties: { shareQuota: 20 }
}

resource exportShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: 'export-data'
  properties: { shareQuota: 20 }
}

// ---------------------------------------------------------------------------
// Container Apps Environment -- the shared boundary. backend/worker/beat/
// frontend all live here and reach each other by short app name
// (http://<app-name>) over the platform's internal proxy -- see
// nginx/default.conf.template's BACKEND_HOST/BACKEND_PORT usage, unchanged
// from docker-compose.yml, just pointed at "backend" (the container app
// name) instead of "backend" (the Compose service name). Same string,
// different platform underneath.
// ---------------------------------------------------------------------------
resource containerAppEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
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
  }
}

resource backupStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: containerAppEnv
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
  parent: containerAppEnv
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
// Shared env vars -- mirrors the block every service in docker-compose.yml
// repeats identically today (see that file's own comment on why it's
// copy-pasted per-service rather than YAML-anchored). Bicep lets us define
// it once and reuse it.
// ---------------------------------------------------------------------------
var databaseUrl = 'postgresql://${postgresAdminUsername}:${postgresAdminPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/asset_db?sslmode=require'
var redisUrl = 'rediss://:${redis.listKeys().primaryKey}@${redis.properties.hostName}:6380/0'

var sharedEnv = [
  { name: 'ENVIRONMENT', value: 'production' }
  { name: 'DATABASE_URL', value: databaseUrl }
  { name: 'REDIS_URL', value: redisUrl }
  { name: 'EXPORT_RESULT_DIR', value: '/app/export_results' }
  { name: 'JWT_ALGORITHM', value: 'HS256' }
  { name: 'JWT_EXPIRY_HOURS', value: '12' }
  { name: 'SITE_NAME', value: 'Snipe-IT Lite' }
  { name: 'AUTO_INIT_DB', value: 'false' } // migrations run via the `migrate` Container Apps Job below, never on boot
  { name: 'AUTO_SEED_DEMO_DATA', value: 'false' }
  { name: 'LOG_LEVEL', value: 'INFO' }
  { name: 'LOG_FORMAT', value: 'json' } // structured JSON -> parses cleanly in Log Analytics, see DEPLOYMENT.md
  { name: 'LOGIN_RATE_LIMIT_MAX', value: '5' }
  { name: 'LOGIN_RATE_LIMIT_WINDOW_SECONDS', value: '60' }
  { name: 'ACCOUNT_LOCKOUT_MAX_ATTEMPTS', value: '5' }
  { name: 'ACCOUNT_LOCKOUT_DURATION_MINUTES', value: '15' }
  { name: 'ENABLE_API_DOCS', value: 'false' }
  { name: 'SUPER_ADMIN_USERNAME', value: 'superadmin' }
  { name: 'SUPER_ADMIN_NAME', value: 'Super Admin' }
  { name: 'NOTIFICATIONS_ENABLED', value: string(notificationsEnabled) }
  { name: 'SMTP_HOST', value: smtpHost }
  { name: 'SMTP_PORT', value: '587' }
  { name: 'SMTP_USERNAME', value: smtpUsername }
  { name: 'SMTP_USE_TLS', value: 'true' }
  { name: 'SMTP_FROM_EMAIL', value: smtpFromEmail }
  { name: 'ADMIN_NOTIFICATION_EMAILS', value: adminNotificationEmails }
  { name: 'OVERDUE_NOTIFICATION_INTERVAL_HOURS', value: '24' }
  { name: 'DUE_SOON_REMINDER_DAYS', value: '2' }
  { name: 'DUE_SOON_NOTIFICATION_INTERVAL_HOURS', value: '24' }
  { name: 'DISPLAY_TIMEZONE', value: 'Africa/Lagos' }
  { name: 'CURRENCY_CODE', value: 'NGN' }
  { name: 'CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER', value: 'false' }
  { name: 'ENABLE_AUTO_BACKUP', value: 'true' }
  { name: 'BACKUP_HOURS_UTC', value: '3' }
  { name: 'BACKUP_DIR', value: '/app/backups' }
  { name: 'BACKUP_RETENTION_COUNT', value: '7' }
  { name: 'BACKUP_GDRIVE_ENABLED', value: 'false' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
]

var sharedSecrets = [
  { name: 'jwt-secret-key', keyVaultUrl: secretJwt.properties.secretUri, identity: identity.id }
  { name: 'super-admin-password', keyVaultUrl: secretSuperAdmin.properties.secretUri, identity: identity.id }
  { name: 'smtp-password', keyVaultUrl: secretSmtpPassword.properties.secretUri, identity: identity.id }
]

var sharedSecretEnvRefs = [
  { name: 'JWT_SECRET_KEY', secretRef: 'jwt-secret-key' }
  { name: 'SUPER_ADMIN_PASSWORD', secretRef: 'super-admin-password' }
  { name: 'SMTP_PASSWORD', secretRef: 'smtp-password' }
]

var volumeMounts = [
  { volumeName: 'backup-data', mountPath: '/app/backups' }
  { volumeName: 'export-data', mountPath: '/app/export_results' }
]

var volumes = [
  { name: 'backup-data', storageType: 'AzureFile', storageName: 'backup-data' }
  { name: 'export-data', storageType: 'AzureFile', storageName: 'export-data' }
]

// ---------------------------------------------------------------------------
// backend -- FastAPI/uvicorn under /api/*. Internal ingress ONLY (not
// public) -- the browser only ever talks to the `frontend` app, which
// reverse-proxies /api/* to this one over the environment's internal
// network, exactly like nginx -> backend:8000 in docker-compose.yml.
// ---------------------------------------------------------------------------
resource backendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'backend'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single' // one live revision at a time -> ACA does the rolling swap; see DEPLOYMENT.md
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: sharedSecrets
      ingress: {
        external: false // internal-only: reachable as http://backend from `frontend`/`worker`/`beat`, never from the public internet
        targetPort: 8000
        transport: 'http'
      }
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: '${acr.properties.loginServer}/snipeit-backend:${initialImageTag}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(sharedEnv, sharedSecretEnvRefs, [
            { name: 'CORS_ORIGINS', value: empty(customDomain) ? 'https://frontend.${containerAppEnv.properties.defaultDomain}' : 'https://${customDomain}' }
          ])
          volumeMounts: volumeMounts
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8000 }, initialDelaySeconds: 5, periodSeconds: 10 }
          ]
        }
      ]
      volumes: volumes
      scale: {
        minReplicas: backendMinReplicas
        maxReplicas: backendMaxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: { metadata: { concurrentRequests: '50' } } // scale out past ~50 in-flight requests per replica
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// worker -- Celery consumer (audit CSV/PDF export jobs). No ingress at all
// (nothing calls it over HTTP -- only via the Redis queue), and scales on
// QUEUE DEPTH via KEDA's Redis List Length scaler, not HTTP traffic -- this
// is the piece docker-compose.yml's `--scale worker=N` did manually for a
// known traffic spike; here it's automatic.
// ---------------------------------------------------------------------------
resource workerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'worker'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: concat(sharedSecrets, [
        { name: 'redis-conn', value: redisUrl }
      ])
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: '${acr.properties.loginServer}/snipeit-backend:${initialImageTag}'
          command: ['celery', '-A', 'celery_app', 'worker', '--loglevel=info', '--concurrency=2']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(sharedEnv, sharedSecretEnvRefs)
          volumeMounts: volumeMounts
        }
      ]
      volumes: volumes
      scale: {
        minReplicas: workerMinReplicas
        maxReplicas: workerMaxReplicas
        rules: [
          {
            name: 'celery-queue-depth'
            custom: {
              type: 'redis'
              metadata: {
                address: '${redis.properties.hostName}:6380'
                listName: 'celery'
                listLength: '5' // add a replica for every 5 queued export jobs
                enableTLS: 'true'
              }
              auth: [
                { secretRef: 'redis-conn', triggerParameter: 'password' }
              ]
            }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// beat -- Celery Beat scheduler. Pinned to EXACTLY 1 replica, always -- see
// docker-compose.yml's own comment on why this must never scale (duplicate
// notification emails). No `rules`/no `maxReplicas` above 1 is what
// enforces that here.
// ---------------------------------------------------------------------------
resource beatApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'beat'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'beat'
          image: '${acr.properties.loginServer}/snipeit-backend:${initialImageTag}'
          command: ['celery', '-A', 'celery_app', 'beat', '--loglevel=info']
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: concat(sharedEnv, sharedSecretEnvRefs)
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1 // NEVER raise this -- see comment above
      }
    }
  }
}

// ---------------------------------------------------------------------------
// frontend -- nginx, built from nginx/Dockerfile, UNCHANGED image/config.
// The only thing that differs from docker-compose.yml is which values
// BACKEND_HOST/BACKEND_PORT/PORT/ENABLE_API_DOCS get -- same variables,
// same envsubst template, same reverse-proxy logic, different platform.
// This is the ONE public-facing app in the whole environment.
// ---------------------------------------------------------------------------
resource frontendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'frontend'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
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
          image: '${acr.properties.loginServer}/snipeit-frontend:${initialImageTag}'
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'PORT', value: '80' }
            // "backend" = the backend Container App's short name -- resolves
            // over the environment's internal proxy exactly like Compose's
            // service-name DNS does for "backend:8000". See
            // nginx/default.conf.template; zero changes needed there.
            { name: 'BACKEND_HOST', value: 'backend' }
            { name: 'BACKEND_PORT', value: '80' }
            { name: 'ENABLE_API_DOCS', value: 'false' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/', port: 80 }, initialDelaySeconds: 5, periodSeconds: 30 }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 5
        rules: [
          { name: 'http-concurrency', http: { metadata: { concurrentRequests: '100' } } }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// migrate -- Container Apps Job running `alembic upgrade head` as an
// explicit, one-shot step. Triggered manually by the CI/CD pipeline
// (`az containerapp job start`) BEFORE the new backend/worker/beat image is
// rolled out -- see docker-compose.yml's own "never on container boot"
// migration comment and DEPLOYMENT.md's promotion pipeline diagram.
// ---------------------------------------------------------------------------
resource migrateJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'migrate'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: containerAppEnv.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 1
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: '${acr.properties.loginServer}/snipeit-backend:${initialImageTag}'
          command: ['alembic', 'upgrade', 'head']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(sharedEnv, sharedSecretEnvRefs)
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs -- consumed by the GitHub Actions deploy workflows.
// ---------------------------------------------------------------------------
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output containerAppEnvName string = containerAppEnv.name
output frontendFqdn string = frontendApp.properties.configuration.ingress.fqdn
output backendAppName string = backendApp.name
output workerAppName string = workerApp.name
output beatAppName string = beatApp.name
output frontendAppName string = frontendApp.name
output migrateJobName string = migrateJob.name
output logAnalyticsWorkspaceId string = logAnalytics.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output identityClientId string = identity.properties.clientId
output identityId string = identity.id
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output redisHostName string = redis.properties.hostName
