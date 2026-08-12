
output "resource_group_name" {
  description = "Resource group containing every resource this stack created."
  value       = azurerm_resource_group.this.name
}

output "vm_name" {
  value = azurerm_linux_virtual_machine.this.name
}

output "public_ip_address" {
  description = "Static public IP of the VM. Not used for normal traffic anymore -- both the app and SSH go through the Cloudflare Tunnel instead (see app_url/ssh_command below) -- kept here purely for the break-glass path (ssh_command_break_glass) and for reference in Azure's own tooling."
  value       = azurerm_public_ip.this.ip_address
}

output "azure_fqdn" {
  description = "Azure-issued FQDN for the public IP (<label>.<region>.cloudapp.azure.com). Nothing routes here in normal operation (see public_ip_address above); kept only as a break-glass reference."
  value       = azurerm_public_ip.this.fqdn
}

output "app_domain" {
  description = "The domain Cloudflare terminates TLS for and proxies to Caddy -- your custom_domain."
  value       = local.effective_domain
}

output "app_url" {
  description = "The URL to open in a browser once deploy-azure-vm.yml's first run finishes."
  value       = "https://${local.effective_domain}"
}

output "ssh_hostname" {
  description = "The hostname Cloudflare Access gates SSH behind -- this is what ssh_command below connects to, proxied through the Tunnel rather than resolving to a real IP of its own."
  value       = local.effective_ssh_domain
}

output "ssh_command" {
  description = "SSH command to use once `cloudflared` is installed locally (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) and your ~/.ssh/config has the ProxyCommand entry from DEPLOYMENT_VM.md's \"Set up Cloudflare Tunnel\" section. First connection opens a browser for Access SSO login (must be one of ssh_access_allowed_emails); cached for session_duration after that. Works with ZERO inbound ports open on the NSG."
  value       = "ssh -i ./snipeit_vm_deploy_key ${var.admin_username}@${local.effective_ssh_domain}"
}

output "ssh_command_break_glass" {
  description = "Direct SSH over the public IP -- only works if ssh_allowed_source_ips is currently non-empty (it's empty, meaning NO direct path in, by default). Intended purely as a documented fallback if Cloudflare's network is ever unreachable from where you are -- see DEPLOYMENT_VM.md's Troubleshooting section."
  value       = "ssh -i ./snipeit_vm_deploy_key ${var.admin_username}@${azurerm_public_ip.this.ip_address}"
}

output "cloudflare_ci_service_token_id" {
  description = "Client ID half of the Access service token deploy-azure-vm.yml/sync-secrets-vm.yml authenticate with -- set as the CF_ACCESS_CLIENT_ID GitHub secret."
  value       = cloudflare_zero_trust_access_service_token.ci.client_id
}

output "cloudflare_ci_service_token_secret" {
  description = "Client secret half of the CI Access service token -- set as the CF_ACCESS_CLIENT_SECRET GitHub secret. Only ever shown once by Cloudflare at creation and here in `terraform output`; if lost, taint and recreate cloudflare_zero_trust_access_service_token.ci rather than trying to retrieve it again."
  value       = cloudflare_zero_trust_access_service_token.ci.client_secret
  sensitive   = true
}

output "cloudflare_tunnel_token" {
  description = "The current, live token for this Tunnel -- what CLOUDFLARE_TUNNEL_TOKEN in /opt/snipeit/.env on the VM must match for the `cloudflared` container to authenticate. cloud-init writes this in ONLY ONCE, at VM creation time -- if the Tunnel resource is ever recreated by a later `terraform apply` (e.g. a config change that forces replacement), the VM's .env keeps the OLD, now-invalid token and cloudflared silently fails to connect (Tunnel shows 'Inactive' with zero connectors in the Zero Trust dashboard, and every ssh/scp through it fails identically, generically, e.g. `remote error: tls: handshake failure`, however far downstream the real cause is). Recover by pulling this value (`terraform output -raw cloudflare_tunnel_token`) and setting it by hand over the Azure Serial Console (SSH itself is unusable while the Tunnel is down): update the `CLOUDFLARE_TUNNEL_TOKEN=` line in /opt/snipeit/.env, then `docker compose -f /opt/snipeit/docker-compose.vm.yml up -d cloudflared` to restart it with the corrected token. See DEPLOYMENT_VM.md's Troubleshooting section."
  value       = cloudflare_zero_trust_tunnel_cloudflared.this.tunnel_token
  sensitive   = true
}

output "data_disk_id" {
  description = "Resource ID of the managed data disk -- referenced if you ever need to detach/reattach it manually, or take an ad-hoc snapshot outside the automatic policy."
  value       = azurerm_managed_disk.data.id
}
