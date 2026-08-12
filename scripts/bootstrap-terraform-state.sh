#!/usr/bin/env bash
set -euo pipefail

# Idempotently creates/reuses the Azure Blob Storage backend used by infra-vm/.
# This deliberately lives OUTSIDE the VM resource group so terraform destroy
# can never delete the state it needs in order to destroy the VM stack.
#
# Required environment:
#   ARM_CLIENT_ID, ARM_SUBSCRIPTION_ID, ARM_TENANT_ID
# Optional:
#   AZURE_LOCATION (default eastus)
#   TF_STATE_RESOURCE_GROUP / TF_STATE_STORAGE_ACCOUNT / TF_STATE_CONTAINER
#
# Discovery rules:
#   1. Reuse an explicitly supplied TF_STATE_STORAGE_ACCOUNT when present.
#   2. Otherwise reuse the current deterministic name
#      snipeittfstate + 10-char subscription hash.
#   3. Otherwise reuse the known legacy account name snipeittfstate01.
#   4. Otherwise reuse an existing tagged/prefix-matching Terraform state
#      account in the subscription.
#   5. Only when no existing account is found is a new account created.
#
# The backend key is NOT recreated or migrated here. terraform init receives
# the requested environment key (prod.tfstate or vm-staging.tfstate), so an
# existing blob is opened in place.

SUBSCRIPTION_ID="${ARM_SUBSCRIPTION_ID:?ARM_SUBSCRIPTION_ID is required}"
CLIENT_ID="${ARM_CLIENT_ID:?ARM_CLIENT_ID is required}"
LOCATION="${AZURE_LOCATION:-eastus}"
DEFAULT_STATE_RG="rg-snipeit-tfstate"
STATE_RG="${TF_STATE_RESOURCE_GROUP:-$DEFAULT_STATE_RG}"
STATE_CONTAINER="${TF_STATE_CONTAINER:-vm-state}"

# Azure Storage Account names are 3-24 chars, lowercase letters/numbers only.
# "snipeittfstate" is 14 chars, so the suffix MUST be 10 chars (24 total).
SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID" | sha256sum | cut -c1-10)"
DEFAULT_STATE_ACCOUNT="snipeittfstate${SUFFIX}"
LEGACY_STATE_ACCOUNT="snipeittfstate01"
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
    STATE_RG="$(printf '%s\n' "$ACCOUNT_INFO" | awk '{print $2}')"
    echo "Reusing explicitly configured Terraform state account '$STATE_ACCOUNT' in resource group '$STATE_RG'." >&2
  else
    STATE_ACCOUNT="$STATE_ACCOUNT_OVERRIDE"
    echo "Configured Terraform state account '$STATE_ACCOUNT' does not exist; it will be created in '$STATE_RG'." >&2
  fi
else
  for candidate in "$DEFAULT_STATE_ACCOUNT" "$LEGACY_STATE_ACCOUNT"; do
    ACCOUNT_INFO="$(find_storage_account "$candidate")"
    if [[ -n "$ACCOUNT_INFO" ]]; then
      STATE_ACCOUNT="$(printf '%s\n' "$ACCOUNT_INFO" | awk '{print $1}')"
      STATE_RG="$(printf '%s\n' "$ACCOUNT_INFO" | awk '{print $2}')"
      echo "Reusing existing Terraform state account '$STATE_ACCOUNT' in resource group '$STATE_RG'." >&2
      break
    fi
  done

  # If neither known name exists, look for an existing state account created
  # by an earlier bootstrap. Prefer the explicit Terraform-state tag; fall
  # back to the snipeittfstate prefix for accounts created before tagging was
  # standardized.
  if [[ -z "$ACCOUNT_INFO" ]]; then
    TAGGED_ACCOUNT_INFO="$(az storage account list \
      --subscription "$SUBSCRIPTION_ID" \
      --query "[?tags.purpose=='terraform-state' && tags.application=='snipeit-lite'] | [].{name:name,resourceGroup:resourceGroup}" \
      -o tsv 2>/dev/null || true)"

    PREFIX_ACCOUNT_INFO="$(az storage account list \
      --subscription "$SUBSCRIPTION_ID" \
      --query "[?starts_with(name, 'snipeittfstate')] | [].{name:name,resourceGroup:resourceGroup}" \
      -o tsv 2>/dev/null || true)"

    CANDIDATES="$TAGGED_ACCOUNT_INFO"
    if [[ -z "$CANDIDATES" ]]; then
      CANDIDATES="$PREFIX_ACCOUNT_INFO"
    fi

    if [[ -n "$CANDIDATES" ]]; then
      mapfile -t candidate_lines < <(printf '%s\n' "$CANDIDATES" | sed '/^[[:space:]]*$/d')
      if [[ "${#candidate_lines[@]}" -gt 1 ]]; then
        # Prefer one already in the expected state RG. If still ambiguous,
        # fail rather than risk attaching Terraform to the wrong state.
        MATCH=""
        for line in "${candidate_lines[@]}"; do
          if [[ "$(printf '%s\n' "$line" | awk '{print $2}')" == "$STATE_RG" ]]; then
            if [[ -n "$MATCH" ]]; then
              MATCH=""
              break
            fi
            MATCH="$line"
          fi
        done
        if [[ -n "$MATCH" ]]; then
          CANDIDATES="$MATCH"
        else
          echo "::error::Multiple possible Terraform state storage accounts were found. Set TF_STATE_STORAGE_ACCOUNT explicitly to the account containing prod.tfstate/vm-staging.tfstate." >&2
          printf '%s\n' "${candidate_lines[@]}" >&2
          exit 1
        fi
      fi

      STATE_ACCOUNT="$(printf '%s\n' "$CANDIDATES" | head -n1 | awk '{print $1}')"
      STATE_RG="$(printf '%s\n' "$CANDIDATES" | head -n1 | awk '{print $2}')"
      echo "Reusing discovered Terraform state account '$STATE_ACCOUNT' in resource group '$STATE_RG'." >&2
    else
      STATE_ACCOUNT="$DEFAULT_STATE_ACCOUNT"
      echo "No existing Terraform state account found; new account '$STATE_ACCOUNT' will be created." >&2
    fi
  fi
fi

validate_storage_account_name "$STATE_ACCOUNT"

# ---------------------------------------------------------------------------
# Resource-group lifecycle: existing -> reuse; missing -> create.
# ---------------------------------------------------------------------------
if az group show --name "$STATE_RG" --subscription "$SUBSCRIPTION_ID" >/dev/null 2>&1; then
  echo "Reusing existing Terraform state resource group '$STATE_RG'." >&2
else
  echo "Creating Terraform state resource group '$STATE_RG'." >&2
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

  echo "Creating storage account '$STATE_ACCOUNT'." >&2
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

STATE_ID="$(az storage account show \
  --name "$STATE_ACCOUNT" \
  --resource-group "$STATE_RG" \
  --subscription "$SUBSCRIPTION_ID" \
  --query id -o tsv)"

# ---------------------------------------------------------------------------
# RBAC: idempotently ensure the GitHub OIDC service principal can read/write
# blobs. Query Azure Resource Manager directly because `az role assignment`
# can return `MissingSubscription` in some tenants even when the ARM
# Authorization API is healthy. An assignment inherited from the subscription
# or state resource group is sufficient; only create one when no effective
# assignment exists.
# ---------------------------------------------------------------------------
OBJECT_ID="$(az ad sp show --id "$CLIENT_ID" --query id -o tsv)"
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
    echo "Storage Blob Data Contributor is already assigned at '$scope' and is effective for '$CLIENT_ID'." >&2
    break
  fi
done

if [[ "$HAS_BLOB_ROLE" == "0" ]]; then
  echo "No effective Storage Blob Data Contributor role found for '$CLIENT_ID'; attempting to grant it on '$STATE_ID'..." >&2

  # Deterministic UUID: rerunning bootstrap for the same account/SP/role
  # targets the same role-assignment resource instead of creating duplicates.
  ROLE_ASSIGNMENT_ID="$(python -c 'import sys,uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, sys.argv[1]))' \
    "${STATE_ID}:${OBJECT_ID}:${STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID}")"

  ROLE_ASSIGNMENT_URL="https://management.azure.com${STATE_ID}/providers/Microsoft.Authorization/roleAssignments/${ROLE_ASSIGNMENT_ID}?api-version=2022-04-01"
  ROLE_BODY="{\"properties\":{\"roleDefinitionId\":\"${STORAGE_ROLE_DEFINITION_ID}\",\"principalId\":\"${OBJECT_ID}\",\"principalType\":\"ServicePrincipal\"}}"

  if ! az rest \
      --method put \
      --url "$ROLE_ASSIGNMENT_URL" \
      --body "$ROLE_BODY" \
      >/dev/null; then
    echo "::error::The GitHub OIDC identity '$CLIENT_ID' cannot create Azure role assignments." >&2
    echo "::error::Grant this identity Owner or User Access Administrator on '$STATE_RG' (or the subscription), OR perform the one-time state RBAC setup as an Azure Owner." >&2
    echo "::error::Required permission: Microsoft.Authorization/roleAssignments/write." >&2
    exit 1
  fi

  echo "Storage Blob Data Contributor role granted on '$STATE_ID'." >&2
fi

# ---------------------------------------------------------------------------
# Container lifecycle: existing -> reuse; missing -> create.
# Management-plane ARM is used deliberately, so container creation does not
# depend on Blob data-plane RBAC propagation.
# ---------------------------------------------------------------------------
CONTAINER_ID="${STATE_ID}/blobServices/default/containers/${STATE_CONTAINER}"

if az resource show --ids "$CONTAINER_ID" >/dev/null 2>&1; then
  echo "Reusing existing Terraform state container '$STATE_CONTAINER'." >&2
else
  echo "Creating Terraform state container '$STATE_CONTAINER'." >&2
  az resource create \
    --ids "$CONTAINER_ID" \
    --api-version 2023-01-03 \
    --properties '{}' \
    >/dev/null
fi

# The state blob itself is deliberately never recreated or copied here.
# terraform init below uses the existing environment key, so prod.tfstate
# remains the source of truth when it already exists.
export TF_STATE_RESOURCE_GROUP="$STATE_RG"
export TF_STATE_STORAGE_ACCOUNT="$STATE_ACCOUNT"
export TF_STATE_CONTAINER="$STATE_CONTAINER"

echo "TF_STATE_RESOURCE_GROUP=$STATE_RG"
echo "TF_STATE_STORAGE_ACCOUNT=$STATE_ACCOUNT"
echo "TF_STATE_CONTAINER=$STATE_CONTAINER"
