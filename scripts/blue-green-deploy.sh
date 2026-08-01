#!/usr/bin/env bash
# scripts/blue-green-deploy.sh
# -----------------------------------------------------------------------------
# Runs ON THE VM (invoked over SSH by .github/workflows/deploy-azure-vm.yml,
# from /opt/snipeit, after docker-compose.vm.yml/Caddyfile/scripts/* have
# already been synced and IMAGE_TAG already pulled). Drives one full
# zero-downtime blue-green rollout.
#
# FIXED ROLES -- BLUE IS ALWAYS THE INCOMING DEPLOY, GREEN IS ALWAYS ACTIVE:
# Earlier versions of this script let "blue" and "green" alternate roles
# from one deploy to the next (whichever slot was idle became the incoming
# target; whichever was serving stayed "active", regardless of which color
# it happened to be). That meant a given color's meaning depended on deploy
# history -- you couldn't tell what "blue" meant right now without also
# knowing what the last deploy did. Roles are now FIXED instead: every
# rollout always deploys the new image into `backend-blue`/`frontend-blue`
# (BLUE = "the candidate currently being validated"), and once every gate
# passes, `backend-green`/`frontend-green` (GREEN = "the slot actually
# serving production traffic") is brought up to the SAME image and takes
# over. Blue is stopped again once green has taken over, so blue is always
# idle between deploys -- ready to receive the next incoming image -- and
# green is always what's currently live. This is what "the naming is
# always unique" buys you: `docker compose -f docker-compose.vm.yml ps
# backend-green` (or the dashboard at /_deploy/) always answers "what's
# live right now" with no need to also check which slot most recently won
# a rollout.
#
#   1. Migrate -- `alembic upgrade head` against the incoming image, run
#      through `backend-blue` (against the one shared `db`; must stay
#      backward-compatible with the still-running green slot for the rest
#      of this rollout).
#   2. Start ONLY backend-blue/frontend-blue (+ refresh worker/beat in
#      place, since they aren't behind Caddy at all) -- Caddy is still
#      sending 100% of traffic to green, so blue gets zero production
#      traffic here.
#   3. scripts/health-check.sh --mode internal against blue directly.
#      Anything short of every check passing aborts the whole rollout (see
#      the `trap` below) -- green is untouched, blue is stopped.
#   4. Gradually reweight Caddy from green->blue (10/25/50/75/100%),
#      re-running the health check after each step against a live
#      partial-traffic slot, not just the empty-load pre-check.
#   5. PROMOTE: now that blue has proven itself under 100% real traffic,
#      bring green up on the exact same image blue is already running
#      (green shares `docker-compose.vm.yml`'s `${IMAGE_TAG}` reference
#      with blue -- see that file's own comment -- so this starts the
#      identical build, not a separate deploy), health-check it
#      internally, then flip Caddy's weight straight back to 100% green /
#      0% blue -- both slots are running the identical, already-proven
#      image at this point, so this is a same-code swap, not a second
#      canary. Finally stop+remove blue's containers so it's idle again,
#      ready for the next incoming image.
#
# NOTE ON .env: earlier versions of this script flipped ACTIVE_SLOT/
# COMPOSE_PROFILES in .env at the end of every rollout, so a reboot (or a
# bare `docker compose up -d`) would come back up on whichever slot had
# just won. With roles now fixed, that's no longer necessary at all --
# COMPOSE_PROFILES is simply "green", permanently, set once by
# infra-vm/cloud-init.yaml on first boot and never touched again by this
# script. A reboot always comes back up on green (the fixed active role)
# with no rollout needing to run first, exactly like before -- it just no
# longer needs this script's help to stay correct.
#
# scripts/deploy-status/status.json is rewritten at every phase transition
# (readable at https://<DOMAIN>/_deploy/ -- see Caddyfile and
# DEPLOYMENT_VM.md's "Monitoring a rollout" section); every individual
# check health-check.sh runs is additionally appended, one JSON line each,
# to scripts/deploy-status/checks.log.
#
# Env vars this script reads (both optional):
#   RAMP_PAUSE_SECONDS  seconds to hold at each traffic-ramp step (default 20)
#   SKIP_MIGRATE        "true" to skip step 1's `alembic upgrade head` --
#                        only safe when no migration changed since
#                        IMAGE_TAG was built (e.g. redeploying the exact
#                        same image, or a config-only change). The rest
#                        of the rollout (replica, health checks, gradual
#                        ramp, promotion) still runs in full either way --
#                        this only ever skips the migration itself, never
#                        the zero-downtime mechanics.
#
# Exit 0 = green is now active and serving 100% of traffic on the new
# image, blue is stopped. Exit non-zero = rollout aborted. If the abort
# happened before step 5 (promotion), green is confirmed still serving
# 100% of traffic on whatever it was already running (see the failure
# trap) -- exactly the old guarantee. If it happened DURING promotion
# (green's own health check failed after blue had already proven itself
# at 100% traffic), the trap deliberately does NOT tear blue down --
# leaving the already-proven new image serving traffic (on blue, for now)
# beats reverting to green's old, potentially-stale image. That state is
# flagged loudly (a non-zero exit, and status.json's phase left as
# "promotion_failed", not "done") specifically because it's the one case
# where "green = active" doesn't hold until the next successful deploy
# re-runs promotion -- see cleanup_on_failure's own comment below.
# -----------------------------------------------------------------------------
set -euo pipefail
cd /opt/snipeit

COMPOSE_FILE="docker-compose.vm.yml"
STATUS_DIR="/mnt/docker-data/volumes/deploy_status"
STATUS_JSON="$STATUS_DIR/status.json"
STATUS_LOG="$STATUS_DIR/checks.log"
WEIGHTS_FILE="caddy/weights.conf"
RAMP_STEPS=(10 25 50 75 100)              # % of traffic moved to blue during the canary ramp
RAMP_PAUSE_SECONDS="${RAMP_PAUSE_SECONDS:-20}"

mkdir -p "$STATUS_DIR"

# --- load current state ------------------------------------------------------
# Deliberately NOT a literal `source .env` / `. .env` anymore. That runs
# .env as actual shell code, so any value containing whitespace that isn't
# quoted (e.g. an old/hand-edited `SITE_NAME=Snipe-IT Lite`) gets word-split
# and bash tries to RUN the leftover word as a command -- e.g.
# ".env: line 31: Lite: command not found" -- which aborts this whole
# script under `set -euo pipefail` before the rollout ever starts. This
# repo's own infra-vm/cloud-init.yaml and sync-secrets-vm.yml both already
# quote every free-text value for exactly this reason when they WRITE
# .env, but that only protects `.env` at the moment those scripts run --
# it does nothing for a `.env` that already exists unquoted (created
# before that fix, or hand-edited on the VM directly), which is the
# failure mode this loop guards against instead: it reads .env as plain
# KEY=VALUE data (never executed as shell), so a value's internal spaces
# can never be parsed as a separate command, quoted or not.
while IFS='=' read -r _env_key _env_value; do
  # Skip blank lines and comments; a bare `#` first char is enough since
  # .env never legitimately has a key starting with `#`.
  [[ -z "$_env_key" || "$_env_key" == \#* ]] && continue
  # Strip one layer of surrounding double or single quotes, if present,
  # so already-quoted values (the common/correct case) come out the same
  # as before rather than keeping their literal quote characters.
  if [[ "$_env_value" == \"*\" || "$_env_value" == \'*\' ]]; then
    _env_value="${_env_value:1:-1}"
  fi
  export "$_env_key=$_env_value"
done < .env
unset _env_key _env_value
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
: > "$STATUS_LOG"

# infra-vm/cloud-init.yaml seeds $WEIGHTS_FILE ONCE, on a brand-new VM's
# first boot, at "0 100" (100% green -- see that file's own comment). It
# is deliberately NOT re-applied on later deploys (so an in-progress
# rollout's live weights are never stomped -- see deploy-azure-vm.yml's
# sync step comment), which also means a VM provisioned before
# caddy/weights.conf existed in cloud-init, or one where the
# file/directory was later lost some other way, is left with nothing to
# ever create it -- every rollout hits `caddy/weights.conf: No such file
# or directory` at the very first set_weights call and fails before doing
# anything. Reproduce that same one-time seed here instead, gated on the
# file not already existing, so this is a no-op on every VM that already
# has it (the normal case) and a one-time self-heal on one that doesn't.
# Unlike the old alternating scheme, this is no longer conditional on
# whatever .env claims is "active" -- green is ALWAYS the fixed active
# role, so the seed value is always "0 100" (0% blue / 100% green).
if [[ ! -f "$WEIGHTS_FILE" ]]; then
  echo "$WEIGHTS_FILE missing or not a regular file -- seeding 100% traffic to green (the fixed active slot) before starting rollout"
  mkdir -p "$(dirname "$WEIGHTS_FILE")"
  # Docker's default behavior for a bind-mount source that doesn't exist
  # on the host at container-start time is to auto-create it AS A
  # DIRECTORY (it can't know it was meant to be a file) -- which is
  # exactly what caddy's own "File to import not found" error means:
  # /etc/caddy/snippets/weights.conf inside the container is that same
  # empty directory, not a file Caddy's `import` can read. `[[ ! -f ]]`
  # above is already false for a directory too, so this branch catches
  # that case as well as truly-missing; rmdir only removes it if it's
  # actually empty (the normal case for something Docker auto-created
  # and nothing has written to since) -- anything else here is
  # unexpected and left alone rather than force-deleted.
  [[ -d "$WEIGHTS_FILE" ]] && { rmdir "$WEIGHTS_FILE" 2>/dev/null || true; }
  echo "lb_policy weighted_round_robin 0 100" > "$WEIGHTS_FILE"
  # A directory-to-file swap underneath an already-established bind
  # mount isn't reliably picked up by a container that's already
  # running -- `caddy reload` alone (what set_weights normally does)
  # re-reads Caddy's config from within the SAME stale mount, not a
  # fresh one. Recreating (not just restarting) `caddy` here forces
  # Docker to re-resolve the mount against the real file that now
  # exists. A brief blip on this one self-heal is a one-time cost;
  # every deploy after this one lands on a VM that already has a real
  # $WEIGHTS_FILE and never takes this branch again.
  docker compose -f "$COMPOSE_FILE" up -d --force-recreate --no-deps caddy
fi

write_status() {
  # write_status <phase> [extra_json_fields_without_braces]
  local phase="$1" extra="${2:-}"
  cat > "$STATUS_JSON" <<JSON
{
  "phase": "$phase",
  "active_slot": "green",
  "incoming_slot": "blue",
  "image_tag": "${IMAGE_TAG:-unknown}",
  "started_at": "$STARTED_AT",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"${extra:+,
  $extra}
}
JSON
}

set_weights() {
  # set_weights <blue_weight> <green_weight>
  echo "lb_policy weighted_round_robin $1 $2" > "$WEIGHTS_FILE"
  docker compose -f "$COMPOSE_FILE" exec -T caddy \
    caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
}

weights_for_blue_pct() {
  # echoes "<blue> <green>" for blue receiving $1 percent of traffic.
  # Blue is ALWAYS the incoming/canary slot now, so unlike the old
  # alternating scheme this never needs to check which color is "new" --
  # it always is blue.
  local pct="$1"
  echo "$pct $((100 - pct))"
}

# Tracks how far the rollout got, purely so cleanup_on_failure (below) can
# tell "blue never proved itself" apart from "blue proved itself and
# green's promotion is what failed" -- those two failures need opposite
# responses (roll back to green vs. deliberately leave blue serving).
ROLLOUT_PHASE="pre_ramp"

cleanup_on_failure() {
  local ec=$?
  if [[ $ec -ne 0 ]]; then
    if [[ "$ROLLOUT_PHASE" == "pre_ramp" ]]; then
      echo "::error::blue-green deploy failed (exit $ec) before blue ever reached 100% traffic -- restoring 100% traffic to green and stopping blue" >&2
      set_weights 0 100 || echo "::warning::failed to restore caddy weights during rollback" >&2
      docker compose -f "$COMPOSE_FILE" stop backend-blue frontend-blue 2>/dev/null || true
      write_status "failed" "\"exit_code\": $ec"
    else
      # ROLLOUT_PHASE == "promoting": blue already reached 100% traffic
      # and passed every canary check -- it's the known-good image at
      # this point, green is what's stale. Reverting to green here would
      # mean throwing away a proven-good deploy in favor of whatever
      # green was last running, for no safety benefit (green's own
      # health check is what just failed, so flipping traffic TO it
      # would risk an actual outage instead of preventing one).
      # Deliberately leaves Caddy's weights and both slots' containers
      # exactly as they are -- 100% of traffic keeps flowing to blue,
      # uninterrupted -- and just reports the failure loudly so it gets
      # fixed (re-running this workflow will redeploy blue with the same
      # or a newer image and retry promotion; "green = active" resumes
      # holding again the next time promotion succeeds).
      echo "::error::blue-green deploy failed (exit $ec) DURING promotion -- blue already proved itself at 100% traffic and is left serving; green's promotion to the new image did not complete. Traffic is NOT being flipped back -- see this script's own top-of-file comment for why. Re-run the deploy to retry promotion." >&2
      write_status "promotion_failed" "\"exit_code\": $ec"
    fi
  fi
}
trap cleanup_on_failure EXIT

echo "Active (green) -> deploying image_tag=${IMAGE_TAG:-unknown} to incoming slot (blue)"
write_status "starting"

echo "== [1/6] Migrating schema (alembic upgrade head, against the incoming image) =="
if [[ "${SKIP_MIGRATE:-false}" == "true" ]]; then
  echo "SKIP_MIGRATE=true -- skipping (only safe when no migration changed since IMAGE_TAG was built)"
  write_status "migrating" "\"skipped\": true"
else
  write_status "migrating"
  docker compose -f "$COMPOSE_FILE" run --rm backend-blue alembic upgrade head
fi

echo "== [2/6] Starting incoming slot (blue) -- receives NO production traffic yet =="
write_status "starting_replica"
docker compose -f "$COMPOSE_FILE" up -d --no-deps backend-blue frontend-blue
# worker/beat aren't behind Caddy and don't need a blue-green split of
# their own -- refresh them in place now that the new image is confirmed
# migratable. A few seconds of no scheduled-task processing here is
# invisible to end users, unlike an HTTP-serving restart would be.
docker compose -f "$COMPOSE_FILE" up -d --no-deps worker beat

echo "== [3/6] Health-checking blue directly (never through Caddy) =="
write_status "health_checking"
DEPLOY_STATUS_FILE="$STATUS_LOG" ./scripts/health-check.sh \
  --mode internal --slot blue --compose-file "$COMPOSE_FILE"

echo "== [4/6] Replica healthy -- ramping traffic green -> blue =="
for pct in "${RAMP_STEPS[@]}"; do
  read -r blue_w green_w <<< "$(weights_for_blue_pct "$pct")"
  echo "  -> ${pct}% of traffic to blue (blue=$blue_w green=$green_w)"
  write_status "ramping" "\"traffic_to_incoming_pct\": $pct"
  set_weights "$blue_w" "$green_w"

  sleep "$RAMP_PAUSE_SECONDS"

  if [[ "$pct" -lt 100 ]]; then
    # Re-verify under real (even if partial) traffic before increasing
    # further -- catches a regression (e.g. a slow leak) that only shows
    # up once the replica is actually handling requests, not just an
    # idle synthetic check.
    DEPLOY_STATUS_FILE="$STATUS_LOG" ./scripts/health-check.sh \
      --mode internal --slot blue --compose-file "$COMPOSE_FILE" \
      --retries 3 --delay 3
  fi
done

echo "== [5/6] Blue fully proven at 100% traffic -- promoting: bringing green up on the same image =="
ROLLOUT_PHASE="promoting"
write_status "promoting" "\"traffic_to_incoming_pct\": 100"
# Same $IMAGE_TAG reference blue is already running (see
# docker-compose.vm.yml's own comment on backend-blue/backend-green
# sharing one `image:` value) -- this is a same-code restart, not a
# second canary. Zero production risk from THIS step itself: blue is
# still serving 100% of traffic throughout.
docker compose -f "$COMPOSE_FILE" up -d --no-deps backend-green frontend-green

echo "== Health-checking green directly before flipping traffic back to it =="
DEPLOY_STATUS_FILE="$STATUS_LOG" ./scripts/health-check.sh \
  --mode internal --slot green --compose-file "$COMPOSE_FILE"

echo "== Flipping 100% of traffic back to green =="
write_status "promoting" "\"traffic_to_incoming_pct\": 0"
set_weights 0 100

echo "== [6/6] Green is active on the new image -- spinning down blue (now idle again) =="
write_status "spinning_down_incoming"
docker compose -f "$COMPOSE_FILE" stop backend-blue frontend-blue
docker compose -f "$COMPOSE_FILE" rm -f backend-blue frontend-blue

write_status "done"
echo "Blue-green deploy complete. Green is active on image_tag=${IMAGE_TAG:-unknown}; blue is idle, ready for the next deploy."
