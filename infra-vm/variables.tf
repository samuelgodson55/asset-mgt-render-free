# =============================================================================
# infra-vm/variables.tf
# -----------------------------------------------------------------------------
# Every input this stack accepts. Mirrors infra/main.bicep's parameter list
# where the concept overlaps (same defaults, same naming where sensible) so
# switching between the Container Apps path and this VM path is predictable.
# Copy infra-vm/terraform.tfvars.example to terraform.tfvars and fill in the
# handful that have no default (marked below) before `terraform apply`.
# =============================================================================

variable "subscription_id" {
  description = "Azure subscription ID to deploy into. Find yours with: az account show --query id -o tsv"
  type        = string
}

variable "environment_name" {
  description = "Short environment name: \"prod\" or \"staging\". Prefixes every resource name and is used as the VM's DNS label. Defaults to \"prod\" -- the infrastructure default across this whole repo (mirrors infra/main.bicep's environmentName default, deploy-azure-vm.yml's/deploy-azure-aca.yml's dropdown defaults, and docker-compose.vm.yml's own ENVIRONMENT fallback -- see each of their own comments). infra-deploy-vm.yml always passes TF_VAR_environment_name explicitly (no default there either, `required: true` with no `default:` on that workflow_dispatch input), so this default only matters for a manual/local terraform run that forgets to set -var environment_name -- it'll provision prod-named resources rather than silently standing up a staging environment nobody asked for."
  type        = string
  default     = "prod"
  validation {
    condition     = contains(["prod", "staging"], var.environment_name)
    error_message = "environment_name must be \"prod\" or \"staging\"."
  }
}

variable "location" {
  description = "Azure region for every resource, e.g. \"eastus\", \"westeurope\". List regions with: az account list-locations -o table"
  type        = string
  default     = "eastus"
}

variable "app_base_name" {
  description = "Base name used to derive resource names, e.g. \"snipeit-lite\". Keep it short -- it also feeds the VM's public DNS label (<app_base_name>-<environment_name>.<region>.cloudapp.azure.com), which Azure caps at 63 characters total."
  type        = string
  default     = "snipeit-lite"
}

# -----------------------------------------------------------------------------
# VM sizing
# -----------------------------------------------------------------------------

variable "vm_size" {
  description = "Azure VM SKU. Standard_B2s (2 vCPU burstable, 4 GiB RAM) is the smallest size that comfortably runs all six containers (db, redis, backend, worker, beat, frontend/Caddy) together for light-to-moderate traffic -- see DEPLOYMENT_VM.md's Cost section for the full sizing table and when to size up to Standard_B2ms."
  type        = string
  default     = "Standard_B2s"
}

variable "os_disk_size_gb" {
  description = "OS disk size in GiB. Only holds the OS + Docker engine itself -- application data lives on the separate data disk below. 30 is the smallest Azure allows for the Ubuntu 22.04 LTS image used here."
  type        = number
  default     = 30
}

variable "data_disk_size_gb" {
  description = "Size in GiB of the separate managed data disk mounted at /mnt/docker-data, which becomes Docker's data-root -- every container, image layer, and named volume (Postgres data, Redis AOF, backup/export files) lives here, not on the OS disk. Can only be INCREASED later (Azure managed disks cannot shrink), so start modest and grow if you actually need to -- see DEPLOYMENT_VM.md's \"Growing the data disk\" section for the resize procedure."
  type        = number
  default     = 32
}

variable "admin_username" {
  description = "Linux admin username created on the VM. Avoid \"admin\"/\"administrator\"/\"root\" -- Azure rejects several reserved names outright."
  type        = string
  default     = "azureuser"
}

variable "ssh_public_key" {
  description = "Your SSH PUBLIC key contents (e.g. the contents of ~/.ssh/id_rsa.pub). Still required -- Azure needs SOME auth method on the VM (password auth is disabled entirely) -- but this key is only ever presented through the Cloudflare Tunnel's SSH route (see the \"Cloudflare Tunnel\" section of main.tf), never over the public internet, since no inbound NSG rule for port 22 exists by default (see ssh_allowed_source_ips below). Generate a dedicated deploy key pair with: ssh-keygen -t rsa -b 4096 -C \"snipeit-lite-vm-deploy\" -f ./snipeit_vm_deploy_key -N \"\" -- then pass the .pub file's contents here and keep the private half for the VM_SSH_PRIVATE_KEY GitHub secret (see DEPLOYMENT_VM.md step 5). MUST be RSA -- Azure's admin_ssh_key rejects ed25519 keys outright (\\\"Only RSA SSH keys are supported by Azure\\\")."
  type        = string
}

variable "ssh_allowed_source_ips" {
  description = "CIDR ranges allowed to reach port 22 over the PUBLIC internet, directly on the VM's NSG. Defaults to an EMPTY list -- meaning no inbound NSG rule for port 22 is created at all, full stop -- because SSH is reached through the Cloudflare Tunnel instead (see cloudflare_zero_trust_tunnel_cloudflared_config in main.tf), which needs no inbound NSG rule of any kind: the VM only ever makes an OUTBOUND connection to Cloudflare's edge to register the tunnel, and Access-gated SSH traffic rides back over that same outbound connection. Only set this to something non-empty (e.g. [\"203.0.113.4/32\"], your own IP) as a temporary break-glass measure if Cloudflare's network is ever unreachable from where you are -- see DEPLOYMENT_VM.md's \"No open ports\" section and its Troubleshooting entry on being locked out."
  type        = list(string)
  default     = []
}

# -----------------------------------------------------------------------------
# Cloudflare Tunnel -- how the VM is reached for BOTH the web app and SSH,
# with ZERO inbound ports open on the NSG, without Tailscale and without
# Azure Bastion. The VM makes one outbound, always-on connection to
# Cloudflare's edge (via the `cloudflared` container in docker-compose.vm.yml)
# and registers itself as that tunnel's only endpoint -- Cloudflare then
# proxies both https://<custom_domain> (-> Caddy) and ssh.<custom_domain>
# (-> this VM's sshd) over that one outbound connection. Nothing about this
# depends on Tailscale's own coordination/DERP infrastructure being
# reachable, which is the whole point if that's what you've been fighting.
# See DEPLOYMENT_VM.md's "Set up Cloudflare Tunnel" section for full setup
# (free on Cloudflare's free plan, no credit card required for Tunnel/Access).
# -----------------------------------------------------------------------------

variable "cloudflare_api_token" {
  description = "Cloudflare API token used by Terraform to create the Tunnel, its DNS records, and the Access application/policy protecting SSH. Scope it to: Account > Cloudflare Tunnel > Edit, Account > Access: Apps and Policies > Edit, Zone > DNS > Edit (restricted to cloudflare_zone_id below) -- create at https://dash.cloudflare.com/profile/api-tokens. Never commit the real value; pass it as TF_VAR_cloudflare_api_token (see infra-deploy-vm.yml)."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Your Cloudflare account ID (Dashboard -> right sidebar of any domain's overview page, or `cloudflare accounts` via `cloudflared` isn't a thing -- use the dashboard). Owns the Tunnel and the Access application."
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Zone ID of the Cloudflare-managed DNS zone that custom_domain belongs to (Dashboard -> your domain's overview page -> right sidebar). custom_domain's nameservers must already point at Cloudflare for the DNS records this stack creates to do anything."
  type        = string
}

variable "cloudflare_zone_name" {
  description = "The apex domain as registered in Cloudflare, e.g. \"example.com\" (NOT a subdomain). Used only to compute DNS record names relative to the zone -- e.g. if custom_domain is \"assets.example.com\", this must be \"example.com\"; if custom_domain IS the apex, set both to the same value."
  type        = string
}

variable "ssh_access_allowed_emails" {
  description = "Email addresses allowed through Cloudflare Access to reach ssh.<custom_domain> interactively (browser SSO login the first time, cached after that). Add every human who needs to SSH in. CI/CD (deploy-azure-vm.yml, sync-secrets-vm.yml) authenticates separately via a Service Token instead (see cloudflare_zero_trust_access_service_token in main.tf) and does not need to be listed here."
  type        = list(string)
}

variable "cloudflare_origin_cert" {
  description = "PEM contents of a Cloudflare Origin CA certificate covering custom_domain (Dashboard -> SSL/TLS -> Origin Server -> Create Certificate; choose \"Let Cloudflare generate a private key\", list custom_domain and *.<cloudflare_zone_name> as hostnames, 15-year validity is fine since only Cloudflare's edge ever validates it). Caddy uses this instead of Let's Encrypt/ACME now that nothing but the Cloudflare Tunnel ever reaches it -- see Caddyfile."
  type        = string
  sensitive   = true
}

variable "cloudflare_origin_cert_key" {
  description = "PEM private key that pairs with cloudflare_origin_cert, from the same \"Create Certificate\" step. Keep this at least as protected as jwt_secret_key -- anyone with it and network access to Caddy could impersonate the origin to a client that already trusts Cloudflare's Origin CA (in practice: no one but Cloudflare's edge, since the tunnel is the only path in)."
  type        = string
  sensitive   = true
}

# -----------------------------------------------------------------------------
# Application domain / TLS
# -----------------------------------------------------------------------------

variable "custom_domain" {
  description = "Your own domain/subdomain, e.g. \"assets.example.com\", that resolves through Cloudflare's proxy to this app (Cloudflare creates the DNS record itself -- see the cloudflare_record resources in main.tf -- so do NOT create your own A/CNAME record for it). REQUIRED in this Cloudflare Tunnel setup: unlike the old direct-IP + Let's Encrypt path, there's no bare public IP left to fall back to a free sslip.io hostname for -- the domain must live in the Cloudflare zone identified by cloudflare_zone_id. Buy a cheap domain and move its nameservers to Cloudflare (free) if you don't have one yet -- see DEPLOYMENT_VM.md's \"Set up Cloudflare Tunnel\" section."
  type        = string

  validation {
    condition     = length(var.custom_domain) > 0
    error_message = "custom_domain is required -- Cloudflare Tunnel needs a real hostname in your Cloudflare zone to route to, not just the VM's bare IP."
  }
}

# -----------------------------------------------------------------------------
# Application secrets -- passed through to the VM's /opt/snipeit/.env at
# provisioning time via cloud-init, then read by docker-compose.vm.yml. All
# marked sensitive so `terraform plan`/`apply` output and any CI log never
# print them. NEVER commit a terraform.tfvars file containing real values --
# it's already listed in infra-vm/.gitignore.
# -----------------------------------------------------------------------------

variable "postgres_password" {
  description = "Password for the `db` container's postgres superuser. Generate with: openssl rand -base64 24"
  type        = string
  sensitive   = true
}

variable "postgres_user" {
  description = "Username for the `db` container's postgres superuser."
  type        = string
  default     = "admin"
}

variable "postgres_db" {
  description = "Database name created inside the `db` container."
  type        = string
  default     = "asset_db"
}

variable "errorbeacon_ingest_api_key" {
  description = "API key used by application producers to submit events to ErrorBeacon."
  type        = string
  sensitive   = true
  default     = ""
}

variable "errorbeacon_admin_api_key" {
  description = "API key used by operators and management endpoints to read/control ErrorBeacon."
  type        = string
  sensitive   = true
  default     = ""
}


variable "errorbeacon_telegram_bot_token" {
  description = "Telegram Bot API token used by ErrorBeacon."
  type        = string
  sensitive   = true
}

variable "errorbeacon_telegram_chat_id" {
  description = "Telegram destination chat ID for ErrorBeacon alerts."
  type        = string
  sensitive   = true
}

variable "errorbeacon_telegram_thread_id" {
  description = "Optional Telegram forum topic/thread ID."
  type        = string
  default     = ""
}

variable "errorbeacon_gemini_api_key" {
  description = "Optional Gemini API key for second-stage AI incident analysis."
  type        = string
  sensitive   = true
  default     = ""
}
variable "errorbeacon_groq_api_key" {
  type      = string
  sensitive = true
  default   = ""
}
variable "errorbeacon_groq_model" {
  type    = string
  default = "llama-3.1-8b-instant"
}
variable "errorbeacon_openrouter_api_key" {
  type      = string
  sensitive = true
  default   = ""
}
variable "errorbeacon_openrouter_model" {
  type    = string
  default = "openrouter/free"
}

variable "errorbeacon_image" {
  description = "Docker image repository for ErrorBeacon."
  type        = string
  default     = "samuelgodson55/errorbeacon-lite"
}

variable "errorbeacon_app" {
  description = "Identity string backend/worker/beat report themselves as via the ERRORBEACON_APP env var -- shows up as the \"app\" field on every event ErrorBeacon receives. Matches docker-compose.yml/docker-compose.vm.yml's own ERRORBEACON_APP default so all three deployment paths report under the same identity unless intentionally overridden."
  type        = string
  default     = "asset-inventory-quotes"
}

variable "jwt_secret_key" {
  description = "JWT signing secret. Generate with: openssl rand -hex 32"
  type        = string
  sensitive   = true
}

variable "root_admin_bootstrap_password" {
  description = "OPTIONAL. One-time root admin bootstrap password, read once by the first `alembic upgrade head` run. Leave empty to have that migration generate and print a random password to the migration's log output instead (see DEPLOYMENT_VM.md step 8)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "deploy_status_user" {
  description = "HTTP Basic Auth username for the /_deploy/ rollout-status dashboard (see Caddyfile and DEPLOYMENT_VM.md's \"Monitoring a rollout\" section). Set this via a repo/environment secret (DEPLOY_STATUS_USER) so first boot comes up with real credentials instead of the fail-closed placeholder below -- no SSH required. sync-secrets-vm.yml can also push a changed value onto an already-provisioned VM."
  type        = string
  default     = "admin"
}

variable "deploy_status_password_hash" {
  description = "OPTIONAL. Bcrypt hash (as produced by `docker run --rm caddy:2-alpine caddy hash-password`) for the /_deploy/ dashboard's Basic Auth password. Set this via a repo/environment secret (DEPLOY_STATUS_PASSWORD_HASH) so first boot -- and every later sync-secrets-vm.yml run -- uses real credentials without ever SSHing in by hand. Leave empty to fall back to a random, never-recorded hash: the route then fails CLOSED (nobody can log in) rather than open, until you set a real one."
  type        = string
  sensitive   = true
  default     = ""
}

variable "site_name" {

  description = "Brand name shown in the navbar/login header and PDF export letterhead."
  type        = string
  default     = "Snipe-IT Lite"
}

variable "enable_api_docs" {
  description = "Expose /docs, /redoc, /openapi.json. Keep false on anything internet-facing unless you specifically need it."
  type        = bool
  default     = false
}

variable "notifications_enabled" {
  description = "Turn on SMTP email notifications (extension requests, overdue/due-soon digests)."
  type        = bool
  default     = true
}

variable "smtp_host" {
  type    = string
  default = ""
}
variable "smtp_port" {
  type    = number
  default = 587
}
variable "smtp_username" {
  type    = string
  default = ""
}
variable "smtp_password" {
  type      = string
  sensitive = true
  default   = ""
}
variable "smtp_from_email" {
  type    = string
  default = ""
}

variable "email_provider" {
  description = "Which transport send_email() uses on the VM: \"smtp\" (default), or an HTTP-API provider (\"brevo\"/\"resend\") -- matches .env.example's EMAIL_PROVIDER and backend/config.py's EMAIL_PROVIDER docstring. The VM has no outbound-port restriction the way Render's Free plan does (see that docstring), so \"smtp\" is fine here too -- brevo/resend are still available for parity with the other deploy targets, or if your own network/ISP blocks outbound SMTP."
  type        = string
  default     = "smtp"
}
variable "brevo_api_key" {
  description = "https://app.brevo.com/settings/keys/api -- only read when email_provider is \"brevo\"."
  type        = string
  sensitive   = true
  default     = ""
}
variable "resend_api_key" {
  description = "https://resend.com/api-keys -- only read when email_provider is \"resend\"."
  type        = string
  sensitive   = true
  default     = ""
}
variable "admin_notification_emails" {
  type    = string
  default = ""
}

variable "overdue_digest_hours_utc" {
  description = "Comma-separated hours of day (UTC, each 0-23) the worker checks for overdue checkouts and emails the admin/manager digest, e.g. \"8\" or \"8,20\" -- matches .env.example's OVERDUE_DIGEST_HOURS_UTC."
  type        = string
  default     = "8"
}

variable "due_soon_digest_hours_utc" {
  description = "Comma-separated hours of day (UTC, each 0-23) the worker checks for checkouts about to go overdue and emails the reminder digest, e.g. \"8\" or \"8,20\" -- matches .env.example's DUE_SOON_DIGEST_HOURS_UTC."
  type        = string
  default     = "8"
}

variable "display_timezone" {
  description = "IANA timezone name used to render CSV/PDF export timestamps. Data itself is always stored as UTC."
  type        = string
  default     = "Africa/Lagos"
}

# --- Pending-approval SLA nudges (ExtensionRequest & Quotation) -----------
# Matches .env.example's own "Pending-approval SLA nudges" block -- see
# backend/tasks/sla_tasks.py's module docstring for the full "why".
variable "extension_request_sla_hours" {
  description = "How many hours a `pending` ExtensionRequest can go without a Manager/Admin/Super Admin decision before the SLA-nudge digest escalates it -- matches .env.example's EXTENSION_REQUEST_SLA_HOURS."
  type        = string
  default     = "24"
}

variable "quotation_sla_hours" {
  description = "How many hours a `submitted` Quotation can go without an Admin/Manager decision before the SLA-nudge digest escalates it -- matches .env.example's QUOTATION_SLA_HOURS."
  type        = string
  default     = "24"
}

variable "approval_sla_check_interval_minutes" {
  description = "How often, in minutes, the worker checks both pending-approval queues for anything past its SLA threshold -- matches .env.example's APPROVAL_SLA_CHECK_INTERVAL_MINUTES."
  type        = string
  default     = "60"
}

variable "approval_sla_escalation_repeat_hours" {
  description = "Once a pending request/quote has been escalated, how many hours before it's eligible to be escalated again if still undecided -- matches .env.example's APPROVAL_SLA_ESCALATION_REPEAT_HOURS."
  type        = string
  default     = "24"
}

variable "send_quotation_recipient_emails" {
  description = "Whether a Quotation's own recipient gets emailed on every change (line items, notes, discount, assignment, approval, fulfillment), on top of the in-app bell notification which is always created regardless -- matches .env.example's SEND_QUOTATION_RECIPIENT_EMAILS."
  type        = string
  default     = "true"
}

variable "currency_code" {
  description = "ISO 4217 currency code applied everywhere a price is shown or exported."
  type        = string
  default     = "NGN"
}

variable "enable_auto_backup" {
  description = "Enable the app's in-process pg_dump backup job (writes to the data disk under /app/backups, in addition to Terraform's own scheduled disk snapshots -- see infra-vm/main.tf's snapshot resources)."
  type        = bool
  default     = true
}

variable "backup_gdrive_enabled" {
  type    = bool
  default = false
}
variable "backup_gdrive_oauth_client_id" {
  type    = string
  default = ""
}
variable "backup_gdrive_oauth_client_secret" {
  type      = string
  sensitive = true
  default   = ""
}
variable "backup_gdrive_oauth_refresh_token" {
  type      = string
  sensitive = true
  default   = ""
}
# Mode 2 -- Google Workspace service account + Shared Drive (the raw JSON
# key contents, one line). BUG FIX: this was never added here even though
# backend/config.py, backend/services/backup_service.py, docker-compose.yml,
# and render.yaml all already support it -- meaning the VM path could only
# ever use Mode 1 (personal OAuth) backups, silently, with no error, even if
# you set this expecting Mode 2 to work. See docker-compose.vm.yml's matching
# comment on this same variable's env var.
variable "backup_gdrive_credentials_json" {
  type      = string
  sensitive = true
  default   = ""
}
variable "backup_gdrive_folder_id" {
  type    = string
  default = ""
}

# -----------------------------------------------------------------------------
# Distributed tracing (OpenTelemetry -- Operations & Observability
# requirement #4; see backend/telemetry.py's module docstring). Off by
# default, matching every other opt-in flag in this file -- mirrors
# infra/main.bicep's otel* params (the Container Apps path's equivalent)
# and .env.example's OTEL_* defaults one-for-one, so the same mental model
# applies whichever deployment target you're using.
# -----------------------------------------------------------------------------

variable "otel_enabled" {
  description = "Master switch for OpenTelemetry distributed tracing on `backend` (and its embedded Celery worker/beat). Off by default -- zero cost, zero behavior change. Turning this on with no exporter destination configured just means spans are created and immediately discarded -- harmless, but pointless. Matches .env.example's OTEL_ENABLED."
  type        = bool
  default     = false
}
variable "otel_service_name" {
  description = "service.name resource attribute every span from `backend` carries. Matches .env.example's OTEL_SERVICE_NAME."
  type        = string
  default     = "snipeit-lite-backend"
}
variable "otel_exporter_otlp_endpoint" {
  description = "OTLP/HTTP collector endpoint spans are exported to. Defaults to the VM's own opt-in `jaeger` service (docker-compose.vm.yml, started via `--profile tracing`) -- override this if you'd rather point at a remote collector/SaaS tracing backend instead."
  type        = string
  default     = "http://jaeger:4318"
}
variable "otel_exporter_otlp_headers" {
  description = "Comma-separated key=value auth headers sent with every OTLP export request (e.g. an API key some SaaS tracing backends require). Treated as sensitive since it commonly carries a credential."
  type        = string
  sensitive   = true
  default     = ""
}
variable "otel_traces_sample_ratio" {
  description = "Fraction (0.0-1.0) of traces actually sampled/exported. 1.0 (the default) traces everything -- fine at this app's scale."
  type        = string
  default     = "1.0"
}
variable "otel_console_exporter" {
  description = "Also print every span to stdout as it finishes -- handy for a first no-Jaeger-needed smoke test, noisy otherwise. Off by default."
  type        = bool
  default     = false
}
variable "applicationinsights_connection_string" {
  description = "Routes spans straight to an Azure Application Insights resource instead of (or alongside) otel_exporter_otlp_endpoint. This VM path provisions no Application Insights resource of its own (that's infra/main.bicep's otelAzureMonitorEnabled, the Container Apps path) -- set this yourself if you have one elsewhere you want this VM's traces to reach. Empty (the default) skips this exporter entirely."
  type        = string
  sensitive   = true
  default     = ""
}

# -----------------------------------------------------------------------------
# Container images -- set once here so the VM's very first boot already has
# something to run; every deploy AFTER that is handled by
# .github/workflows/deploy-azure-vm.yml updating /opt/snipeit/.env's
# IMAGE_TAG over SSH and re-running `docker compose up -d`, not by
# re-applying Terraform (see DEPLOYMENT_VM.md step 9).
# -----------------------------------------------------------------------------

variable "dockerhub_backend_image" {
  description = "Docker Hub repository for the backend image, e.g. \"yourusername/snipeit-lite-backend\"."
  type        = string
}

variable "dockerhub_frontend_image" {
  description = "Docker Hub repository for the frontend image, e.g. \"yourusername/snipeit-lite-frontend-legacy\" or \"yourusername/snipeit-lite-frontend-react\" -- MUST match whichever flavor frontend_build_target below names (there are two separate, mutually exclusive Docker Hub repos now, one per frontend flavor -- see frontend/Dockerfile's own top-of-file comment for why)."
  type        = string
}

variable "frontend_build_target" {
  description = "Which of frontend/Dockerfile's two mutually exclusive final stages the image in dockerhub_frontend_image is EXPECTED to be built from: \"react\" (frontend-react-only, the React \"Ledger\" SPA at /) or \"legacy\" (frontend-legacy-only, the legacy static site at /, the default). This VM path pulls a pre-built image over SSH (docker-compose.vm.yml's `image:` line) rather than building it itself -- the actual build target is chosen in CI by that Docker Hub image's paired GitHub Environment's own FRONTEND_BUILD_TARGET Actions variable (see .github/workflows/deploy-azure-vm.yml's resolve-target job and frontend-app/README.md's \"Detaching this app\" section), NOT by this variable. This is documentation/expectation only: it's written into /opt/snipeit/.env as EXPECTED_FRONTEND_BUILD_TARGET so an operator SSHed into the VM (or reading Terraform state) can see at a glance which kind of image this environment is supposed to be running, and so it doesn't silently drift out of sync with whichever GitHub Environment variable actually controls the CI build -- keep the two in agreement by hand, INCLUDING dockerhub_frontend_image above, which must point at the matching flavor's own Docker Hub repo."
  type        = string
  default     = "legacy"
  validation {
    condition     = contains(["react", "legacy"], var.frontend_build_target)
    error_message = "frontend_build_target must be exactly \"react\" or \"legacy\" -- see frontend/Dockerfile's own stage names (frontend-react-only / frontend-legacy-only)."
  }
}

variable "initial_image_tag" {
  description = "Image tag to deploy on first VM boot, applied to both images. Every push to the tracked branch afterwards updates this on the VM directly via deploy-azure-vm.yml (see that workflow) -- re-applying this Terraform stack later will NOT roll the running containers back to this value, since cloud-init only runs once, on first boot."
  type        = string
  default     = "latest"
}

variable "dockerhub_username" {
  description = "OPTIONAL. Set only if dockerhub_backend_image/dockerhub_frontend_image are PRIVATE repositories. Leave empty (default) if both are public -- no registry login needed on the VM at all."
  type        = string
  default     = ""
}

variable "dockerhub_token" {
  description = "Docker Hub Personal Access Token, only required if dockerhub_username is set."
  type        = string
  sensitive   = true
  default     = ""
}

# -----------------------------------------------------------------------------
# Snapshots (disk-level backup of the data disk, independent of the app's own
# pg_dump job -- covers Redis/export files too, and is a fast whole-disk
# restore path if the VM itself is ever lost)
# -----------------------------------------------------------------------------

variable "enable_data_disk_snapshots" {
  description = "Enable Terraform-managed Azure Backup protection for the VM/data workload. Controlled by the GitHub Environment Variable ENABLE_DATA_DISK_SNAPSHOTS; defaults to true. Disabling stops Terraform-managed protection but is not a recovery-point deletion mechanism."
  type        = bool
  default     = true
}

variable "snapshot_retention_days" {
  description = "How many daily snapshots to retain before the oldest is deleted."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Extra resource tags applied to everything this stack creates, merged with a few fixed tags (app, environment, managed-by)."
  type        = map(string)
  default     = {}
}
