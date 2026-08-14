#!/usr/bin/env bash
set -euo pipefail

# Defensive only: when this script is run under Git Bash for Windows
# (MINGW64), MSYS silently rewrites any standalone argument that looks like
# an absolute POSIX path (starts with "/") into a Windows path before the
# target program ever sees it. `SCOPE="/subscriptions/<id>"` matches that
# pattern. We route every scope-bearing Azure call through `az rest --url
# "https://management.azure.com${SCOPE}/..."` below specifically to avoid
# this (a URL that starts with "https://" is never rewritten), but this
# export is kept as a second line of defense in case any future edit adds a
# bare "/subscriptions/..." argument back in.
export MSYS_NO_PATHCONV=1

# One-time, idempotent ACA GitHub OIDC bootstrap.
#
# Run locally after `az login` and `gh auth login`:
#   ./scripts/bootstrap-aca-github.sh production
#   ./scripts/bootstrap-aca-github.sh staging
#
# The script discovers the Azure subscription/tenant from the current Azure
# CLI login and creates/reuses the GitHub Actions Entra application by name.
# It then creates/reuses the service principal, GitHub OIDC federation, and
# the Azure RBAC required by the ACA infrastructure workflow. Finally it
# writes the three Azure login secrets to the selected GitHub Environment.
#
# It deliberately does NOT manage:
#   - Terraform state
#   - VM resources
#   - ACA providers
#   - resource groups
#   - Bicep / deployment stacks
#   - Container Apps resources
# Those belong to the ACA infrastructure/deployment workflows.

ENVIRONMENT="${1:-}"
case "$ENVIRONMENT" in
  production|staging) ;;
  *)
    echo "Usage: $0 <production|staging>" >&2
    exit 2
    ;;
esac

APP_NAME="${AZURE_GITHUB_APP_NAME:-snipeit-lite-github-actions}"
REPO="${GITHUB_REPOSITORY:-}"

command -v az >/dev/null 2>&1 || { echo "ERROR: Azure CLI (az) is required." >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "ERROR: GitHub CLI (gh) is required." >&2; exit 1; }
az account show >/dev/null 2>&1 || { echo "ERROR: Azure CLI is not logged in. Run: az login" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: GitHub CLI is not authenticated. Run: gh auth login" >&2; exit 1; }

SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
TENANT_ID="${AZURE_TENANT_ID:-$(az account show --query tenantId -o tsv)}"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

if [[ -z "$SUBSCRIPTION_ID" || -z "$TENANT_ID" || -z "$REPO" ]]; then
  echo "ERROR: Could not determine subscription, tenant, or GitHub repository." >&2
  exit 1
fi

az account set --subscription "$SUBSCRIPTION_ID"

SCOPE="/subscriptions/$SUBSCRIPTION_ID"
CONTRIBUTOR_ROLE_ID="b24988ac-6180-42a0-ab88-20f7382dd24c"
ROLE_DEFINITION_ID="$SCOPE/providers/Microsoft.Authorization/roleDefinitions/$CONTRIBUTOR_ROLE_ID"

# Azure CLI occasionally returns a trailing CR when invoked from Git Bash on
# Windows. Normalize IDs before passing them back to Entra/Graph commands.
normalize_id() {
  local value="$1"
  value="${value//$'\r'/}"
  value="${value//$'\n'/}"
  printf '%s' "$value"
}

SUBSCRIPTION_ID="$(normalize_id "$SUBSCRIPTION_ID")"
TENANT_ID="$(normalize_id "$TENANT_ID")"

UUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
for pair in \
  "AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID" \
  "AZURE_TENANT_ID=$TENANT_ID"
do
  NAME="${pair%%=*}"
  VALUE="${pair#*=}"
  if [[ ! "$VALUE" =~ $UUID_RE ]]; then
    echo "ERROR: $NAME is not a valid UUID: $VALUE" >&2
    exit 1
  fi
done

echo "Environment   : $ENVIRONMENT"
echo "Subscription  : $SUBSCRIPTION_ID"
echo "Tenant        : $TENANT_ID"
echo "GitHub repo   : $REPO"
echo "OIDC app      : $APP_NAME"
echo

echo "Checking Microsoft Entra application..."
mapfile -t APP_IDS < <(az ad app list --display-name "$APP_NAME" --query '[].appId' -o tsv 2>/dev/null || true)

if [[ "${#APP_IDS[@]}" -gt 1 ]]; then
  echo "ERROR: Multiple Microsoft Entra applications named '$APP_NAME' were found. Refusing to guess." >&2
  printf '  %s\n' "${APP_IDS[@]}" >&2
  exit 1
fi

APP_ID="$(normalize_id "${APP_IDS[0]:-}")"
if [[ -z "$APP_ID" ]]; then
  echo "Creating Microsoft Entra application '$APP_NAME'..."
  APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
  APP_ID="$(normalize_id "$APP_ID")"
  echo "Created application: $APP_ID"
else
  echo "Reusing existing Microsoft Entra application: $APP_ID"
fi

# Confirm that the value is really an application appId before attempting
# service-principal creation. This also gives Entra propagation a moment when
# the application was created moments ago.
APP_READY=false
for attempt in 1 2 3 4 5 6; do
  if VERIFIED_APP_ID="$(az ad app show --id "$APP_ID" --query appId -o tsv 2>/dev/null)"; then
    VERIFIED_APP_ID="$(normalize_id "$VERIFIED_APP_ID")"
    if [[ "$VERIFIED_APP_ID" == "$APP_ID" ]]; then
      APP_READY=true
      break
    fi
  fi
  echo "Waiting for Microsoft Entra application propagation (attempt $attempt/6)..."
  sleep 5
done

if [[ "$APP_READY" != true ]]; then
  echo "ERROR: Microsoft Entra application '$APP_ID' could not be verified after creation/lookup." >&2
  exit 1
fi

# Resolve the service principal without the problematic server-side appId
# filter. `az ad sp list --all` returns the directory objects and the JMESPath
# expression is evaluated locally by Azure CLI. This avoids both the Graph
# appId filter failure and the JSONDecodeError path seen with `az ad sp show`.
get_sp_id() {
  local result
  result="$(az ad sp list --all --query "[?appId=='$APP_ID'].id | [0]" -o tsv 2>/dev/null || true)"
  normalize_id "$result"
}

SP_ID="$(get_sp_id)"
if [[ -n "$SP_ID" ]]; then
  echo "Reusing existing service principal: $SP_ID"
else
  echo "Creating service principal for application $APP_ID..."
  CREATED=false
  for attempt in 1 2 3 4 5; do
    if az ad sp create --id "$APP_ID" --only-show-errors >/dev/null 2>/tmp/aca-sp-create.err; then
      CREATED=true
      break
    fi

    # A concurrent/replication race can make create report an error even when
    # the service principal is already present. Always re-check before failing.
    SP_ID="$(get_sp_id)"
    if [[ -n "$SP_ID" ]]; then
      CREATED=true
      break
    fi

    echo "Service principal creation is not visible yet (attempt $attempt/5); retrying..."
    sleep 5
  done

  if [[ "$CREATED" != true ]]; then
    echo "ERROR: Could not create the service principal for '$APP_ID'." >&2
    cat /tmp/aca-sp-create.err >&2 || true
    rm -f /tmp/aca-sp-create.err
    exit 1
  fi
  rm -f /tmp/aca-sp-create.err

  SP_ID="$(get_sp_id)"
  if [[ -z "$SP_ID" ]]; then
    echo "ERROR: Service principal was created but could not be resolved afterwards." >&2
    exit 1
  fi
  echo "Service principal ready: $SP_ID"
fi

# The ACA infrastructure workflow creates the resource group itself. It runs
# at subscription scope initially, so the OIDC principal needs Contributor on
# the subscription. The assignment is idempotent and uses the SP object ID so
# Azure does not need to resolve the principal by display name/appId.
#
# NOTE: this is intentionally done via `az rest` against the Azure Resource
# Manager REST API rather than `az role assignment list/create --scope
# "$SCOPE"`. On Git Bash for Windows, a bare "/subscriptions/<id>" argument
# gets rewritten into a Windows path before Azure CLI parses it, which
# corrupts the scope and produces exactly the "MissingSubscription: ... did
# not have a subscription or a valid tenant level resource provider" error.
# Embedding the same scope inside a "https://management.azure.com/..." URL
# string avoids the rewrite, since only whole arguments starting with "/" are
# affected. This mirrors the approach already used in
# scripts/bootstrap-azure-github.sh for the same reason.
generate_role_assignment_guid() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import uuid; print(uuid.uuid4())'
  elif command -v python >/dev/null 2>&1; then
    python -c 'import uuid; print(uuid.uuid4())'
  else
    local hex
    hex="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
    if [[ -z "$hex" || "${#hex}" -lt 32 ]]; then
      hex="$(printf '%08x%08x%08x%08x' "$RANDOM$RANDOM" "$RANDOM$RANDOM" "$RANDOM$RANDOM" "$RANDOM$RANDOM")"
      hex="${hex:0:32}"
    fi
    printf '%s-%s-4%s-8%s-%s\n' "${hex:0:8}" "${hex:8:4}" "${hex:13:3}" "${hex:17:3}" "${hex:20:12}"
  fi
}

ROLE_ASSIGNMENTS_URL="https://management.azure.com${SCOPE}/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01"

check_contributor_assignment() {
  az rest --method get --url "$ROLE_ASSIGNMENTS_URL" \
    --query "value[?properties.principalId=='$SP_ID' && properties.roleDefinitionId=='$ROLE_DEFINITION_ID' && properties.scope=='$SCOPE'] | [0].name" \
    -o tsv --only-show-errors 2>/dev/null || true
}

echo "Checking subscription Contributor role..."
EXISTING_ASSIGNMENT_ID="$(normalize_id "$(check_contributor_assignment)")"

if [[ -n "$EXISTING_ASSIGNMENT_ID" ]]; then
  echo "Subscription Contributor role already present."
else
  echo "Granting Contributor on subscription to the GitHub OIDC service principal..."
  ASSIGNED=false
  for attempt in 1 2 3 4 5; do
    ROLE_ASSIGNMENT_ID="$(generate_role_assignment_guid)"
    ROLE_ASSIGNMENT_URL="https://management.azure.com${SCOPE}/providers/Microsoft.Authorization/roleAssignments/${ROLE_ASSIGNMENT_ID}?api-version=2022-04-01"
    ROLE_BODY="{\"properties\":{\"roleDefinitionId\":\"${ROLE_DEFINITION_ID}\",\"principalId\":\"${SP_ID}\",\"principalType\":\"ServicePrincipal\"}}"

    if az rest --method put --url "$ROLE_ASSIGNMENT_URL" --body "$ROLE_BODY" \
      --only-show-errors >/dev/null 2>/tmp/aca-role.err; then
      ASSIGNED=true
      break
    fi

    # A concurrent/replication race can make the PUT report an error even
    # when the assignment already exists (or lands moments later). Always
    # re-check before treating this attempt as a failure.
    if [[ -n "$(normalize_id "$(check_contributor_assignment)")" ]]; then
      ASSIGNED=true
      break
    fi

    echo "Waiting for service principal replication before RBAC assignment (attempt $attempt/5)..."
    sleep 5
  done

  if [[ "$ASSIGNED" != true ]]; then
    echo "ERROR: Could not grant Contributor to the GitHub OIDC service principal." >&2
    cat /tmp/aca-role.err >&2 || true
    rm -f /tmp/aca-role.err
    exit 1
  fi
  rm -f /tmp/aca-role.err
  echo "Subscription Contributor role ready."
fi

# Reconcile the selected environment's GitHub OIDC credential. Existing
# credentials are updated if their subject/issuer/audience drifted; otherwise
# they are left alone. This keeps reruns safe and fixes stale configuration.
FED_NAME="github-${ENVIRONMENT}"
FED_SUBJECT="repo:${REPO}:environment:${ENVIRONMENT}"
FED_ISSUER="https://token.actions.githubusercontent.com/"
FED_AUDIENCE="api://AzureADTokenExchange"

FED_ID="$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$FED_NAME'] | [0].id" -o tsv 2>/dev/null || true)"
FED_ID="$(normalize_id "$FED_ID")"
FED_EXISTING_SUBJECT="$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$FED_NAME'] | [0].subject" -o tsv 2>/dev/null || true)"
FED_EXISTING_SUBJECT="$(normalize_id "$FED_EXISTING_SUBJECT")"
FED_EXISTING_ISSUER="$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$FED_NAME'] | [0].issuer" -o tsv 2>/dev/null || true)"
FED_EXISTING_ISSUER="$(normalize_id "$FED_EXISTING_ISSUER")"

FED_PARAMETERS="$(cat <<JSON
{
  "name": "$FED_NAME",
  "issuer": "$FED_ISSUER",
  "subject": "$FED_SUBJECT",
  "description": "GitHub Actions OIDC for $REPO ($ENVIRONMENT)",
  "audiences": ["$FED_AUDIENCE"]
}
JSON
)"

if [[ -z "$FED_ID" ]]; then
  echo "Creating GitHub OIDC federated credential: $FED_NAME"
  az ad app federated-credential create --id "$APP_ID" --parameters "$FED_PARAMETERS" --only-show-errors >/dev/null
else
  if [[ "$FED_EXISTING_SUBJECT" == "$FED_SUBJECT" && "$FED_EXISTING_ISSUER" == "$FED_ISSUER" ]]; then
    echo "GitHub OIDC federated credential already correct: $FED_NAME"
  else
    echo "Updating GitHub OIDC federated credential: $FED_NAME"
    az ad app federated-credential update \
      --id "$APP_ID" \
      --federated-credential-id "$FED_ID" \
      --parameters "$FED_PARAMETERS" \
      --only-show-errors >/dev/null
  fi
fi

# Only after Azure identity, federation, and RBAC are ready do we publish the
# three coordinates used by azure/login. gh secret set is an upsert.
set_github_environment_secret() {
  local name="$1"
  local value="$2"
  echo "Setting GitHub Environment secret: $name"
  gh secret set "$name" --repo "$REPO" --env "$ENVIRONMENT" --body "$value" >/dev/null
}

set_github_environment_secret AZURE_CLIENT_ID "$APP_ID"
set_github_environment_secret AZURE_TENANT_ID "$TENANT_ID"
set_github_environment_secret AZURE_SUBSCRIPTION_ID "$SUBSCRIPTION_ID"

# ACA workflows use a fixed region in the workflow itself. We deliberately do
# not create an AZURE_LOCATION GitHub variable here, keeping bootstrap limited
# to the identity inputs consumed by azure/login.

echo
echo "ACA GitHub bootstrap complete for '$ENVIRONMENT'."
echo "Identity: $APP_NAME ($APP_ID)"
echo "GitHub OIDC: $FED_NAME"
echo "RBAC: Contributor on $SCOPE"
echo "Secrets: AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID"
echo
echo "Next: run the ACA infrastructure workflow, then Deploy to ACA."
