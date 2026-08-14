#!/usr/bin/env bash
set -euo pipefail

# Idempotently creates/reuses the Azure Blob Storage backend used by infra-vm/.
# This deliberately lives OUTSIDE the VM resource group so terraform destroy
# can never delete the state it needs in order to destroy the VM stack.
#
# Required environment:
#   ARM_CLIENT_ID, ARM_SUBSCRIPTION_ID, ARM_TENANT_ID
# Optional:
#   AZURE_LOCATION (required)
#   ALLOW_RBAC_BOOTSTRAP=true when invoked by the one-time privileged
#   scripts/bootstrap-azure-github.sh. Normal GitHub Actions deployments
#   must leave this unset. Privileged mode grants Storage Blob Data Contributor
#   and creates the initial container using the storage account key.
#   TF_STATE_RESOURCE_GROUP / TF_STATE_STORAGE_ACCOUNT / TF_STATE_CONTAINER
#
# Discovery rules:
#   1. Reuse an explicitly supplied TF_STATE_STORAGE_ACCOUNT when present.
#   2. Otherwise use the deterministic subscription-derived state account name.
#   3. Never attach automatically to an unrelated/legacy state account.
#   4. Only when the expected account is absent is a new account created.
#
# The backend key is NOT recreated or migrated here. terraform init receives
# the requested environment key (prod.tfstate or vm-staging.tfstate), so an
# existing blob is opened in place.

SUBSCRIPTION_ID="${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID is required}"
CLIENT_ID="${ARM_CLIENT_ID:?ARM_CLIENT_ID is required}"
LOCATION="${AZURE_LOCATION:-}"
if [[ -z "$LOCATION" ]]; then
  echo "::error::AZURE_LOCATION is required. Refusing to default Terraform state resources to another region." >&2
  exit 1
fi
ALLOW_RBAC_BOOTSTRAP="${ALLOW_RBAC_BOOTSTRAP:-false}"
DEFAULT_STATE_RG="rg-snipeit-tfstate"
STATE_RG="${TF_STATE_RESOURCE_GROUP:-$DEFAULT_STATE_RG}"
STATE_CONTAINER="${TF_STATE_CONTAINER:-vm-state}"

# Azure Storage Account names are 3-24 chars, lowercase letters/numbers only.
# "snipeittfstate" is 14 chars, so the suffix MUST be 10 chars (24 total).
SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID" | sha256sum | cut -c1-10)"
DEFAULT_STATE_ACCOUNT="snipeittfstate${SUFFIX}"
STATE_ACCOUNT_OVERRIDE="${TF_STATE_STORAGE_ACCOUNT:-}"

validate_storage_account_name() {
  local name="$1"
  if [[ ! "$name" =~ ^[a-z0-9]{3,24}$ ]]; then
    echo "::error::Invalid Terraform state storage account name '$name'. Azure requires 3-24 lowercase letters/numbers only." >&2
    exit 1
  fi
}

validate_storage_account_name "$DEFAULT_STATE_ACCOUNT"
if [[ -n "$STATE_ACCOUNT_OVERRIDE" ]]; then
  validate_storage_account_name "$STATE_ACCOUNT_OVERRIDE"
fi

az account set --subscription "$SUBSCRIPTION_ID"

# Return "<name>\t<resource-group>" for an existing storage account, or empty.
find_storage_account() {
  local name="$1"
  az storage account list \
    --subscription "$SUBSCRIPTION_ID" \
    --query "[?name=='${name}'] | [0].{name:name,resourceGroup:resourceGroup}" \
    -o tsv 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Discover the state account BEFORE creating anything. This is what makes the
# workflow safe for an already-provisioned environment: it does not create a
# second state account just because the deterministic naming formula changed.
# ---------------------------------------------------------------------------
ACCOUNT_INFO=""
if [[ -n "$STATE_ACCOUNT_OVERRIDE" ]]; then
  ACCOUNT_INFO="$(find_storage_account "$STATE_ACCOUNT_OVERRIDE")"
  if [[ -n "$ACCOUNT_INFO" ]]; then
    STATE_ACCOUNT="$(printf '%s\n' "$ACCOUNT_INFO" | awk '{print $1}')"
    EXISTING_STATE_RG="$(printf '%s\n' "$ACCOUNT_INFO" | awk '{print $2}')"
    if [[ "$EXISTING_STATE_RG" != "$STATE_RG" ]]; then
      echo "::error::Configured Terraform state account '$STATE_ACCOUNT' exists in resource group '$EXISTING_STATE_RG', expected '$STATE_RG'. Refusing to silently attach to a different resource group." >&2
      exit 1
    fi
    echo "Reusing explicitly configured Terraform state account '$STATE_ACCOUNT'." >&2
  else
    STATE_ACCOUNT="$STATE_ACCOUNT_OVERRIDE"
    echo "Configured Terraform state account '$STATE_ACCOUNT' does not exist; it will be created in '$STATE_RG'." >&2
  fi
else
  STATE_ACCOUNT="$DEFAULT_STATE_ACCOUNT"
  ACCOUNT_INFO="$(find_storage_account "$STATE_ACCOUNT")"
  if [[ -n "$ACCOUNT_INFO" ]]; then
    EXISTING_STATE_RG="$(printf '%s\n' "$ACCOUNT_INFO" | awk '{print $2}')"
    if [[ "$EXISTING_STATE_RG" != "$STATE_RG" ]]; then
      echo "::error::Deterministic Terraform state account '$STATE_ACCOUNT' already exists in resource group '$EXISTING_STATE_RG', expected '$STATE_RG'. Refusing to use a different state location." >&2
      exit 1
    fi
    echo "Reusing existing Terraform state account '$STATE_ACCOUNT'." >&2
  else
    echo "No existing Terraform state account found; new account '$STATE_ACCOUNT' will be created." >&2
  fi
fi

validate_storage_account_name "$STATE_ACCOUNT"

# ---------------------------------------------------------------------------
# Resource-group lifecycle: existing -> reuse; missing -> create.
# ---------------------------------------------------------------------------
if az group show --name "$STATE_RG" --subscription "$SUBSCRIPTION_ID" >/dev/null 2>&1; then
  RG_LOCATION="$(az group show --name "$STATE_RG" --subscription "$SUBSCRIPTION_ID" --query location -o tsv)"
  if [[ "$RG_LOCATION" != "$LOCATION" ]]; then
    echo "::error::Terraform state resource group '$STATE_RG' is in '$RG_LOCATION', but AZURE_LOCATION is '$LOCATION'. Refusing to continue with a location mismatch." >&2
    exit 1
  fi
  echo "Reusing existing Terraform state resource group '$STATE_RG'." >&2
else
  echo "Creating Terraform state resource group '$STATE_RG' in location '$LOCATION'." >&2
  az group create \
    --name "$STATE_RG" \
    --location "$LOCATION" \
    --tags managed-by=github-actions purpose=terraform-state application=snipeit-lite \
    >/dev/null
fi

# ---------------------------------------------------------------------------
# Storage-account lifecycle: existing -> reuse; missing -> create.
# ---------------------------------------------------------------------------
if az storage account show \
    --name "$STATE_ACCOUNT" \
    --resource-group "$STATE_RG" \
    --subscription "$SUBSCRIPTION_ID" >/dev/null 2>&1; then
  echo "Reusing existing storage account '$STATE_ACCOUNT'." >&2
else
  # A discovered account must exist in its discovered RG; do not accidentally
  # create a duplicate with the same intended backend name elsewhere.
  if [[ -n "$ACCOUNT_INFO" ]]; then
    echo "::error::Storage account '$STATE_ACCOUNT' was discovered but could not be read from resource group '$STATE_RG'." >&2
    exit 1
  fi

  echo "Creating storage account '$STATE_ACCOUNT' in location '$LOCATION'." >&2
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

STORAGE_LOCATION="$(az storage account show \
  --name "$STATE_ACCOUNT" \
  --resource-group "$STATE_RG" \
  --subscription "$SUBSCRIPTION_ID" \
  --query primaryLocation -o tsv)"
if [[ "$STORAGE_LOCATION" != "$LOCATION" ]]; then
  echo "::error::Terraform state storage account '$STATE_ACCOUNT' is in '$STORAGE_LOCATION', but AZURE_LOCATION is '$LOCATION'. Refusing to continue." >&2
  exit 1
fi

STATE_ID="$(az storage account show \
  --name "$STATE_ACCOUNT" \
  --resource-group "$STATE_RG" \
  --subscription "$SUBSCRIPTION_ID" \
  --query id -o tsv)"

# ---------------------------------------------------------------------------
# State RBAC.
#
# The one-time local bootstrap is the only place allowed to create the
# Storage Blob Data Contributor assignment. Normal GitHub Actions runs only
# verify that the assignment already exists.
# ---------------------------------------------------------------------------
OBJECT_ID="$(az ad sp show --id "$CLIENT_ID" --query id -o tsv)"
if [[ -z "$OBJECT_ID" ]]; then
  echo "::error::Unable to resolve the GitHub OIDC service principal '$CLIENT_ID' in Entra ID." >&2
  exit 1
fi

STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID="ba92f5b4-2d11-453d-a403-e96b0029c9fe"
STORAGE_ROLE_DEFINITION_ID="/subscriptions/${SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/${STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID}"

role_assignment_count_at_scope() {
  local scope="$1"
  local url="https://management.azure.com${scope}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01"

  az rest \
    --method get \
    --url "$url" \
    --query "value[?properties.principalId=='${OBJECT_ID}' && properties.roleDefinitionId=='${STORAGE_ROLE_DEFINITION_ID}'] | length(@)" \
    -o tsv 2>/dev/null || echo 0
}

HAS_BLOB_ROLE="0"
for scope in \
  "/subscriptions/${SUBSCRIPTION_ID}" \
  "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${STATE_RG}" \
  "$STATE_ID"
do
  COUNT="$(role_assignment_count_at_scope "$scope")"
  if [[ "$COUNT" != "0" ]]; then
    HAS_BLOB_ROLE="1"
    echo "Storage Blob Data Contributor is already assigned at '$scope'." >&2
    break
  fi
done

ensure_state_container_with_key() {
  local account_key
  account_key="$(az storage account keys list \
    --account-name "$STATE_ACCOUNT" \
    --resource-group "$STATE_RG" \
    --subscription "$SUBSCRIPTION_ID" \
    --query '[0].value' \
    -o tsv)"

  if [[ -z "$account_key" ]]; then
    echo "::error::Unable to retrieve a storage account key for '$STATE_ACCOUNT'." >&2
    exit 1
  fi

  for attempt in 1 2 3 4 5; do
    if az storage container exists \
        --name "$STATE_CONTAINER" \
        --account-name "$STATE_ACCOUNT" \
        --account-key "$account_key" \
        --query exists \
        -o tsv 2>/dev/null | grep -qx true; then
      echo "Terraform state container '$STATE_CONTAINER' is ready." >&2
      return
    fi

    if az storage container create \
        --name "$STATE_CONTAINER" \
        --account-name "$STATE_ACCOUNT" \
        --account-key "$account_key" \
        >/dev/null 2>&1; then
      echo "Terraform state container '$STATE_CONTAINER' is ready." >&2
      return
    fi

    if [[ "$attempt" != "5" ]]; then
      sleep 3
    fi
  done

  echo "::error::Unable to create or access Terraform state container '$STATE_CONTAINER'." >&2
  exit 1
}

if [[ "$ALLOW_RBAC_BOOTSTRAP" == "true" ]]; then
  if [[ "$HAS_BLOB_ROLE" == "0" ]]; then
    ROLE_ASSIGNMENT_ID="$(python -c 'import sys,uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, sys.argv[1]))' \
      "${STATE_ID}:${OBJECT_ID}:${STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID}")"
    ROLE_ASSIGNMENT_URL="https://management.azure.com${STATE_ID}/providers/Microsoft.Authorization/roleAssignments/${ROLE_ASSIGNMENT_ID}?api-version=2022-04-01"
    ROLE_BODY="{\"properties\":{\"roleDefinitionId\":\"${STORAGE_ROLE_DEFINITION_ID}\",\"principalId\":\"${OBJECT_ID}\",\"principalType\":\"ServicePrincipal\"}}"

    echo "Granting Storage Blob Data Contributor on '$STATE_ACCOUNT'..."
    if ! az rest --method put --url "$ROLE_ASSIGNMENT_URL" --body "$ROLE_BODY" >/dev/null; then
      echo "::error::The current Azure identity cannot create role assignments." >&2
      echo "::error::Run the bootstrap while authenticated as an Azure Owner or User Access Administrator." >&2
      exit 1
    fi
    echo "Storage Blob Data Contributor granted successfully." >&2
  else
    echo "Storage Blob Data Contributor already exists; no RBAC change required." >&2
  fi

  # The privileged bootstrap uses the account key for the initial container so
  # it cannot be blocked by RBAC propagation delay.
  ensure_state_container_with_key
else
  if [[ "$HAS_BLOB_ROLE" == "0" ]]; then
    echo "::error::GitHub OIDC identity '$CLIENT_ID' does not have Storage Blob Data Contributor on '$STATE_ACCOUNT'." >&2
    echo "::error::Normal GitHub Actions runs never create Azure role assignments." >&2
    exit 1
  fi

  echo "Ensuring Terraform state container '$STATE_CONTAINER' exists in '$STATE_ACCOUNT'..." >&2
  for attempt in 1 2 3 4 5; do
    if container_exists="$(az storage container exists \
        --name "$STATE_CONTAINER" \
        --account-name "$STATE_ACCOUNT" \
        --auth-mode login \
        --query exists \
        -o tsv 2>/dev/null)"; then
      if [[ "$container_exists" == "true" ]]; then
        echo "Terraform state container '$STATE_CONTAINER' already exists and is ready." >&2
        break
      fi

      if az storage container create \
          --name "$STATE_CONTAINER" \
          --account-name "$STATE_ACCOUNT" \
          --auth-mode login \
          >/dev/null; then
        echo "Terraform state container '$STATE_CONTAINER' is ready." >&2
        break
      fi
    fi

    if [[ "$attempt" == "5" ]]; then
      echo "::error::Unable to create or access Terraform state container '$STATE_CONTAINER' after 5 attempts." >&2
      exit 1
    fi

    echo "State container attempt $attempt/5 failed; waiting for Azure Storage RBAC propagation..." >&2
    sleep 10
  done
fi

# Emit backend settings for the caller. Keep stdout machine-readable; all
# diagnostics above intentionally go to stderr.
printf 'TF_STATE_RESOURCE_GROUP=%s\n' "$STATE_RG"
printf 'TF_STATE_STORAGE_ACCOUNT=%s\n' "$STATE_ACCOUNT"
printf 'TF_STATE_CONTAINER=%s\n' "$STATE_CONTAINER"
