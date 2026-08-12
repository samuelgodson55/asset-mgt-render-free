#!/usr/bin/env bash
set -euo pipefail

# Idempotently creates the Azure Blob Storage backend used by infra-vm/.
# This deliberately lives OUTSIDE the VM resource group so terraform destroy
# can never delete the state it needs in order to destroy the VM stack.
#
# The GitHub Actions workflow calls this before `terraform init`, so there is
# no one-time Portal/CLI storage-account setup and no TF_STATE_* GitHub vars.
# The same state account is shared by vm-staging/prod; their state files are
# separate keys inside the same container.
#
# Required environment:
#   ARM_CLIENT_ID, ARM_SUBSCRIPTION_ID, ARM_TENANT_ID
# Optional:
#   AZURE_LOCATION (default eastus)
#   TF_STATE_RESOURCE_GROUP / TF_STATE_STORAGE_ACCOUNT / TF_STATE_CONTAINER
#     override the deterministic defaults if desired.

SUBSCRIPTION_ID="${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID is required}"
CLIENT_ID="${ARM_CLIENT_ID:?ARM_CLIENT_ID is required}"
LOCATION="${AZURE_LOCATION:-eastus}"
STATE_RG="${TF_STATE_RESOURCE_GROUP:-rg-snipeit-tfstate}"
STATE_CONTAINER="${TF_STATE_CONTAINER:-vm-state}"

# Storage account names are global. A short subscription-derived suffix makes
# the default deterministic for the subscription while staying within Azure's
# 24-character limit.
SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID" | sha256sum | cut -c1-12)"
STATE_ACCOUNT="${TF_STATE_STORAGE_ACCOUNT:-snipeittfstate${SUFFIX}}"

case "$STATE_ACCOUNT" in
  *[!a-z0-9]*|?*) : ;;
esac
if [[ ${#STATE_ACCOUNT} -lt 3 || ${#STATE_ACCOUNT} -gt 24 ]]; then
  echo "Invalid Terraform state storage account name: $STATE_ACCOUNT" >&2
  exit 1
fi

export TF_STATE_RESOURCE_GROUP="$STATE_RG"
export TF_STATE_STORAGE_ACCOUNT="$STATE_ACCOUNT"
export TF_STATE_CONTAINER="$STATE_CONTAINER"

az account set --subscription "$SUBSCRIPTION_ID"

# Register the VM path's Azure resource providers. Registration is harmless if
# a provider is already registered and keeps a fresh subscription bootstrap
# fully automated.
for provider in \
  Microsoft.Resources \
  Microsoft.Compute \
  Microsoft.Network \
  Microsoft.Storage \
  Microsoft.RecoveryServices
  do
    az provider register --namespace "$provider" --wait >/dev/null
  done

az group create \
  --name "$STATE_RG" \
  --location "$LOCATION" \
  --tags managed-by=github-actions purpose=terraform-state application=snipeit-lite \
  >/dev/null

if ! az storage account show --name "$STATE_ACCOUNT" --resource-group "$STATE_RG" >/dev/null 2>&1; then
  az storage account create \
    --name "$STATE_ACCOUNT" \
    --resource-group "$STATE_RG" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --https-only true \
    --allow-blob-public-access false \
    --tags managed-by=github-actions purpose=terraform-state application=snipeit-lite \
    >/dev/null
fi

STATE_ID="$(az storage account show --name "$STATE_ACCOUNT" --resource-group "$STATE_RG" --query id -o tsv)"

# Terraform's azurerm backend uses Azure AD/data-plane auth. Contributor is a
# management-plane role and does NOT grant Blob data access, so explicitly add
# Storage Blob Data Contributor to the federated CI service principal.
OBJECT_ID="$(az ad sp show --id "$CLIENT_ID" --query id -o tsv)"
if ! az role assignment list \
  --assignee-object-id "$OBJECT_ID" \
  --scope "$STATE_ID" \
  --role "Storage Blob Data Contributor" \
  --query '[0].id' -o tsv | grep -q .; then
  az role assignment create \
    --assignee-object-id "$OBJECT_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Contributor" \
    --scope "$STATE_ID" \
    >/dev/null
fi

# Container creation is management-plane ARM, so it does not depend on the
# data-plane role assignment having propagated yet.
CONTAINER_ID="${STATE_ID}/blobServices/default/containers/${STATE_CONTAINER}"
az resource show --ids "$CONTAINER_ID" >/dev/null 2>&1 || \
az resource create \
  --ids "$CONTAINER_ID" \
  --api-version 2023-01-03 \
  --properties '{}' \
  >/dev/null

echo "TF_STATE_RESOURCE_GROUP=$STATE_RG"
echo "TF_STATE_STORAGE_ACCOUNT=$STATE_ACCOUNT"
echo "TF_STATE_CONTAINER=$STATE_CONTAINER"
