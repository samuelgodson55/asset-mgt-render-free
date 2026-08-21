#!/usr/bin/env bash
# scripts/emergency-tunnel-repair-run-command.sh
# -----------------------------------------------------------------------------
# Emergency, no-CI-required version of what .github/workflows/repair-tunnel-token-vm.yml
# automates. Use this when:
#   - `docker ps` on the VM (via Azure Portal -> VM -> Run command -> RunShellScript,
#     or `az vm run-command invoke`) shows NO `cloudflared` container running
#   - deploy/sync workflows fail their first ssh/scp step with something like
#     "websocket: bad handshake" / "tls: handshake failure" /
#     "Connection closed by UNKNOWN port 65535"
#   - the Cloudflare Zero Trust dashboard (Networks -> Tunnels -> this tunnel)
#     shows Status: Inactive with an empty Connectors table
#
# This is almost always a STALE CLOUDFLARE_TUNNEL_TOKEN in /opt/snipeit/.env --
# cloud-init.yaml only ever writes that token ONCE, at first boot. It is never
# refreshed automatically after that. If the Tunnel resource in Cloudflare was
# ever recreated (rotating its token) after the VM's first boot, the VM keeps
# trying to authenticate with the OLD token forever, and `cloudflared` just
# fails to connect -- which takes SSH access down with it, since SSH rides
# over the same Tunnel (see docker-compose.vm.yml's `cloudflared` service and
# infra-vm/main.tf's "Cloudflare Tunnel" section).
#
# IMPORTANT: editing infra-vm/cloud-init.yaml does NOT fix an already-running
# VM. cloud-init only runs on a VM's literal first boot, and main.tf's
# azurerm_linux_virtual_machine resource has `lifecycle { ignore_changes =
# [custom_data] }` -- so a `terraform apply` after editing cloud-init.yaml is
# a no-op against an existing VM. If your VM is already up, the only way to
# fix a stale token on it is to push the new value directly, which is what
# this script does.
#
# WHAT THIS DOES: reads the CURRENT tunnel token straight out of your local
# Terraform state (the same source of truth `terraform output` uses) and
# pushes it to the VM via `az vm run-command invoke`, which runs over Azure's
# VM Agent/extension control-plane channel -- NOT SSH, NOT the Tunnel -- so it
# works even while the Tunnel itself is completely down. It then restarts
# just the `cloudflared` container.
#
# USAGE (run from your own machine, inside infra-vm/, with `az login` and
# `terraform init` already done against the right environment's state file):
#   cd infra-vm
#   ../scripts/emergency-tunnel-repair-run-command.sh <resource-group> <vm-name>
#
# If you don't have `resource-group`/`vm-name` handy, this script will try to
# read them from `terraform output` too -- just run it with no arguments.
# -----------------------------------------------------------------------------
set -euo pipefail

RESOURCE_GROUP="${1:-}"
VM_NAME="${2:-}"

if [[ -z "$RESOURCE_GROUP" || -z "$VM_NAME" ]]; then
  echo "No resource group / VM name passed -- reading them from terraform output instead..."
  RESOURCE_GROUP="$(terraform output -raw resource_group_name)"
  VM_NAME="$(terraform output -raw vm_name)"
fi

echo "Reading current live tunnel token from Terraform state..."
NEW_TOKEN="$(terraform output -raw cloudflare_tunnel_token)"

if [[ -z "$NEW_TOKEN" || -z "$RESOURCE_GROUP" || -z "$VM_NAME" ]]; then
  echo "One or more required values came back empty -- is this environment's .tfstate actually applied?" >&2
  exit 1
fi

echo "Resource group: $RESOURCE_GROUP"
echo "VM name:        $VM_NAME"
echo "(token masked)"
echo

cat > /tmp/repair-tunnel-token.sh <<'SCRIPT'
#!/bin/bash
set -euo pipefail
NEW_TOKEN="$1"
cd /opt/snipeit
if [ ! -f .env ]; then
  echo "No /opt/snipeit/.env on this VM -- is it actually provisioned?" >&2
  exit 1
fi
cp .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
ls -1t .env.bak.* 2>/dev/null | tail -n +6 | xargs -r rm --
if grep -q '^CLOUDFLARE_TUNNEL_TOKEN=' .env; then
  sed -i "s|^CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=${NEW_TOKEN}|" .env
else
  echo "CLOUDFLARE_TUNNEL_TOKEN=${NEW_TOKEN}" >> .env
fi
docker compose -f docker-compose.vm.yml up -d cloudflared
sleep 5
echo "---- docker ps (confirm cloudflared + caddy are both up) ----"
docker ps
echo "---- cloudflared logs (last 20 lines) ----"
docker compose -f docker-compose.vm.yml logs --tail 20 cloudflared
SCRIPT

echo "Pushing token via az vm run-command invoke (Azure control-plane channel, bypasses SSH/Tunnel entirely)..."
az vm run-command invoke \
  --resource-group "$RESOURCE_GROUP" \
  --name "$VM_NAME" \
  --command-id RunShellScript \
  --scripts @/tmp/repair-tunnel-token.sh \
  --parameters "$NEW_TOKEN" \
  --query 'value[].message' -o tsv

rm -f /tmp/repair-tunnel-token.sh

echo
echo "Done. Confirm in the Cloudflare Zero Trust dashboard (Networks -> Tunnels)"
echo "that Status flips to Active with one connector listed, then retry SSH /"
echo "re-run whichever deploy workflow failed."
