#!/usr/bin/env bash
set -euo pipefail

# One-time, per-environment Azure/GitHub bootstrap.
# Run locally after `az login` and `gh auth login`.
#
# Usage:
#   ./scripts/bootstrap-azure-github.sh production
#   ./scripts/bootstrap-azure-github.sh prod
#
# This configures ONLY the selected GitHub Environment. It does not create
# credentials, secrets, or infrastructure for the other environments.
#
# It performs the only privileged setup required for a new environment:
#   - creates/reuses the shared Terraform state backend in AZURE_LOCATION
#   - grants Storage Blob Data Contributor to the GitHub OIDC application
#   - creates/reuses the selected environment's OIDC federation
#   - writes Azure OIDC secrets ONLY to the selected GitHub Environment
#
# Normal GitHub Actions deployments never need roleAssignments/write.

ENVIRONMENT="${1:-}"
case "$ENVIRONMENT" in
  production|staging|prod|vm-staging) ;;
  *)
    echo "Usage: $0 <production|staging|prod|vm-staging>" >&2
    exit 2
    ;;
esac

APP_NAME="${AZURE_GITHUB_APP_NAME:-snipeit-lite-github-actions}"
SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
TENANT_ID="${AZURE_TENANT_ID:-$(az account show --query tenantId -o tsv)}"
REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
LOCATION="${AZURE_LOCATION:-$(gh variable get AZURE_LOCATION --env "$ENVIRONMENT" 2>/dev/null || true)}"
if [[ -z "$LOCATION" ]]; then
  echo "ERROR: AZURE_LOCATION is required. Set it as an environment-scoped GitHub variable or export AZURE_LOCATION before running bootstrap." >&2
  exit 1
fi

if ! az account list-locations --query "[?name=='${LOCATION}'].name | [0]" -o tsv | grep -qx "$LOCATION"; then
  echo "ERROR: Azure location '$LOCATION' is not valid for this subscription." >&2
  exit 1
fi
ROLE="Contributor"
SCOPE="/subscriptions/$SUBSCRIPTION_ID"

if [[ -z "$SUBSCRIPTION_ID" || -z "$TENANT_ID" || -z "$REPO" ]]; then
  echo "Missing subscription, tenant, or GitHub repository context." >&2
  exit 1
fi

# Force the Azure CLI commands below to use the selected subscription.
az account set --subscription "$SUBSCRIPTION_ID"
az account show --subscription "$SUBSCRIPTION_ID" --query "{id:id,tenantId:tenantId,state:state}" -o table >/dev/null

echo "Environment  : $ENVIRONMENT"
echo "Subscription : $SUBSCRIPTION_ID"
echo "Tenant       : $TENANT_ID"
echo "GitHub repo  : $REPO"
echo "Azure region : $LOCATION"
echo "CI app       : $APP_NAME"
echo

echo "Registering Azure providers used by infrastructure..."
for provider in \
  Microsoft.Resources \
  Microsoft.Compute \
  Microsoft.App \
  Microsoft.DBforPostgreSQL \
  Microsoft.Insights \
  Microsoft.Network \
  Microsoft.OperationalInsights \
  Microsoft.Storage \
  Microsoft.RecoveryServices
 do
  az provider register --namespace "$provider" --wait >/dev/null
done

mapfile -t APP_IDS < <(az ad app list --display-name "$APP_NAME" --query '[].appId' -o tsv)
if [[ "${#APP_IDS[@]}" -gt 1 ]]; then
  echo "ERROR: Multiple Microsoft Entra applications named '$APP_NAME' were found. Refusing to guess." >&2
  printf '%s\n' "${APP_IDS[@]}" >&2
  exit 1
fi
APP_ID="${APP_IDS[0]:-}"
if [[ -z "$APP_ID" ]]; then
  echo "Creating Microsoft Entra application..."
  APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
else
  echo "Reusing existing Microsoft Entra application: $APP_ID"
fi

mapfile -t SP_IDS < <(az ad sp list --filter "appId eq '$APP_ID'" --query '[].id' -o tsv)
if [[ "${#SP_IDS[@]}" -gt 1 ]]; then
  echo "ERROR: Multiple service principals were found for application '$APP_ID'. Refusing to guess." >&2
  printf '%s\n' "${SP_IDS[@]}" >&2
  exit 1
fi
SP_ID="${SP_IDS[0]:-}"
if [[ -z "$SP_ID" ]]; then
  echo "Creating service principal..."
  SP_ID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"
else
  echo "Reusing existing service principal: $SP_ID"
fi

CONTRIBUTOR_ROLE_ID="b24988ac-6180-42a0-ab88-20f7382dd24c"
ROLE_DEFINITION_ID="$SCOPE/providers/Microsoft.Authorization/roleDefinitions/$CONTRIBUTOR_ROLE_ID"
ROLE_ASSIGNMENTS_URL="https://management.azure.com${SCOPE}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01"
EXISTING_ASSIGNMENT_ID="$(az rest --method get --url "$ROLE_ASSIGNMENTS_URL" \
  --query "value[?properties.principalId=='$SP_ID' && properties.roleDefinitionId=='$ROLE_DEFINITION_ID' && properties.scope=='$SCOPE'] | [0].name" -o tsv)"

if [[ -n "$EXISTING_ASSIGNMENT_ID" ]]; then
  echo "Subscription Contributor role already present: $EXISTING_ASSIGNMENT_ID"
else
  echo "Granting $ROLE on the subscription..."
  ROLE_ASSIGNMENT_ID="$(python -c 'import sys,uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, sys.argv[1]))' "${SCOPE}:${SP_ID}:${CONTRIBUTOR_ROLE_ID}")"
  ROLE_ASSIGNMENT_URL="https://management.azure.com${SCOPE}/providers/Microsoft.Authorization/roleAssignments/${ROLE_ASSIGNMENT_ID}?api-version=2022-04-01"
  ROLE_BODY="{\"properties\":{\"roleDefinitionId\":\"${ROLE_DEFINITION_ID}\",\"principalId\":\"${SP_ID}\",\"principalType\":\"ServicePrincipal\"}}"
  az rest --method put --url "$ROLE_ASSIGNMENT_URL" --body "$ROLE_BODY" >/dev/null
fi

create_federated_credential() {
  local name="$1"
  local subject="$2"
  local existing
  existing="$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$name'] | [0].name" -o tsv)"
  if [[ -n "$existing" ]]; then
    echo "Federated credential already exists: $name"
    return
  fi
  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' RETURN
  cat >"$tmp" <<JSON
{
  "name": "$name",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "$subject",
  "description": "GitHub Actions OIDC for $REPO ($name)",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
  echo "Creating federated credential: $name"
  az ad app federated-credential create --id "$APP_ID" --parameters "@$tmp" >/dev/null
}

FED_NAME="github-${ENVIRONMENT}"
FED_SUBJECT="repo:${REPO}:environment:${ENVIRONMENT}"
create_federated_credential "$FED_NAME" "$FED_SUBJECT"

# Reconcile ONLY the selected GitHub Environment.
# `gh secret set` is an upsert: an existing secret is overwritten with the
# current value, while a missing secret is created. We deliberately never
# delete GitHub secrets or variables, so a rebuild does not require manual
# cleanup in the GitHub UI.
set_github_environment_secret() {
  local name="$1"
  local value="$2"
  echo "Setting GitHub Environment secret: $name"
  gh secret set "$name" \
    --repo "$REPO" \
    --env "$ENVIRONMENT" \
    --body "$value" >/dev/null
}

set_github_environment_secret AZURE_CLIENT_ID "$APP_ID"
set_github_environment_secret AZURE_TENANT_ID "$TENANT_ID"
set_github_environment_secret AZURE_SUBSCRIPTION_ID "$SUBSCRIPTION_ID"

# Create/reuse Terraform state, grant the state data-plane role, and create
# the state container as part of this one-time privileged bootstrap.
# State resources are always created/reused in AZURE_LOCATION.
export ARM_CLIENT_ID="$APP_ID"
export ARM_TENANT_ID="$TENANT_ID"
export ARM_SUBSCRIPTION_ID="$SUBSCRIPTION_ID"
export AZURE_LOCATION="$LOCATION"
export ALLOW_RBAC_BOOTSTRAP=true

if ! ./scripts/bootstrap-terraform-state.sh; then
  echo "ERROR: Terraform state backend bootstrap failed. No deployment should be attempted." >&2
  exit 1
fi

unset ALLOW_RBAC_BOOTSTRAP ARM_CLIENT_ID ARM_TENANT_ID ARM_SUBSCRIPTION_ID AZURE_LOCATION

echo
echo "Bootstrap complete for GitHub Environment '$ENVIRONMENT'."
echo "No other GitHub Environment was modified."
echo "Future deployments use the existing state backend and RBAC automatically."
