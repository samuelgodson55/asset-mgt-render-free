#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# .github/scripts/aca-deploy-status.sh
# -----------------------------------------------------------------------------
# The ACA-path equivalent of scripts/blue-green-deploy.sh's write_status()
# function on the VM -- except this runs on the GitHub-hosted RUNNER (there's
# no VM to SSH into and write a local file on), and the "disk" it writes to
# is the `deploy-status` Azure Files share (infra/main.bicep's
# deployStatusShare), which `frontend` mounts READ-ONLY and serves at
# /_deploy/ (see nginx/default.conf.template's own comment). Azure Files has
# no "append" primitive over its REST API, so this script keeps its own
# local working copy of status.json/checks.log in $WORKDIR and re-uploads
# the WHOLE file (overwrite) after every change -- cheap and simple for a
# file this small, updated at most a few dozen times per deploy.
#
# SUBCOMMANDS
#   init      <storage-account> <workdir>
#             Uploads this repo's own scripts/deploy-status-aca/index.html
#             (the dashboard) and a freshly-generated .htpasswd (from
#             DEPLOY_STATUS_USER/DEPLOY_STATUS_PASSWORD env vars -- see
#             below) to the share, and resets the local status.json/
#             checks.log to a clean "starting" state. Call this ONCE, at
#             the very start of deploy-azure-aca.yml's `deploy` job.
#   write     <phase> [json_fields_no_braces]
#             Overwrites status.json (locally, then on the share) with the
#             given phase and any extra fields (raw JSON, comma-prefixed
#             automatically -- same calling convention as
#             blue-green-deploy.sh's write_status). Always includes
#             $IMAGE_TAG/$ENVIRONMENT from the environment if set.
#   check     <check-name> <pass|fail> <detail>
#             Appends one JSON line to checks.log (locally, then re-uploads
#             the whole file), same shape as scripts/health-check.sh's own
#             output lines on the VM path, so the dashboard's checks-log
#             renderer (shared markup/JS between both dashboards) can
#             treat them identically.
#
# ENV VARS THIS SCRIPT READS
#   STORAGE_ACCOUNT           Azure Storage account name (main.bicep's own
#                              `storageAccountName` output)
#   IMAGE_TAG, ENVIRONMENT     included in every status.json write, if set
#   DEPLOY_STATUS_USER         only read by `init` -- Basic Auth username
#   DEPLOY_STATUS_PASSWORD_APR1_HASH
#                              only read by `init` -- an $apr1$-format hash
#                              (NOT bcrypt -- nginx's auth_basic_user_file
#                              only understands {PLAIN}/{SSHA}/$apr1$, not
#                              $2a$/$2b$/$2y$ -- see
#                              nginx/default.conf.template's own comment).
#                              Generate one with:
#                                openssl passwd -apr1 'your-password-here'
#
# AUTH: every `az storage file` call below deliberately omits an explicit
# `--account-key`/`--sas-token` -- the Azure CLI auto-resolves the account
# key via ARM (Microsoft.Storage/storageAccounts/listKeys/action) using
# whatever identity `azure/login@v3` already authenticated as earlier in
# the calling workflow, the same Contributor-level OIDC identity that just
# ran `az deployment group create` against this same resource group. No
# extra secret needed.
#
# Best-effort throughout: every failure here is a `|| true`/`2>/dev/null
# || echo ...` -- a storage hiccup uploading STATUS should never fail the
# actual deploy it's trying to report on.
# -----------------------------------------------------------------------------
set -uo pipefail

SHARE="deploy-status"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") init  <storage-account> <workdir>
  $(basename "$0") write <phase> [json_fields_no_braces]
  $(basename "$0") check <check-name> <pass|fail> <detail>

STORAGE_ACCOUNT/WORKDIR must be set via 'init' first (this script stashes
them in \$RUNNER_TEMP/aca-deploy-status.env for subsequent write/check
calls within the same job).
EOF
  exit 1
}

# write/check need STORAGE_ACCOUNT + WORKDIR from the earlier `init` call,
# but GitHub Actions steps don't share shell state -- stash them in a tiny
# env file under $RUNNER_TEMP (a real per-job scratch dir, always writable)
# and re-source it here instead of requiring every step to pass them again.
STASH="${RUNNER_TEMP:-/tmp}/aca-deploy-status.env"

cmd_init() {
  [ "$#" -eq 2 ] || usage
  STORAGE_ACCOUNT="$1"
  WORKDIR="$2"
  mkdir -p "$WORKDIR"
  { echo "STORAGE_ACCOUNT=$STORAGE_ACCOUNT"; echo "WORKDIR=$WORKDIR"; } > "$STASH"

  # Share may already exist from a previous deploy (infra-deploy.yml only
  # creates it once) -- creating it again is a harmless no-op, and this
  # guards a repo that ran deploy-azure-aca.yml before its infra was ever
  # re-applied with the deploy-status share added.
  az storage share create --account-name "$STORAGE_ACCOUNT" --name "$SHARE" >/dev/null 2>&1 || true

  echo "Uploading dashboard (scripts/deploy-status-aca/index.html)..."
  az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" \
    --source "scripts/deploy-status-aca/index.html" --path "index.html" \
    --no-progress >/dev/null 2>&1 \
    || echo "::warning::failed to upload deploy-status dashboard (non-fatal -- the deploy itself is unaffected)"

  if [ -n "${DEPLOY_STATUS_USER:-}" ] && [ -n "${DEPLOY_STATUS_PASSWORD_APR1_HASH:-}" ]; then
    echo "${DEPLOY_STATUS_USER}:${DEPLOY_STATUS_PASSWORD_APR1_HASH}" > "$WORKDIR/.htpasswd"
    az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" \
      --source "$WORKDIR/.htpasswd" --path ".htpasswd" \
      --no-progress >/dev/null 2>&1 \
      || echo "::warning::failed to upload .htpasswd for the deploy-status dashboard (non-fatal)"
  else
    echo "::warning::DEPLOY_STATUS_USER/DEPLOY_STATUS_PASSWORD_APR1_HASH not set -- leaving any existing .htpasswd on the share untouched. Without one ever having been uploaded, nginx's auth_basic_user_file has nothing to check against and /_deploy/ will 500, not fail open -- see nginx/default.conf.template."
  fi

  STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "STARTED_AT=$STARTED_AT" >> "$STASH"
  : > "$WORKDIR/checks.log"
  cmd_write "starting"
}

_load_stash() {
  [ -f "$STASH" ] || { echo "::error::aca-deploy-status.sh: no prior 'init' call found in this job (missing $STASH) -- call 'init' first." >&2; exit 1; }
  # shellcheck disable=SC1090
  source "$STASH"
}

cmd_write() {
  [ "$#" -ge 1 ] || usage
  local phase="$1" extra="${2:-}"
  _load_stash
  local extra_line=""
  [ -n "$extra" ] && extra_line=",
  $extra"
  cat > "$WORKDIR/status.json" <<JSON
{
  "phase": "$phase",
  "environment": "${ENVIRONMENT:-unknown}",
  "image_tag": "${IMAGE_TAG:-unknown}",
  "started_at": "${STARTED_AT:-unknown}",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"$extra_line
}
JSON
  az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" \
    --source "$WORKDIR/status.json" --path "status.json" \
    --no-progress >/dev/null 2>&1 \
    || echo "::warning::failed to upload status.json (phase=$phase) -- dashboard will show stale state until the next successful write"
}

cmd_check() {
  [ "$#" -eq 3 ] || usage
  local check="$1" status="$2" detail="$3"
  _load_stash
  local py_status="pass"
  [ "$status" = "pass" ] || py_status="fail"
  # Minimal manual JSON-line construction (no jq dependency, matching
  # scripts/health-check.sh's own approach on the VM path) -- $detail is
  # expected to be a short, log-safe string; quotes inside it are escaped
  # so a stray one can't break the line.
  local escaped_detail="${detail//\"/\\\"}"
  printf '{"check": "%s", "status": "%s", "detail": "%s", "ts": "%s"}\n' \
    "$check" "$py_status" "$escaped_detail" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$WORKDIR/checks.log"
  az storage file upload --account-name "$STORAGE_ACCOUNT" --share-name "$SHARE" \
    --source "$WORKDIR/checks.log" --path "checks.log" \
    --no-progress >/dev/null 2>&1 \
    || echo "::warning::failed to upload checks.log entry ($check=$status) -- dashboard will be missing this line until the next successful upload"
}

[ "$#" -ge 1 ] || usage
subcommand="$1"; shift
case "$subcommand" in
  init)  cmd_init "$@" ;;
  write) cmd_write "$@" ;;
  check) cmd_check "$@" ;;
  *) usage ;;
esac
