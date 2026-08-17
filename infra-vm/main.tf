# =============================================================================
# infra-vm/main.tf
# -----------------------------------------------------------------------------
# Everything Azure-side for the VM deployment target:
#   resource group -> vnet/subnet -> NSG -> static public IP -> NIC -> VM
#   + a separate managed data disk for all application data
#   + (optional) a daily snapshot policy for that data disk
#
# cloud-init.yaml does the actual OS-level setup (Docker install, UFW,
# fail2ban, the /opt/snipeit/.env + docker-compose.vm.yml + Caddyfile that
# get the app running on first boot) -- this file's job is purely
# provisioning the Azure resources and handing cloud-init the values it
# needs (rendered via templatefile() below).
# =============================================================================

locals {
  name_prefix = "${var.app_base_name}-${var.environment_name}"

  common_tags = merge({
    app         = var.app_base_name
    environment = var.environment_name
    managed-by  = "terraform"
  }, var.tags)

  # DNS label Azure attaches to the public IP -> gives us a stable
  # <label>.<region>.cloudapp.azure.com FQDN even before any DNS record of
  # our own exists. Must be globally unique across ALL of Azure, lowercase,
  # start with a letter -- app_base_name + environment_name alone isn't
  # guaranteed unique across every Azure customer worldwide, so
  # random_string.suffix (kept STABLE across re-applies via its own
  # resource identity, not regenerated on every plan) is appended to make
  # a collision practically impossible without you having to pick a
  # unique name yourself.
  dns_label = lower(replace("${var.app_base_name}-${var.environment_name}-${random_string.suffix.result}", "_", "-"))
}

# A short random suffix appended to local.dns_label above so the public
# IP's DNS label doesn't collide with someone else's identically-named
# deployment somewhere else in Azure. Generated once and then left alone
# by Terraform on every later plan/apply (that's what makes a `resource`
# stable here instead of a `random_string` regenerating on every run) --
# the label only changes if this resource is explicitly tainted/destroyed.
resource "random_string" "suffix" {
  length  = 4
  special = false
  upper   = false
  numeric = true
  lower   = true
}

# -----------------------------------------------------------------------------
# Resource group -- everything this stack creates lives in exactly one, so
# `terraform destroy` (or deleting the group in the Portal) cleanly removes
# the whole deployment with nothing orphaned elsewhere.
# -----------------------------------------------------------------------------
resource "azurerm_resource_group" "this" {
  name     = "rg-${local.name_prefix}-vm"
  location = var.location
  tags     = local.common_tags
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------
resource "azurerm_virtual_network" "this" {
  name                = "vnet-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  address_space       = ["10.20.0.0/24"]
  tags                = local.common_tags
}

resource "azurerm_subnet" "this" {
  name                 = "snet-app"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.20.0.0/26"]
}

resource "azurerm_network_security_group" "this" {
  name                = "nsg-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.common_tags

  # -------------------------------------------------------------------
  # Break-glass ONLY: this rule is entirely ABSENT unless you explicitly
  # set ssh_allowed_source_ips to something non-empty. SSH is reached
  # through the Cloudflare Tunnel instead (see the "Cloudflare Tunnel"
  # section below and docker-compose.vm.yml's `cloudflared` service): the
  # VM only makes an OUTBOUND connection to Cloudflare's edge, and Azure
  # NSGs automatically permit return traffic on a flow the VM itself
  # initiated, with no explicit inbound "allow" needed. This dynamic
  # block exists purely so you have a documented, deliberate way to
  # temporarily open direct SSH (e.g. if Cloudflare's network is ever
  # unreachable from where you are) without hand-editing the NSG -- see
  # DEPLOYMENT_VM.md's Troubleshooting section.
  # -------------------------------------------------------------------
  dynamic "security_rule" {
    for_each = length(var.ssh_allowed_source_ips) > 0 ? [1] : []
    content {
      name                       = "AllowSSH"
      priority                   = 100
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "22"
      source_address_prefixes    = var.ssh_allowed_source_ips
      destination_address_prefix = "*"
    }
  }

  # Deliberately no rules for 80/443/443-udp here. `cloudflared` (see
  # docker-compose.vm.yml) makes the only connection, purely outbound to
  # Cloudflare's edge, which proxies both the app and SSH over that one
  # connection -- there is no public IP-based path in at all. See
  # Caddyfile's top comment and DEPLOYMENT_VM.md's "Set up Cloudflare
  # Tunnel" section.

  security_rule {
    name                       = "DenyAllOtherInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
    # Explicit deny-all, ranked last -- Azure NSGs already deny
    # unmatched inbound traffic by default, but spelling it out here
    # means anyone reading this file doesn't have to know that Azure
    # default to confirm nothing else is reachable.
  }
}

resource "azurerm_subnet_network_security_group_association" "this" {
  subnet_id                 = azurerm_subnet.this.id
  network_security_group_id = azurerm_network_security_group.this.id
}

# -----------------------------------------------------------------------------
# BUG FIX: on a fresh (or freshly recreated) apply, azurerm_network_interface
# .this below used to fail intermittently with:
#   Error: creating Network Interface ... InvalidResourceReference: Resource
#   .../subnets/snet-app referenced by resource .../networkInterfaces/nic-...
#   was not found.
# and azurerm_network_security_group.this itself would sometimes fail the
# SAME apply with:
#   Error: Provider produced inconsistent result after apply ... Root object
#   was present, but now absent.
# Both are the same underlying cause, not two separate bugs: Azure Resource
# Manager's control plane can return HTTP 201/200 for a create/update (vnet,
# subnet, NSG) before that object has actually finished replicating to every
# ARM shard that a DEPENDENT resource's own create call gets validated
# against -- so a NIC created moments later, referencing a subnet ID that
# genuinely does exist, can still get "not found" back, and a near-
# simultaneous read of the NSG can briefly see a null/absent object. This is
# a well-documented class of ARM eventual-consistency race (not something
# retrying `terraform plan` fixes on its own, and not something a `depends_on`
# alone fixes either, since the implicit dependency via subnet_id already
# forces correct ORDERING -- the problem is ARM's own propagation lag after
# that order is already respected). A short, one-time pause after the
# subnet/NSG/association are created -- before anything that references them
# -- gives ARM's replication time to catch up. See
# infra-deploy-vm.yml's terraform-apply retry loop for the second half of
# this fix (a transient failure here should never require a human to notice
# and manually re-run the workflow).
# -----------------------------------------------------------------------------
resource "time_sleep" "network_propagation" {
  depends_on = [
    azurerm_subnet.this,
    azurerm_network_security_group.this,
    azurerm_subnet_network_security_group_association.this,
  ]
  create_duration = "30s"
}

resource "azurerm_public_ip" "this" {
  name                = "pip-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  allocation_method   = "Static"
  sku                 = "Standard"
  domain_name_label   = local.dns_label
  tags                = local.common_tags
}

resource "azurerm_network_interface" "this" {
  name                = "nic-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.common_tags

  # Explicit, on top of the implicit dependency subnet_id already creates --
  # see time_sleep.network_propagation's comment above for why ordering
  # alone (which the implicit dependency already guaranteed) wasn't
  # sufficient by itself.
  depends_on = [time_sleep.network_propagation]

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.this.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.this.id
  }
}

# -----------------------------------------------------------------------------
# Free HTTPS, via Cloudflare instead of Let's Encrypt
# -----------------------------------------------------------------------------
# custom_domain is what real visitors hit; Cloudflare terminates their TLS
# at its edge (free, automatic, no ACME dance on this VM at all) and
# forwards the request down the Tunnel to Caddy, which presents the
# Cloudflare Origin CA certificate (cloudflare_origin_cert/_key) for that
# inner hop -- see Caddyfile. ssh.<custom_domain> is the second hostname
# on the same Tunnel, used for SSH instead of a separate mesh network.
# -----------------------------------------------------------------------------
locals {
  effective_domain = var.custom_domain

  # Falls back to a real, validly-formatted bcrypt hash of a random,
  # never-recorded password when var.deploy_status_password_hash is left
  # empty (its default) -- same fail-closed behavior cloud-init.yaml used
  # to hardcode directly. Set the DEPLOY_STATUS_PASSWORD_HASH secret to
  # override with a hash of your own choosing (docker run --rm
  # caddy:2-alpine caddy hash-password) -- see variables.tf's comment.
  effective_deploy_status_password_hash = var.deploy_status_password_hash != "" ? var.deploy_status_password_hash : "$2b$14$1FLbT3EqJ/ebM2oPXK/FEOSjkHp1XCSTe3KyB99xEas.JdktP0JMm"

  # DNS record names are relative to the zone, e.g. custom_domain
  # "assets.example.com" in zone "example.com" needs record name "assets"
  # (and "ssh-assets" for the SSH hostname); an apex custom_domain (equal
  # to cloudflare_zone_name) needs the record name "@" instead.
  effective_dns_record_name = local.effective_domain == var.cloudflare_zone_name ? "@" : trimsuffix(local.effective_domain, ".${var.cloudflare_zone_name}")

  # Uses a hyphen, not a dot ("ssh-assets.example.com", not
  # "ssh.assets.example.com") to keep the SSH hostname to a single DNS
  # label under the zone apex. Cloudflare's default Universal SSL
  # certificate is a single-level wildcard ("*.example.com") and doesn't
  # cover a second label, so a dot-separated hostname fails the TLS
  # handshake before the Tunnel is ever reached. Apex custom_domain
  # (record name "@") still gets the simple "ssh" record name.
  effective_ssh_dns_record_name = local.effective_dns_record_name == "@" ? "ssh" : "ssh-${local.effective_dns_record_name}"
  effective_ssh_domain          = "${local.effective_ssh_dns_record_name}.${var.cloudflare_zone_name}"

  # Azure limits VM OS customData to 64 KiB after Base64 decoding. This
  # cloud-init payload embeds the Compose file, Caddy configuration, deploy
  # status assets, certificates, and secrets, so the rendered YAML is larger
  # than that limit. Gzip it before Base64 encoding: cloud-init automatically
  # detects gzip-compressed user-data and decompresses it before processing.
  # Keeping the rendered payload in a local also lets the VM resource below
  # enforce Azure's 87,380-character Base64 ceiling before ARM is called.
  rendered_vm_cloud_init = templatefile("${path.module}/cloud-init.yaml", {
    docker_compose_vm_yml                 = file("${path.module}/../docker-compose.vm.yml")
    caddyfile                             = file("${path.module}/../Caddyfile")
    caddy_weights_conf                    = file("${path.module}/../caddy/weights.conf")
    deploy_status_index_html              = file("${path.module}/../scripts/deploy-status/index.html")
    deploy_status_seed_json               = file("${path.module}/../scripts/deploy-status/status.json")
    admin_username                        = var.admin_username
    domain                                = local.effective_domain
    cloudflare_tunnel_token               = cloudflare_zero_trust_tunnel_cloudflared.this.tunnel_token
    cloudflare_origin_cert                = var.cloudflare_origin_cert
    cloudflare_origin_cert_key            = var.cloudflare_origin_cert_key
    dockerhub_backend_image               = var.dockerhub_backend_image
    dockerhub_frontend_image              = var.dockerhub_frontend_image
    frontend_build_target                 = var.frontend_build_target
    initial_image_tag                     = var.initial_image_tag
    dockerhub_username                    = var.dockerhub_username
    dockerhub_token                       = var.dockerhub_token
    postgres_user                         = var.postgres_user
    postgres_password                     = var.postgres_password
    postgres_user_urlencoded              = urlencode(var.postgres_user)
    postgres_password_urlencoded          = urlencode(var.postgres_password)
    postgres_db                           = var.postgres_db
    jwt_secret_key                        = var.jwt_secret_key
    errorbeacon_api_key                   = var.errorbeacon_api_key
    errorbeacon_ingest_api_key             = var.errorbeacon_ingest_api_key
    errorbeacon_admin_api_key              = var.errorbeacon_admin_api_key
    errorbeacon_telegram_bot_token        = var.errorbeacon_telegram_bot_token
    errorbeacon_telegram_chat_id          = var.errorbeacon_telegram_chat_id
    errorbeacon_telegram_thread_id        = var.errorbeacon_telegram_thread_id
    errorbeacon_gemini_api_key            = var.errorbeacon_gemini_api_key
    errorbeacon_groq_api_key              = var.errorbeacon_groq_api_key
    errorbeacon_groq_model                = var.errorbeacon_groq_model
    errorbeacon_openrouter_api_key        = var.errorbeacon_openrouter_api_key
    errorbeacon_openrouter_model          = var.errorbeacon_openrouter_model
    errorbeacon_image                     = var.errorbeacon_image
    errorbeacon_app                       = var.errorbeacon_app
    root_admin_bootstrap_password         = var.root_admin_bootstrap_password
    deploy_status_user                    = var.deploy_status_user
    deploy_status_password_hash           = local.effective_deploy_status_password_hash
    site_name                             = var.site_name
    enable_api_docs                       = var.enable_api_docs
    notifications_enabled                 = var.notifications_enabled
    smtp_host                             = var.smtp_host
    smtp_port                             = var.smtp_port
    smtp_username                         = var.smtp_username
    smtp_password                         = var.smtp_password
    smtp_from_email                       = var.smtp_from_email
    email_provider                        = var.email_provider
    brevo_api_key                         = var.brevo_api_key
    resend_api_key                        = var.resend_api_key
    admin_notification_emails             = var.admin_notification_emails
    overdue_digest_hours_utc              = var.overdue_digest_hours_utc
    due_soon_digest_hours_utc             = var.due_soon_digest_hours_utc
    extension_request_sla_hours           = var.extension_request_sla_hours
    quotation_sla_hours                   = var.quotation_sla_hours
    approval_sla_check_interval_minutes   = var.approval_sla_check_interval_minutes
    approval_sla_escalation_repeat_hours  = var.approval_sla_escalation_repeat_hours
    send_quotation_recipient_emails       = var.send_quotation_recipient_emails
    display_timezone                      = var.display_timezone
    currency_code                         = var.currency_code
    enable_auto_backup                    = var.enable_auto_backup
    backup_gdrive_enabled                 = var.backup_gdrive_enabled
    backup_gdrive_oauth_client_id         = var.backup_gdrive_oauth_client_id
    backup_gdrive_oauth_client_secret     = var.backup_gdrive_oauth_client_secret
    backup_gdrive_oauth_refresh_token     = var.backup_gdrive_oauth_refresh_token
    backup_gdrive_credentials_json        = var.backup_gdrive_credentials_json
    backup_gdrive_folder_id               = var.backup_gdrive_folder_id
    otel_enabled                          = var.otel_enabled
    otel_service_name                     = var.otel_service_name
    otel_exporter_otlp_endpoint           = var.otel_exporter_otlp_endpoint
    otel_exporter_otlp_headers            = var.otel_exporter_otlp_headers
    otel_traces_sample_ratio              = var.otel_traces_sample_ratio
    otel_console_exporter                 = var.otel_console_exporter
    applicationinsights_connection_string = var.applicationinsights_connection_string
  })
  rendered_vm_cloud_init_base64gzip = base64gzip(local.rendered_vm_cloud_init)
}

# -----------------------------------------------------------------------------
# Cloudflare Tunnel -- the substitute for both Tailscale (SSH) and the old
# open-80/443 Caddy/Let's Encrypt path (the app), per the security tradeoffs
# discussed in DEPLOYMENT_VM.md's "Set up Cloudflare Tunnel" section. One
# tunnel, one outbound-only `cloudflared` container (docker-compose.vm.yml),
# two hostnames routed over it:
#   - effective_domain     -> Caddy (443)      -> frontend -> backend
#   - effective_ssh_domain -> this VM's sshd (22), gated by Access below
# -----------------------------------------------------------------------------

# 32 random bytes, base64-encoded, used as the tunnel's own secret (proves
# to Cloudflare's edge that a `cloudflared` claiming this tunnel ID is
# actually authorized to -- distinct from the connector token below, which
# is what actually gets handed to the VM).
resource "random_id" "tunnel_secret" {
  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = var.cloudflare_account_id
  name       = "tun-${local.name_prefix}"
  secret     = random_id.tunnel_secret.b64_std
  config_src = "cloudflare" # ingress rules managed remotely (below), not by a local config.yml on the VM
}

# The long-lived token cloud-init writes into /opt/snipeit/.env as
# CLOUDFLARE_TUNNEL_TOKEN -- this, not the raw secret above, is what
# `cloudflared tunnel run` in docker-compose.vm.yml actually authenticates
# with.
#
# Read directly off cloudflare_zero_trust_tunnel_cloudflared.this below,
# not a separate data source -- on the v4.x provider line this repo pins,
# the token is a computed, sensitive attribute on the resource itself
# (tunnel_token in schema_cloudflare_tunnel.go), not a standalone data
# source (that's a v5-only addition).

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id

  config {
    ingress_rule {
      hostname = local.effective_domain
      service  = "https://caddy:443"
      origin_request {
        origin_server_name = local.effective_domain
        # Caddy presents the Cloudflare Origin CA cert for this name (see
        # Caddyfile) -- cloudflared trusts Cloudflare's own Origin CA
        # automatically, no extra ca_pool needed here.
      }
    }
    ingress_rule {
      hostname = local.effective_ssh_domain
      service  = "ssh://host.docker.internal:22"
      # host.docker.internal resolves to the VM itself from inside the
      # `cloudflared` container (see docker-compose.vm.yml's extra_hosts) --
      # sshd is a normal host-level service, not a container.
    }
    ingress_rule {
      service = "http_status:404" # required catch-all; anything not matched above is refused
    }
  }
}

resource "cloudflare_record" "app" {
  zone_id = var.cloudflare_zone_id
  name    = local.effective_dns_record_name
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  proxied = true # MUST be true -- this is what makes Cloudflare terminate TLS and proxy over the Tunnel instead of just publishing a DNS record
  ttl     = 1    # "Automatic", only valid when proxied = true
}

resource "cloudflare_record" "ssh" {
  zone_id = var.cloudflare_zone_id
  name    = local.effective_ssh_dns_record_name
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

# -----------------------------------------------------------------------------
# Cloudflare Access -- gates ssh.<custom_domain>. Without this, anyone who
# guesses the SSH hostname could still attempt a connection (OpenSSH's own
# key auth would still stop them, but Access adds a second, independent
# identity check in front of it -- the "value security" ask this whole
# substitution is in service of). Two ways in, both covered:
#   - humans:     browser SSO login, see ssh_access_allowed_emails
#   - CI/CD:      the service token below (deploy-azure-vm.yml, sync-secrets-vm.yml)
# -----------------------------------------------------------------------------
resource "cloudflare_zero_trust_access_application" "ssh" {
  account_id                = var.cloudflare_account_id
  name                      = "${local.name_prefix}-ssh"
  domain                    = local.effective_ssh_domain
  type                      = "ssh"
  session_duration          = "24h"
  auto_redirect_to_identity = false
}

resource "cloudflare_zero_trust_access_policy" "ssh_humans" {
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.ssh.id
  name           = "allow-listed-admins"
  precedence     = 1
  decision       = "allow"

  include {
    email = var.ssh_access_allowed_emails
  }
}

resource "cloudflare_zero_trust_access_service_token" "ci" {
  account_id = var.cloudflare_account_id
  name       = "${local.name_prefix}-ci-deploy"
}

resource "cloudflare_zero_trust_access_policy" "ssh_ci" {
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.ssh.id
  name           = "allow-ci-service-token"
  precedence     = 2
  decision       = "non_identity" # service tokens authenticate the calling SYSTEM, not a human identity

  include {
    service_token = [cloudflare_zero_trust_access_service_token.ci.id]
  }
}

# -----------------------------------------------------------------------------
# Separate managed data disk -- Docker's data-root (all images, containers,
# and the bind-mounted volumes docker-compose.vm.yml uses under
# /mnt/docker-data/volumes/...) lives here, not on the OS disk. Two
# concrete benefits: (1) you can grow just this disk later without
# touching the OS disk at all (see DEPLOYMENT_VM.md's resize section), and
# (2) the snapshot policy below can back up exactly the data that matters
# without also snapshotting the OS disk on every run.
# -----------------------------------------------------------------------------
resource "azurerm_managed_disk" "data" {
  name                 = "disk-${local.name_prefix}-data"
  resource_group_name  = azurerm_resource_group.this.name
  location             = azurerm_resource_group.this.location
  storage_account_type = "StandardSSD_LRS"
  create_option        = "Empty"
  disk_size_gb         = var.data_disk_size_gb
  tags                 = local.common_tags
}

resource "azurerm_virtual_machine_data_disk_attachment" "data" {
  managed_disk_id    = azurerm_managed_disk.data.id
  virtual_machine_id = azurerm_linux_virtual_machine.this.id
  lun                = "0"
  caching            = "ReadWrite"
}

# -----------------------------------------------------------------------------
# The VM itself
# -----------------------------------------------------------------------------
resource "azurerm_linux_virtual_machine" "this" {
  name                = "vm-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  size                = var.vm_size
  admin_username      = var.admin_username
  network_interface_ids = [
    azurerm_network_interface.this.id,
  ]
  tags = local.common_tags

  # Password auth is never enabled -- SSH key only.
  disable_password_authentication = true
  admin_ssh_key {
    username   = var.admin_username
    public_key = var.ssh_public_key
  }

  os_disk {
    name                 = "osdisk-${local.name_prefix}"
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = var.os_disk_size_gb
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  # Azure applies OS security patches automatically on its own schedule
  # instead of you having to SSH in and `apt upgrade` -- reboots only if a
  # patch requires one, and only during Azure's maintenance window.
  patch_mode                                             = "AutomaticByPlatform"
  patch_assessment_mode                                  = "AutomaticByPlatform"
  bypass_platform_safety_checks_on_user_schedule_enabled = false

  # cloud-init: installs Docker, formats/mounts the data disk, writes
  # /opt/snipeit/{.env,docker-compose.vm.yml,Caddyfile}, and brings the
  # whole stack up on FIRST boot only -- every deploy after that is
  # deploy-azure-vm.yml SSHing in directly (see that workflow and
  # DEPLOYMENT_VM.md step 9), not a re-run of this file.
  # Azure's OSProfile customData limit is 64 KiB of decoded content
  # (87,380 Base64 characters). The rendered cloud-init is larger than that
  # when the embedded Compose/Caddy/status files are included, so send it as
  # gzip-compressed Base64. cloud-init detects the gzip payload and
  # transparently decompresses it before processing #cloud-config.
  custom_data = local.rendered_vm_cloud_init_base64gzip

  lifecycle {
    precondition {
      condition     = length(local.rendered_vm_cloud_init_base64gzip) <= 87380
      error_message = "Rendered VM cloud-init custom_data is ${nonsensitive(length(local.rendered_vm_cloud_init_base64gzip))} Base64 characters; Azure allows at most 87,380. Reduce the embedded first-boot payload before deploying."
    }

    ignore_changes = [
      # cloud-init's custom_data only runs on FIRST boot -- Azure won't
      # even let you change it on a running VM without a rebuild/reset.
      # Ignoring it here stops `terraform plan` from showing a perpetual
      # "will replace VM" diff every time a later deploy changes
      # IMAGE_TAG on the running VM directly (over SSH, via
      # deploy-azure-vm.yml) without going through Terraform at all --
      # that drift is EXPECTED, not something to reconcile back.
      custom_data,
    ]
  }
}

# -----------------------------------------------------------------------------
# Optional: daily snapshot of the data disk (Postgres/Redis/backup/export
# data) via Azure Backup. Independent of, and in addition to, the app's own
# `ENABLE_AUTO_BACKUP` pg_dump job (see variables.tf) -- this covers the
# WHOLE disk (including Redis and any export files), and is the fast path
# to recover if the VM itself is ever lost or corrupted, not just the
# database.
# -----------------------------------------------------------------------------
resource "azurerm_recovery_services_vault" "this" {
  count               = var.enable_data_disk_snapshots ? 1 : 0
  name                = "rsv-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Standard"
  # Azure now makes soft delete mandatory on new Recovery Services Vaults --
  # trying to disable it (soft_delete_enabled = false, the old default here)
  # fails the vault's create/update call with
  # BMSUserErrorDisablingSoftDeleteStateNotAllowed. Leave it enabled (the
  # provider default -- explicit here just to document why).
  soft_delete_enabled = true
  tags                = local.common_tags
}

resource "azurerm_backup_policy_vm" "this" {
  count               = var.enable_data_disk_snapshots ? 1 : 0
  name                = "backup-policy-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  recovery_vault_name = azurerm_recovery_services_vault.this[0].name

  backup {
    frequency = "Daily"
    time      = "23:00"
  }

  retention_daily {
    count = var.snapshot_retention_days
  }
}

resource "azurerm_backup_protected_vm" "this" {
  count               = var.enable_data_disk_snapshots ? 1 : 0
  resource_group_name = azurerm_resource_group.this.name
  recovery_vault_name = azurerm_recovery_services_vault.this[0].name
  source_vm_id        = azurerm_linux_virtual_machine.this.id
  backup_policy_id    = azurerm_backup_policy_vm.this[0].id
}
