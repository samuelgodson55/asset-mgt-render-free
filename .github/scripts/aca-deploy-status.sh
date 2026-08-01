#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# .github/scripts/aca-deploy-status.sh
# -----------------------------------------------------------------------------
# The ACA-path equivalent of scripts/blue-green-deploy.sh's write_status()
# function on the VM -- except this runs on the GitHub-hosted RUNNER (there's
# no VM to SSH into and write a local file on), and the "disk" it writes to
# is the `deploy-status` Blob container (infra/main.bicep's
# deployStatusContainer), which `frontend`'s nginx proxies LIVE, per-request
# (see nginx/default.conf.template's own /_deploy/ comment) -- not a mount.
# Blob Storage has no "append" primitive over its REST API either, so this
# script still keeps its own local working copy of status.json/checks.log in
# $WORKDIR and re-uploads the WHOLE file (overwrite) after every change --
# cheap and simple for a file this small, updated at most a few dozen times
# per deploy.
#
# BUG FIX: this used to upload to the `deploy-status` Azure FILES share via
# `az storage file upload`/`az storage share create`. That share (and the
# `mountOptions` tweak `frontend`'s volume mount needed to keep it fresh)
# are gone -- ACA rejects the `actimeo` mount option outright, and doesn't
# support mounting Blob Storage as a volume at all -- see infra/main.bicep's
# own comment on `deployStatusContainer` for the full history. `frontend`
# now proxies live to Blob Storage instead of mounting anything, so this
# script's job changed from "keep a share in sync" to "keep a blob
# container in sync" -- same shape, different Azure Storage service (`az
# storage blob upload`/`az storage container create` instead of their
# `file`/`share` equivalents).
#
# SUBCOMMANDS
#   init      <storage-account> <workdir>
#             Uploads a freshly-generated .htpasswd (from
#             DEPLOY_STATUS_USER/DEPLOY_STATUS_PASSWORD env vars -- see
#             below) to the container, and resets the local status.json/
#             checks.log to a clean "starting" state. Call this ONCE, at
#             the very start of deploy-azure-aca.yml's `deploy` job.
#             NOTE: unlike the old Azure-Files version, this does NOT
#             upload the dashboard's index.html -- it now ships baked
#             into the `frontend` image itself (see
#             frontend/Dockerfile's own COPY of
#             scripts/deploy-status-aca/index.html), since it only
#             changes with a code release, not per-deploy.
#   write     <phase> [json_fields_no_braces]
#             Writes status.json (locally, then on the container) with the
#             given phase and any extra fields (raw JSON, comma-prefixed
#             automatically -- same calling convention as
#             blue-green-deploy.sh's write_status). Always includes
#             $IMAGE_TAG/$ENVIRONMENT from the environment if set.
#             MERGES "apps" rather than overwriting it: aca-blue-green.sh's
#             own Gate-3 canary loop calls this once per traffic step with
#             only the ONE app it's currently rolling out (e.g. "apps":
#             {"frontend": {...}}) -- deploy-azure-aca.yml's own checkpoint
#             writes include both apps, but the per-step writes in between
#             don't. A plain overwrite would erase the OTHER app's
#             already-reported state for the whole duration of that loop
#             (see cmd_write's own comment below for the fix history). Every
#             other top-level field (phase/environment/image_tag/
#             started_at/updated_at) still comes entirely from THIS call.
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
# AUTH: every `az storage blob`/`az storage container` call below
# deliberately omits an explicit `--account-key`/`--sas-token` -- the Azure
# CLI auto-resolves the account key via ARM
# (Microsoft.Storage/storageAccounts/listKeys/action) using whatever
# identity `azure/login@v3` already authenticated as earlier in the calling
# workflow, the same Contributor-level OIDC identity that just ran `az
# deployment group create` against this same resource group. No extra
# secret needed -- and note this is a DIFFERENT credential from
# `DEPLOY_STATUS_SAS` (infra/main.bicep), which is what `frontend`'s nginx
# uses to READ this container at request time; this script only ever WRITES.
#
# Best-effort throughout: every failure here is a `|| true`/`2>/dev/null
# || echo ...` -- a storage hiccup uploading STATUS should never fail the
# actual deploy it's trying to report on.
# -----------------------------------------------------------------------------
set -uo pipefail

CONTAINER="deploy-status"

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

  # Container may already exist from a previous deploy (infra-deploy.yml
  # only creates it once) -- creating it again is a harmless no-op, and
  # this guards a repo that ran deploy-azure-aca.yml before its infra was
  # ever re-applied with the deploy-status container added.
  az storage container create --account-name "$STORAGE_ACCOUNT" --name "$CONTAINER" \
    --public-access off >/dev/null 2>&1 || true

  if [ -n "${DEPLOY_STATUS_USER:-}" ] && [ -n "${DEPLOY_STATUS_PASSWORD_APR1_HASH:-}" ]; then
    echo "${DEPLOY_STATUS_USER}:${DEPLOY_STATUS_PASSWORD_APR1_HASH}" > "$WORKDIR/.htpasswd"
    az storage blob upload --account-name "$STORAGE_ACCOUNT" --container-name "$CONTAINER" \
      --file "$WORKDIR/.htpasswd" --name ".htpasswd" --content-type "text/plain" \
      --overwrite >/dev/null 2>&1 \
      || echo "::warning::failed to upload .htpasswd for the deploy-status dashboard (non-fatal)"
  else
    echo "::warning::DEPLOY_STATUS_USER/DEPLOY_STATUS_PASSWORD_APR1_HASH not set -- leaving any existing .htpasswd on the container untouched. Without one ever having been uploaded, nginx's auth_basic_user_file has nothing to check against and /_deploy/ will 401 on every request -- see nginx/default.conf.template."
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
  local new_json
  new_json="$(cat <<JSON
{
  "phase": "$phase",
  "environment": "${ENVIRONMENT:-unknown}",
  "image_tag": "${IMAGE_TAG:-unknown}",
  "started_at": "${STARTED_AT:-unknown}",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"$extra_line
}
JSON
)"

  # BUG FIX: this used to `cat >` status.json unconditionally -- a full
  # overwrite, not a merge. aca-blue-green.sh's Gate-3 canary loop writes
  # here once per traffic step with ONLY the single app it's currently
  # rolling out (e.g. "apps": {"frontend": {...}}); a plain overwrite wiped
  # out whatever the OTHER app's card had last reported, so for the entire
  # duration of frontend's ramp the dashboard's backend card went blank
  # ("--") even though backend had already finished and was sitting at
  # 100%. jq is already a given in this pipeline (deploy-azure-aca.yml
  # itself uses it, e.g. its "Get container app job execution name" step),
  # so this merges THIS write's "apps" into whatever "apps" the previous
  # status.json already had -- per-app, per-field ("*" is jq's recursive
  # merge) -- rather than replacing the object wholesale. Every OTHER
  # top-level field (phase/environment/image_tag/started_at/updated_at)
  # still comes entirely from this write, same as before. Falls back to a
  # plain overwrite (the old behavior) if there's no previous file yet
  # (the very first write of a run) or if jq isn't available/the previous
  # file isn't valid JSON -- never let a merge failure block reporting
  # status at all.
  if [ -s "$WORKDIR/status.json" ] && command -v jq >/dev/null 2>&1; then
    if ! jq -n \
         --argjson old "$(cat "$WORKDIR/status.json" 2>/dev/null || echo '{}')" \
         --argjson new "$new_json" \
         '$new + {apps: (($old.apps // {}) * ($new.apps // {}))}' \
         > "$WORKDIR/status.json.tmp" 2>/dev/null; then
      echo "::warning::aca-deploy-status.sh: failed to merge status.json's 'apps' (phase=$phase) -- falling back to a plain overwrite for this write; the other app's card may show stale/blank state until its own next write"
      printf '%s\n' "$new_json" > "$WORKDIR/status.json.tmp"
    fi
    mv "$WORKDIR/status.json.tmp" "$WORKDIR/status.json"
  else
    printf '%s\n' "$new_json" > "$WORKDIR/status.json"
  fi

  az storage blob upload --account-name "$STORAGE_ACCOUNT" --container-name "$CONTAINER" \
    --file "$WORKDIR/status.json" --name "status.json" --content-type "application/json" \
    --overwrite >/dev/null 2>&1 \
    || echo "::warning::failed to upload status.json (phase=$phase) -- dashboard will show stale state until the next successful write"
}

cmd_check() {
  [ "$#" -eq 3 ] || usage
  local check="$1" status="$2" detail="$3"
  _load_stash
  # "pass"/"fail" are verdicts; "pending" (aca-blue-green.sh's
  # wait_for_revision_healthy heartbeat, written once per poll while a
  # health wait is still in progress) is neither -- it's proof the wait
  # loop is still alive, not a result. Anything else unrecognized still
  # collapses to "fail" rather than being silently dropped, same as
  # before this "pending" state existed.
  local py_status="fail"
  case "$status" in
    pass|pending) py_status="$status" ;;
  esac
  # Minimal manual JSON-line construction (no jq dependency, matching
  # scripts/health-check.sh's own approach on the VM path) -- $detail is
  # expected to be a short, log-safe string; quotes inside it are escaped
  # so a stray one can't break the line.
  local escaped_detail="${detail//\"/\\\"}"
  printf '{"check": "%s", "status": "%s", "detail": "%s", "ts": "%s"}\n' \
    "$check" "$py_status" "$escaped_detail" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$WORKDIR/checks.log"
  az storage blob upload --account-name "$STORAGE_ACCOUNT" --container-name "$CONTAINER" \
    --file "$WORKDIR/checks.log" --name "checks.log" --content-type "text/plain" \
    --overwrite >/dev/null 2>&1 \
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
