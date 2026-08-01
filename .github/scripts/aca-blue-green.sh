#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# .github/scripts/aca-blue-green.sh
# -----------------------------------------------------------------------------
# Blue-green rollout for a single Azure Container Apps app (`backend` or
# `frontend`), built on Container Apps' native "Multiple" active-revisions
# mode + weighted traffic splitting -- no external load balancer, no second
# environment, no extra Azure resource. One "slot" is just one revision.
#
# WHY THIS EXISTS (vs. the old Single-revision-mode rolling update):
# Single revision mode replaces replicas of the SAME revision in place --
# there's only ever one revision, so a bad new replica still shares fate
# with (and can still take real traffic alongside) the old ones while it's
# being health-checked. Multiple revision mode gives us two fully independent
# revisions (the currently-live one = "green", the incoming one being
# validated = "blue" -- fixed roles, never swapped; see this repo's
# blue-green.md) that can run side by side, with traffic weight decoupled
# from "which one is newest" -- which is what makes a genuine 0% -> 100%
# cutover, gradual traffic shift, and an instant traffic-only rollback (no
# redeploy needed) possible at all.
#
# THE FOUR SUBCOMMANDS
#   rollout   Create the new ("blue") revision at 0% traffic, wait for
#             Container Apps' OWN readiness probe (backend: /readyz on
#             :8000, frontend: / on :80 -- see infra/main.bicep's `probes`
#             blocks) to report the new revision Healthy, optionally smoke
#             test it directly on its own per-revision FQDN (only possible
#             for `frontend` -- see --public below), then walk traffic
#             across it in the requested steps, re-checking health after
#             each step. Never deactivates the currently-live ("green")
#             revision -- that's a separate, deliberate step (see
#             `finalize`).
#   finalize  Deactivate the old (former-"green") revision now that the
#             new one is fully live and proven under real traffic -- "spin
#             down the other slot." Call this ONLY after every app being
#             deployed this run (backend AND frontend) has finished its
#             rollout AND the end-to-end smoke test in
#             deploy-azure-aca.yml has passed -- see that workflow for why
#             the two apps' cutovers are gated together rather than each
#             finalizing independently. Once this returns, the revision
#             that was "blue" during the rollout is now simply the app's
#             one live revision -- "green" for the NEXT deploy's purposes.
#   rollback  Flip traffic back to the active revision at 100% and
#             deactivate the bad incoming one. Since the active revision
#             was never scaled down or stopped, this is a weight change,
#             not a redeploy -- the fastest possible recovery, and the
#             reason this whole design exists.
#   status    Read-only. Prints each revision's health, replica count, and
#             live traffic weight for one app. This is "the way to monitor
#             the checks" -- run it during a deploy (pass --watch to poll
#             every 5s) or any time after, from a laptop, with no GitHub
#             Actions access required, just `az login` and the resource
#             group name. deploy-azure-aca.yml also prints the same
#             information into the run's GITHUB_STEP_SUMMARY at each stage,
#             so you don't have to be at a terminal to watch it live.
#
# REQUIRES: az CLI, already `az login`'d (or, in CI, already
# `azure/login@v3`'d) with access to the target resource group. `jq` is not
# required -- everything goes through `az --query`/`-o tsv`.
# -----------------------------------------------------------------------------
set -uo pipefail

SCRIPT_NAME="$(basename "$0")"

usage() {
  cat <<EOF
Usage:
  $SCRIPT_NAME rollout  <app> <resource-group> <image> <canary-steps-csv> <step-wait-seconds> <public:true|false>
  $SCRIPT_NAME finalize <app> <resource-group> <active-revision>
  $SCRIPT_NAME rollback <app> <resource-group> <active-revision> <incoming-revision>
  $SCRIPT_NAME status   <app> <resource-group> [--watch]

  app                 Container App name, e.g. "backend" or "frontend"
  resource-group      Azure resource group containing it
  image               Full image ref to deploy, e.g. user/repo:v1.4.2
  canary-steps-csv    Traffic-weight checkpoints for the new revision, e.g.
                       "10,25,50,75,100" (production -- same five-step ramp
                       scripts/blue-green-deploy.sh uses on the VM path) or
                       "100" (staging -- one jump straight to full traffic
                       once healthy, since there's no live traffic on a
                       scale-to-zero app to protect against a gradual shift
                       in the first place)
  step-wait-seconds   How long to hold at each checkpoint before re-checking
                       health and moving to the next one
  public              "true" for an app with external ingress (only
                       "frontend" today) -- enables a direct curl smoke test
                       against the new revision's OWN per-revision FQDN
                       before ANY traffic is shifted to it, on top of the
                       platform-level health probe. "false" (e.g.
                       "backend", internal-only ingress) relies on the
                       platform-level health probe alone -- its per-revision
                       FQDN only resolves inside the app's own VNet, which a
                       GitHub-hosted runner cannot reach, and progressively
                       shifting real traffic through \`frontend\`'s reverse
                       proxy in the run's later steps is what actually
                       exercises it end to end.
EOF
  exit 1
}

# GITHUB_OUTPUT is only set when this script runs as a GitHub Actions step;
# fall back to /dev/null so `status`/manual local runs don't fail on an
# unset variable, and so key=value lines below never leak into stdout that a
# human reads.
GH_OUT="${GITHUB_OUTPUT:-/dev/null}"

# Every revision name Container Apps returns/accepts is fully-qualified
# already (e.g. "backend--bg123-1"), so callers never need to know the
# "<app>--<suffix>" naming convention themselves.

wait_for_revision_healthy() {
  # $1 app  $2 rg  $3 revision  $4 max_tries (10s apiece)  $5 status_script
  # (optional)  $6 check-name prefix (optional, defaults to "$app-waiting")
  #
  # BUG FIX: this used to be silent on the dashboard for its entire
  # duration -- up to 400s at gate 1, up to 60s per gate-3 step -- because
  # nothing in here ever touched aca-deploy-status.sh, only `echo`'d to the
  # GitHub Actions run log. From the dashboard's point of view that looked
  # exactly like "the status bar does nothing," even during a completely
  # normal, still-in-progress wait: the health check log stayed on
  # whichever gate's pass/fail line was written last (or empty, if this is
  # the very first wait of the whole run) with no sign anything was still
  # happening in between. $5/$6 are optional so this function stays fully
  # usable standalone (a laptop, `az login`, no dashboard wiring) with zero
  # behavior change -- a missing/non-executable status_script just skips
  # the heartbeat.
  local app="$1" rg="$2" rev="$3" max_tries="$4"
  local status_script="${5:-}" check_prefix="${6:-${1}-waiting}"
  local i state
  for i in $(seq 1 "$max_tries"); do
    state=$(az containerapp revision show --name "$app" --resource-group "$rg" \
              --revision "$rev" --query "properties.healthState" -o tsv 2>/dev/null)
    echo "  [$i/$max_tries] $rev health: ${state:-<not found yet>}"
    # "pending" (not pass/fail) -- this isn't a verdict yet, just proof the
    # wait loop is still alive and what it's seeing each poll. See
    # aca-deploy-status.sh's `check` subcommand and
    # scripts/deploy-status-aca/index.html's `.dot.pending` for how the
    # dashboard renders this distinctly from an actual pass/fail.
    [ -x "$status_script" ] && "$status_script" check "$check_prefix" "pending" \
      "[$i/$max_tries] $rev health: ${state:-checking...}" 2>/dev/null || true
    if [ "$state" = "Healthy" ]; then return 0; fi
    if [ "$state" = "Unhealthy" ]; then
      # Fail fast on a definitive Unhealthy rather than burning the whole
      # timeout -- Container Apps only reports this after the readiness
      # probe has actually failed enough times to give up, not on the
      # first miss, so there's nothing to be gained by continuing to poll.
      echo "  $rev reported Unhealthy -- not waiting out the rest of the timeout"
      return 1
    fi
    sleep 10
  done
  echo "  $rev did not become Healthy within $((max_tries * 10))s"
  return 1
}

curl_check() {
  # $1 url  $2 label  -- expects 200 or 401 (both prove a real response came
  # back through the whole chain, not just that something answered on 443)
  #
  # BUG FIX: this used to pass `-f`, which makes curl treat ANY HTTP status
  # >=400 -- including the 401 /api/auth/me is SUPPOSED to return here
  # (there's no auth cookie on a bare curl against the revision's own
  # per-revision FQDN) -- as a hard failure (exit 22, "The requested URL
  # returned error: 401"). That's exactly what was rolling back every
  # healthy new revision at gate 2: the check was failing on the very
  # response it was designed to accept. `-f` also suppresses the response
  # body/status on error, which is why the log only ever showed a bare
  # "curl: (22) ..." instead of a status code. Fixed by dropping `-f` and
  # validating the captured status code ourselves -- `--retry` still
  # retries real transient failures (curl itself exiting non-zero: DNS,
  # connection refused/reset, timeout), it just no longer treats a 401 as
  # one of them.
  local url="$1" label="$2"
  local code
  code=$(curl -s -S --connect-timeout 30 --max-time 90 \
       --retry 5 --retry-delay 5 --retry-connrefused --retry-max-time 570 \
       -o /dev/null -w "%{http_code}" "$url") \
    || { echo "  $label: request failed against $url"; return 1; }
  echo "  $label: HTTP $code"
  case "$code" in
    200|401) return 0 ;;
    *) echo "  $label: unexpected status $code (expected 200 or 401) from $url"; return 1 ;;
  esac
}

cmd_rollout() {
  [ "$#" -eq 6 ] || usage
  local app="$1" rg="$2" image="$3" steps_csv="$4" step_wait="$5" public="$6"

  # Best-effort dashboard instrumentation -- a no-op unless
  # deploy-azure-aca.yml already called `aca-deploy-status.sh init` earlier
  # in this same job (see that script's own $STASH mechanism). Wrapped so
  # this script stays fully usable standalone (a laptop, `az login`, no
  # deploy-status wiring at all) with zero behavior change -- a missing
  # status script or an upload hiccup only ever produces a warning here,
  # never fails the actual rollout.
  local status_script="$(dirname "$0")/aca-deploy-status.sh"
  record_check() {
    # record_check <check-name-suffix> <pass|fail> <detail>
    [ -x "$status_script" ] || return 0
    "$status_script" check "${app}-$1" "$2" "$3" 2>/dev/null || true
  }

  local active_rev
  active_rev=$(az containerapp revision list --name "$app" --resource-group "$rg" \
              --query "[?properties.active] | [0].name" -o tsv 2>/dev/null)

  if [ -z "$active_rev" ]; then
    # Brand-new app, nothing live yet -- there is no "green" slot to
    # protect, so there's nothing to blue-green against. Deploy plainly;
    # this first revision picks up the app's default traffic rule (100%
    # to whichever revision is newest -- see infra/main.bicep's
    # `ingress.traffic`) with nothing else to compete with it. It becomes
    # "green" (the active role) from this point on, for the NEXT deploy's
    # purposes.
    echo "No active revision found for '$app' -- first-ever deploy, skipping blue-green."
    az containerapp update --name "$app" --resource-group "$rg" --image "$image" || return 1
    local first_rev
    first_rev=$(az containerapp revision list --name "$app" --resource-group "$rg" \
                  --query "[?properties.active] | [0].name" -o tsv)
    wait_for_revision_healthy "$app" "$rg" "$first_rev" 40 \
      "$status_script" "${app}-first-deploy" || return 1
    { echo "active_revision="; echo "incoming_revision=$first_rev"; echo "skipped=true"; } >> "$GH_OUT"
    return 0
  fi

  echo "Current live revision ('green'): $active_rev"

  # Pin traffic explicitly to the current revision at 100%. Idempotent --
  # harmless to re-run even if it's already pinned from a previous deploy.
  # This is what stops the revision we're about to create from
  # auto-inheriting traffic just for being newest (Container Apps' default
  # `latestRevision: true` rule) -- with an explicit weight set instead, a
  # newly-created revision defaults to 0% until we say otherwise below.
  az containerapp ingress traffic set --name "$app" --resource-group "$rg" \
    --revision-weight "${active_rev}=100" >/dev/null || return 1

  # Unique per workflow run (and per retry attempt of that run), so
  # re-running a deploy -- including redeploying the exact same image tag
  # for a rollback -- never collides with a previous revision's suffix.
  local suffix="bg${GITHUB_RUN_ID:-$(date +%s)}-${GITHUB_RUN_ATTEMPT:-1}"

  echo "Creating incoming ('blue') revision from $image, suffix '$suffix', at 0% traffic..."
  az containerapp update --name "$app" --resource-group "$rg" \
    --image "$image" --revision-suffix "$suffix" >/dev/null || return 1

  local incoming_rev
  incoming_rev=$(az containerapp revision list --name "$app" --resource-group "$rg" \
              --query "sort_by([?properties.active], &properties.createdTime)[-1].name" -o tsv)
  if [ -z "$incoming_rev" ] || [ "$incoming_rev" = "$active_rev" ]; then
    echo "Could not resolve the newly-created revision's name -- aborting before touching traffic."
    return 1
  fi
  echo "Incoming revision: $incoming_rev"

  # GATE 1 -- the platform-level check: Container Apps' own readiness probe
  # (backend: GET /readyz:8000, frontend: GET /:80 -- infra/main.bicep's
  # `probes`) polling the incoming replica directly, the SAME check CI
  # already runs against the built image, run again here against the
  # actual deployed replica as it comes up -- catches anything
  # environment- or runtime-specific that a CI-time check couldn't
  # (missing env var, DB unreachable from this network, etc.). Zero
  # production traffic reaches this revision while this runs -- it's
  # still pinned at 0%.
  echo "Gate 1/3 -- waiting for the incoming revision's own readiness probe..."
  if ! wait_for_revision_healthy "$app" "$rg" "$incoming_rev" 40 \
       "$status_script" "${app}-gate1-waiting"; then
    echo "Incoming revision failed its own health checks before receiving any traffic -- rolling back."
    record_check "gate1-readiness" "fail" "$incoming_rev did not become Healthy"
    az containerapp revision deactivate --name "$app" --resource-group "$rg" --revision "$incoming_rev" >/dev/null 2>&1
    { echo "active_revision=$active_rev"; echo "incoming_revision=$incoming_rev"; echo "skipped=false"; } >> "$GH_OUT"
    return 1
  fi
  record_check "gate1-readiness" "pass" "$incoming_rev healthy at 0% traffic"

  # GATE 2 -- for the one app with external ingress (`frontend`), a real
  # HTTP smoke test against THIS revision's own dedicated FQDN
  # (<app>---<suffix>.<domain>, Container Apps assigns one automatically to
  # every revision once activeRevisionsMode is Multiple) -- still zero
  # production traffic, since nothing routes to this URL except a request
  # sent to it by name. `backend` has no public FQDN to hit this way (see
  # this file's --public description above); gate 1 plus the traffic-shift
  # checks below are what cover it.
  if [ "$public" = "true" ]; then
    local incoming_fqdn
    incoming_fqdn=$(az containerapp revision show --name "$app" --resource-group "$rg" \
                 --revision "$incoming_rev" --query "properties.fqdn" -o tsv)
    echo "Gate 2/3 -- direct smoke test against the incoming revision's own slot (https://$incoming_fqdn)..."
    if ! curl_check "https://$incoming_fqdn/" "incoming revision /" \
       || ! curl_check "https://$incoming_fqdn/api/auth/me" "incoming revision /api/auth/me (through backend proxy)"; then
      echo "Incoming revision failed a direct smoke test on its own slot -- rolling back."
      record_check "gate2-direct-smoke-test" "fail" "https://$incoming_fqdn/ or /api/auth/me did not return 200/401"
      az containerapp revision deactivate --name "$app" --resource-group "$rg" --revision "$incoming_rev" >/dev/null 2>&1
      { echo "active_revision=$active_rev"; echo "incoming_revision=$incoming_rev"; echo "skipped=false"; } >> "$GH_OUT"
      return 1
    fi
    record_check "gate2-direct-smoke-test" "pass" "https://$incoming_fqdn/ and /api/auth/me both returned 200/401"
  else
    echo "Gate 2/3 -- skipped (internal-only ingress; covered by gate 1 and the traffic-shift checks below)."
  fi

  # GATE 3 -- gradual traffic migration. Each checkpoint in steps_csv (e.g.
  # "10,25,50,75,100") is re-verified against the SAME readiness probe used in
  # gate 1 -- a revision that was healthy at 0% can still degrade once it
  # starts carrying real concurrent load, and this is what catches that
  # before it reaches 100%.
  echo "Gate 3/3 -- migrating traffic: $steps_csv"
  local step
  IFS=',' read -ra STEPS <<< "$steps_csv"
  for step in "${STEPS[@]}"; do
    local active_weight=$((100 - step))
    echo "  -> ${incoming_rev}=${step}% / ${active_rev}=${active_weight}%"
    # Deliberately only THIS app's key -- aca-deploy-status.sh's cmd_write
    # merges "apps" into whatever the previous status.json already had
    # (per-app, per-field) instead of overwriting the object wholesale, so
    # this can't blank out the OTHER app's card on the dashboard while this
    # loop runs. See that script's own cmd_write comment for the fix
    # history if this ever needs to change.
    [ -x "$status_script" ] && "$status_script" write "rolling_out_${app}" \
      "\"apps\": {\"$app\": {\"active_revision\": \"$active_rev\", \"incoming_revision\": \"$incoming_rev\", \"traffic_to_incoming_pct\": $step}}" \
      2>/dev/null || true
    az containerapp ingress traffic set --name "$app" --resource-group "$rg" \
      --revision-weight "${active_rev}=${active_weight}" "${incoming_rev}=${step}" >/dev/null || return 1
    sleep "$step_wait"
    if ! wait_for_revision_healthy "$app" "$rg" "$incoming_rev" 6 \
         "$status_script" "${app}-gate3-waiting-${step}pct"; then
      echo "Incoming revision degraded at ${step}% traffic -- rolling back."
      record_check "gate3-traffic-${step}pct" "fail" "$incoming_rev degraded at ${step}% traffic"
      az containerapp ingress traffic set --name "$app" --resource-group "$rg" \
        --revision-weight "${active_rev}=100" "${incoming_rev}=0" >/dev/null 2>&1
      az containerapp revision deactivate --name "$app" --resource-group "$rg" --revision "$incoming_rev" >/dev/null 2>&1
      { echo "active_revision=$active_rev"; echo "incoming_revision=$incoming_rev"; echo "skipped=false"; } >> "$GH_OUT"
      return 1
    fi
    record_check "gate3-traffic-${step}pct" "pass" "$incoming_rev healthy at ${step}% traffic"
  done

  echo "$app is fully migrated to $incoming_rev (100% traffic). $active_rev is left ACTIVE but at 0% traffic -- call 'finalize' once every app in this deploy has reached this point and the end-to-end smoke test has passed. Once finalized, $incoming_rev becomes 'green' (the active role) for the next deploy."
  { echo "active_revision=$active_rev"; echo "incoming_revision=$incoming_rev"; echo "skipped=false"; } >> "$GH_OUT"
  return 0
}

cmd_finalize() {
  [ "$#" -eq 3 ] || usage
  local app="$1" rg="$2" active_rev="$3"
  if [ -z "$active_rev" ]; then
    echo "No previous revision to spin down for '$app' (first-ever deploy) -- nothing to do."
    return 0
  fi
  echo "Spinning down the old (formerly 'green') slot: deactivating $active_rev (already at 0% traffic)."
  az containerapp revision deactivate --name "$app" --resource-group "$rg" --revision "$active_rev"
}

cmd_rollback() {
  [ "$#" -eq 4 ] || usage
  local app="$1" rg="$2" active_rev="$3" incoming_rev="$4"
  if [ -z "$active_rev" ] || [ -z "$incoming_rev" ]; then
    echo "Missing active/incoming revision name for '$app' -- nothing safe to do automatically. Check 'status' and fix traffic weights by hand."
    return 1
  fi
  echo "Rolling back '$app': ${active_rev}=100% / ${incoming_rev}=0%, then deactivating $incoming_rev."
  az containerapp ingress traffic set --name "$app" --resource-group "$rg" \
    --revision-weight "${active_rev}=100" "${incoming_rev}=0" || return 1
  az containerapp revision deactivate --name "$app" --resource-group "$rg" --revision "$incoming_rev" || true
}

cmd_status() {
  [ "$#" -ge 2 ] || usage
  local app="$1" rg="$2" watch="${3:-}"
  local run_once
  run_once() {
    echo "=== $app ($(date -u +%H:%M:%S) UTC) ==="
    az containerapp revision list --name "$app" --resource-group "$rg" \
      --query "reverse(sort_by([], &properties.createdTime))[].{revision:name, active:properties.active, health:properties.healthState, running:properties.runningState, replicas:properties.replicas, traffic:properties.trafficWeight, created:properties.createdTime}" \
      -o table
  }
  if [ "$watch" = "--watch" ]; then
    while true; do
      clear
      run_once
      sleep 5
    done
  else
    run_once
  fi
}

[ "$#" -ge 1 ] || usage
subcommand="$1"; shift
case "$subcommand" in
  rollout)  cmd_rollout "$@" ;;
  finalize) cmd_finalize "$@" ;;
  rollback) cmd_rollback "$@" ;;
  status)   cmd_status "$@" ;;
  *) usage ;;
esac
