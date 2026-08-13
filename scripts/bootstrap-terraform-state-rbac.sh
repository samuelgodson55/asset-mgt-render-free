#!/usr/bin/env bash
set -euo pipefail

# One-time privileged bootstrap for the Terraform state backend.
#
# Run this as an Azure Owner or User Access Administrator. It grants ONLY
# Storage Blob Data Contributor to the GitHub OIDC service principal. Normal
# GitHub Actions deployments must not have roleAssignments/write.
#
# Required environment:
#   ARM_CLIENT_ID          GitHub OIDC application/client ID
#   ARM_SUBSCRIPTION_ID
# Optional:
#   TF_STATE_RESOURCE_GROUP
#   TF_STATE_STORAGE_ACCOUNT
#
# This script does not grant Owner, User Access Administrator, or Contributor.

SUBSCRIPTION_ID="${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID is required}"
CLIENT_ID="${ARM_CLIENT_ID:?ARM_CLIENT_ID is required}"
STATE_RG="${TF_STATE_RESOURCE_GROUP:-rg-snipeit-tfstate}"
STATE_ACCOUNT_OVERRIDE="${TF_STATE_STORAGE_ACCOUNT:-}"
STATE_CONTAINER="${TF_STATE_CONTAINER:-vm-state}"

az account set --subscription "$SUBSCRIPTION_ID"

if [[ -n "$STATE_ACCOUNT_OVERRIDE" ]]; then
  STATE_ACCOUNT="$STATE_ACCOUNT_OVERRIDE"
else
  SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID" | sha256sum | cut -c1-10)"
  DEFAULT_STATE_ACCOUNT="snipeittfstate${SUFFIX}"
  LEGACY_STATE_ACCOUNT="snipeittfstate01"

  STATE_ACCOUNT=""
  for candidate in "$DEFAULT_STATE_ACCOUNT" "$LEGACY_STATE_ACCOUNT"; do
    if az storage account show --name "$candidate" --subscription "$SUBSCRIPTION_ID" >/dev/null 2>&1; then
      STATE_ACCOUNT="$candidate"
      break
    fi
  done
fi

if [[ -z "$STATE_ACCOUNT" ]]; then
  echo "ERROR: No Terraform state storage account was found." >&2
  echo "Create the state backend first, or provide TF_STATE_STORAGE_ACCOUNT." >&2
  exit 1
fi

ACCOUNT_RESOURCE_GROUP="$(az storage account show \
  --name "$STATE_ACCOUNT" \
  --subscription "$SUBSCRIPTION_ID" \
  --query resourceGroup -o tsv)"

if [[ -z "$ACCOUNT_RESOURCE_GROUP" ]]; then
  echo "ERROR: Unable to determine the resource group for Terraform state account '$STATE_ACCOUNT'." >&2
  exit 1
fi

STATE_RG="$ACCOUNT_RESOURCE_GROUP"
STATE_ID="$(az storage account show \
  --name "$STATE_ACCOUNT" \
  --resource-group "$STATE_RG" \
  --subscription "$SUBSCRIPTION_ID" \
  --query id -o tsv)"

OBJECT_ID="$(az ad sp show --id "$CLIENT_ID" --query id -o tsv)"
if [[ -z "$OBJECT_ID" ]]; then
  echo "ERROR: Unable to resolve service principal '$CLIENT_ID'." >&2
  exit 1
fi

ROLE_ID="ba92f5b4-2d11-453d-a403-e96b0029c9fe"
ROLE_DEFINITION_ID="/subscriptions/${SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/${ROLE_ID}"
ROLE_ASSIGNMENT_ID="$(python -c 'import sys,uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, sys.argv[1]))' \
  "${STATE_ID}:${OBJECT_ID}:${ROLE_ID}")"
ROLE_ASSIGNMENT_URL="https://management.azure.com${STATE_ID}/providers/Microsoft.Authorization/roleAssignments/${ROLE_ASSIGNMENT_ID}?api-version=2022-04-01"
ROLE_BODY="{\"properties\":{\"roleDefinitionId\":\"${ROLE_DEFINITION_ID}\",\"principalId\":\"${OBJECT_ID}\",\"principalType\":\"ServicePrincipal\"}}"

# The Terraform backend needs the blob container itself to exist before
# `terraform init` can list workspaces. Creating the container with the storage
# account key is intentional here: this script is the one-time privileged
# bootstrap, so it can create the data-plane container immediately without
# waiting for the newly-created GitHub OIDC data role to propagate. The key is
# never printed and is not stored anywhere.
ensure_state_container() {
  local account_key
  account_key="$(az storage account keys list     --name "$STATE_ACCOUNT"     --resource-group "$STATE_RG"     --subscription "$SUBSCRIPTION_ID"     --query '[0].value' -o tsv)"

  if [[ -z "$account_key" ]]; then
    echo "ERROR: Unable to retrieve a storage account key for '$STATE_ACCOUNT'." >&2
    exit 1
  fi

  echo "Ensuring Terraform state container '$STATE_CONTAINER' exists..."
  az storage container create     --name "$STATE_CONTAINER"     --account-name "$STATE_ACCOUNT"     --account-key "$account_key"     --fail-on-exist false     >/dev/null
  echo "Terraform state container '$STATE_CONTAINER' is ready."
}

# Check effective assignments at the subscription, resource-group, and storage
# account scopes before creating anything.
for scope in \
  "/subscriptions/${SUBSCRIPTION_ID}" \
  "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${STATE_RG}" \
  "$STATE_ID"
do
  URL="https://management.azure.com${scope}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01"
  COUNT="$(az rest --method get --url "$URL" \
    --query "value[?properties.principalId=='${OBJECT_ID}' && properties.roleDefinitionId=='${ROLE_DEFINITION_ID}'] | length(@)" \
    -o tsv 2>/dev/null || echo 0)"
  if [[ "$COUNT" != "0" ]]; then
    echo "Storage Blob Data Contributor already exists at '$scope'."
    ensure_state_container
    echo "Nothing else to change."
    exit 0
  fi
done

echo "Granting Storage Blob Data Contributor to '$CLIENT_ID' on '$STATE_ACCOUNT'..."
if ! az rest \
    --method put \
    --url "$ROLE_ASSIGNMENT_URL" \
    --body "$ROLE_BODY" \
    >/dev/null; then
  echo "ERROR: The current Azure identity cannot create role assignments." >&2
  echo "Authenticate as an Owner or User Access Administrator and rerun this one-time script." >&2
  exit 1
fi

echo "Storage Blob Data Contributor granted successfully."

# The role assignment may take time to propagate, but the privileged bootstrap
# can create the container immediately with the storage account key. This makes
# the initial bootstrap complete, rather than leaving Terraform init to discover
# a missing container later.
ensure_state_container

echo "Normal GitHub Actions deployments now require no roleAssignments/write permission."
