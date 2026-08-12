
locals {
  name_prefix = "${var.app_base_name}-${var.environment_name}"

  common_tags = merge({
    app         = var.app_base_name
    environment = var.environment_name
    managed-by  = "terraform"
  }, var.tags)

  dns_label = lower(replace("${var.app_base_name}-${var.environment_name}-${random_string.suffix.result}", "_", "-"))
}

resource "random_string" "suffix" {
  length  = 4
  special = false
  upper   = false
  numeric = true
  lower   = true
}

resource "azurerm_resource_group" "this" {
  name     = "rg-${local.name_prefix}-vm"
  location = var.location
  tags     = local.common_tags
}

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
  }
}

resource "azurerm_subnet_network_security_group_association" "this" {
  subnet_id                 = azurerm_subnet.this.id
  network_security_group_id = azurerm_network_security_group.this.id
}

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

  depends_on = [time_sleep.network_propagation]

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.this.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.this.id
  }
}

locals {
  effective_domain = var.custom_domain

  effective_deploy_status_password_hash = var.deploy_status_password_hash != "" ? var.deploy_status_password_hash : "$2b$14$1FLbT3EqJ/ebM2oPXK/FEOSjkHp1XCSTe3KyB99xEas.JdktP0JMm"

  effective_dns_record_name = local.effective_domain == var.cloudflare_zone_name ? "@" : trimsuffix(local.effective_domain, ".${var.cloudflare_zone_name}")

  effective_ssh_dns_record_name = local.effective_dns_record_name == "@" ? "ssh" : "ssh-${local.effective_dns_record_name}"
  effective_ssh_domain          = "${local.effective_ssh_dns_record_name}.${var.cloudflare_zone_name}"
}

resource "random_id" "tunnel_secret" {
  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = var.cloudflare_account_id
  name       = "tun-${local.name_prefix}"
  secret     = random_id.tunnel_secret.b64_std
  config_src = "cloudflare" # ingress rules managed remotely (below), not by a local config.yml on the VM
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id

  config {
    ingress_rule {
      hostname = local.effective_domain
      service  = "https://caddy:443"
      origin_request {
        origin_server_name = local.effective_domain
      }
    }
    ingress_rule {
      hostname = local.effective_ssh_domain
      service  = "ssh://host.docker.internal:22"
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

  patch_mode                                             = "AutomaticByPlatform"
  patch_assessment_mode                                  = "AutomaticByPlatform"
  bypass_platform_safety_checks_on_user_schedule_enabled = false

  custom_data = base64encode(templatefile("${path.module}/cloud-init.yaml", {
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
  }))

  lifecycle {
    ignore_changes = [
      custom_data,
    ]
  }
}

resource "azurerm_recovery_services_vault" "this" {
  count               = var.enable_data_disk_snapshots ? 1 : 0
  name                = "rsv-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Standard"
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
