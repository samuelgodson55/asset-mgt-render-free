#!/usr/bin/env bash
set -euo pipefail

# One-time Azure/GitHub bootstrap. Run locally after `az login` and `gh auth login`.
# This creates the CI identity; CI then creates/updates/destroys all Bicep resources.
#
# Usage:
#   ./scripts/bootstrap-azure-github.sh
#
# No Azure resource group is created here. The Bicep workflow derives the
# resource-group name and creates it when needed.

APP_NAME="${AZURE_GITHUB_APP_NAME:-snipeit-lite-github-actions}"
SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
TENANT_ID="${AZURE_TENANT_ID:-$(az account show --query tenantId -o tsv)}"
REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
ROLE="Contributor"
SCOPE="/subscriptions/$SUBSCRIPTION_ID"

if [[ -z "$SUBSCRIPTION_ID" || -z "$TENANT_ID" || -z "$REPO" ]]; then
  echo "Missing subscription, tenant, or GitHub repository context." >&2
  exit 1
fi

echo "Subscription : $SUBSCRIPTION_ID"
echo "Tenant       : $TENANT_ID"
echo "GitHub repo  : $REPO"
echo "CI app       : $APP_NAME"
echo

echo "Registering Azure providers used by infra/main.bicep..."
for provider in \
  Microsoft.Resources \
  Microsoft.App \
  Microsoft.DBforPostgreSQL \
  Microsoft.Insights \
  Microsoft.Network \
  Microsoft.OperationalInsights \
  Microsoft.Storage
do
  az provider register --namespace "$provider" --wait >/dev/null
done

APP_ID="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv)"
if [[ -z "$APP_ID" ]]; then
  echo "Creating Microsoft Entra application..."
  APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
else
  echo "Reusing existing Microsoft Entra application: $APP_ID"
fi

SP_ID="$(az ad sp list --filter "appId eq '$APP_ID'" --query '[0].id' -o tsv)"
if [[ -z "$SP_ID" ]]; then
  echo "Creating service principal..."
  SP_ID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"
else
  echo "Reusing existing service principal: $SP_ID"
fi

if ! az role assignment list \
  --assignee "$APP_ID" \
  --scope "$SCOPE" \
  --role "$ROLE" \
  --query '[0].id' -o tsv | grep -q .; then
  echo "Granting $ROLE on the subscription..."
  az role assignment create \
    --assignee "$APP_ID" \
    --role "$ROLE" \
    --scope "$SCOPE" >/dev/null
else
  echo "Subscription Contributor role already present."
fi

create_federated_credential() {
  local name="$1"
  local subject="$2"
  local existing
  existing="$(az ad app federated-credential list --id "$APP_ID" \
    --query "[?name=='$name'] | [0].name" -o tsv)"
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
  rm -f "$tmp"
  trap - RETURN
}

# These are the GitHub Environments used by the existing Container Apps and VM
# workflows. The same CI identity is safe to reuse because the environment
# boundary is enforced by GitHub's OIDC subject and Azure only accepts these
# exact subjects.
declare -A ENV_SUBJECTS=(
  [production]="repo:${REPO}:environment:production"
  [staging]="repo:${REPO}:environment:staging"
  [prod]="repo:${REPO}:environment:prod"
  [vm-staging]="repo:${REPO}:environment:vm-staging"
)

for env in production staging prod vm-staging; do
  create_federated_credential "github-${env}" "${ENV_SUBJECTS[$env]}"
done

echo "Writing Azure OIDC values to the four existing GitHub Environments..."
for env in production staging prod vm-staging; do
  gh secret set AZURE_CLIENT_ID --env "$env" --body "$APP_ID"
  gh secret set AZURE_TENANT_ID --env "$env" --body "$TENANT_ID"
  gh secret set AZURE_SUBSCRIPTION_ID --env "$env" --body "$SUBSCRIPTION_ID"
done

echo
echo "Bootstrap complete."
echo "The workflow now owns:"
echo "  - resource-group creation"
echo "  - Bicep deployment-stack lifecycle"
echo "  - safe Bicep destroy"
echo "  - Azure provider registration"
echo "  - GitHub Actions OIDC authentication"
echo
echo "You still need application/runtime secrets such as POSTGRES_PASSWORD,"
echo "JWT_SECRET_KEY, Docker Hub credentials, etc. Those are application data,"
echo "not Azure infrastructure bootstrap and are intentionally not generated"
echo "or copied by this script."
