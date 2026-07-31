#!/usr/bin/env bash
# scripts/blue-green-deploy.sh
# -----------------------------------------------------------------------------
# Runs ON THE VM (invoked over SSH by .github/workflows/deploy-azure-vm.yml,
# from /opt/snipeit, after docker-compose.vm.yml/Caddyfile/scripts/* have
# already been synced and IMAGE_TAG already pulled). Drives one full
# zero-downtime blue-green rollout:
#
#   1. Read ACTIVE_SLOT from .env -> compute the other slot (NEW_SLOT).
#   2. Run `alembic upgrade head` against the incoming image, against the
#      one shared `db` -- must stay backward-compatible with the
#      still-running OLD slot for the rest of this rollout.
#   3. Start ONLY backend-$NEW_SLOT / frontend-$NEW_SLOT (+ refresh
#      worker/beat in place, since they aren't behind Caddy at all) --
#      Caddy is still sending 100% of traffic to the OLD slot, so
#      NEW_SLOT gets zero production traffic here.
#   4. scripts/health-check.sh --mode internal against NEW_SLOT directly.
#      Anything short of every check passing aborts the whole rollout
#      (see the `trap` below) -- OLD slot is untouched, NEW_SLOT is
#      stopped.
#   5. Gradually reweight Caddy from OLD->NEW (10/25/50/75/100%),
#      re-running the health check after each step against a live
#      partial-traffic slot, not just the empty-load pre-check.
#   6. Flip ACTIVE_SLOT/COMPOSE_PROFILES in .env (so a future reboot comes
#      back up on NEW_SLOT) and stop+remove the OLD slot's containers.
#
# scripts/deploy-status/status.json is rewritten at every phase transition
# (readable at https://<DOMAIN>/_deploy/ -- see Caddyfile and
# DEPLOYMENT_VM.md's "Monitoring a rollout" section); every individual
# check health-check.sh runs is additionally appended, one JSON line each,
# to scripts/deploy-status/checks.log.
#
# Env vars this script reads (both optional):
#   RAMP_PAUSE_SECONDS  seconds to hold at each traffic-ramp step (default 20)
#   SKIP_MIGRATE        "true" to skip step 2's `alembic upgrade head` --
#                        only safe when no migration changed since
#                        IMAGE_TAG was built (e.g. redeploying the exact
#                        same image, or a config-only change). The rest
#                        of the rollout (replica, health checks, gradual
#                        ramp) still runs in full either way -- this only
#                        ever skips the migration itself, never the
#                        zero-downtime mechanics.
#
# Exit 0 = NEW_SLOT is now active and serving 100% of traffic, OLD_SLOT is
# stopped. Exit non-zero = rollout aborted, OLD_SLOT is confirmed still
# serving 100% of traffic (see the failure trap).
# -----------------------------------------------------------------------------
set -euo pipefail
cd /opt/snipeit

COMPOSE_FILE="docker-compose.vm.yml"
STATUS_DIR="/mnt/docker-data/volumes/deploy_status"
STATUS_JSON="$STATUS_DIR/status.json"
STATUS_LOG="$STATUS_DIR/checks.log"
WEIGHTS_FILE="caddy/weights.conf"
RAMP_STEPS=(10 25 50 75 100)              # % of traffic moved to NEW_SLOT
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
ACTIVE_SLOT="${ACTIVE_SLOT:-blue}"
if [[ "$ACTIVE_SLOT" == "blue" ]]; then NEW_SLOT="green"; else NEW_SLOT="blue"; fi
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
: > "$STATUS_LOG"

# infra-vm/cloud-init.yaml seeds $WEIGHTS_FILE ONCE, on a brand-new VM's
# first boot, at "100 0" (100% ACTIVE_SLOT) -- see that file's own
# comment. It is deliberately NOT re-applied on later deploys (so an
# in-progress rollout's live weights are never stomped -- see
# deploy-azure-vm.yml's sync step comment), which also means a VM
# provisioned before caddy/weights.conf existed in cloud-init, or one
# where the file/directory was later lost some other way, is left with
# nothing to ever create it -- every rollout hits `caddy/weights.conf: No
# such file or directory` at the very first set_weights call and fails
# before doing anything. Reproduce that same one-time seed here instead,
# gated on the file not already existing, so this is a no-op noop on
# every VM that already has it (the normal case) and a one-time
# self-heal on one that doesn't -- matching whatever $ACTIVE_SLOT .env
# already claims is live, not necessarily "blue", so this never
# contradicts containers that are already running.
if [[ ! -f "$WEIGHTS_FILE" ]]; then
  echo "$WEIGHTS_FILE missing or not a regular file -- seeding 100% traffic to current ACTIVE_SLOT ($ACTIVE_SLOT) before starting rollout"
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
  if [[ "$ACTIVE_SLOT" == "blue" ]]; then
    echo "lb_policy weighted_round_robin 100 0" > "$WEIGHTS_FILE"
  else
    echo "lb_policy weighted_round_robin 0 100" > "$WEIGHTS_FILE"
  fi
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
  "active_slot": "$ACTIVE_SLOT",
  "new_slot": "$NEW_SLOT",
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

weights_for_new_slot_pct() {
  # echoes "<blue> <green>" for NEW_SLOT receiving $1 percent of traffic
  local pct="$1" old_pct=$((100 - pct))
  if [[ "$NEW_SLOT" == "blue" ]]; then echo "$pct $old_pct"; else echo "$old_pct $pct"; fi
}

cleanup_on_failure() {
  local ec=$?
  if [[ $ec -ne 0 ]]; then
    echo "::error::blue-green deploy failed (exit $ec) -- restoring 100% traffic to $ACTIVE_SLOT and stopping $NEW_SLOT" >&2
    read -r old_blue old_green <<< "$(weights_for_new_slot_pct 0)"
    set_weights "$old_blue" "$old_green" || echo "::warning::failed to restore caddy weights during rollback" >&2
    docker compose -f "$COMPOSE_FILE" stop "backend-$NEW_SLOT" "frontend-$NEW_SLOT" 2>/dev/null || true
    write_status "failed" "\"exit_code\": $ec"
  fi
}
trap cleanup_on_failure EXIT

echo "Active slot: $ACTIVE_SLOT -> deploying image_tag=${IMAGE_TAG:-unknown} to: $NEW_SLOT"
write_status "starting"

echo "== [1/6] Migrating schema (alembic upgrade head, against the incoming image) =="
if [[ "${SKIP_MIGRATE:-false}" == "true" ]]; then
  echo "SKIP_MIGRATE=true -- skipping (only safe when no migration changed since IMAGE_TAG was built)"
  write_status "migrating" "\"skipped\": true"
else
  write_status "migrating"
  docker compose -f "$COMPOSE_FILE" run --rm "backend-$NEW_SLOT" alembic upgrade head
fi

echo "== [2/6] Starting replica slot ($NEW_SLOT) -- receives NO production traffic yet =="
write_status "starting_replica"
docker compose -f "$COMPOSE_FILE" up -d --no-deps "backend-$NEW_SLOT" "frontend-$NEW_SLOT"
# worker/beat aren't behind Caddy and don't need a blue-green split of
# their own -- refresh them in place now that the new image is confirmed
# migratable. A few seconds of no scheduled-task processing here is
# invisible to end users, unlike an HTTP-serving restart would be.
docker compose -f "$COMPOSE_FILE" up -d --no-deps worker beat

echo "== [3/6] Health-checking $NEW_SLOT directly (never through Caddy) =="
write_status "health_checking"
DEPLOY_STATUS_FILE="$STATUS_LOG" ./scripts/health-check.sh \
  --mode internal --slot "$NEW_SLOT" --compose-file "$COMPOSE_FILE"

echo "== [4/6] Replica healthy -- ramping traffic $ACTIVE_SLOT -> $NEW_SLOT =="
for pct in "${RAMP_STEPS[@]}"; do
  read -r blue_w green_w <<< "$(weights_for_new_slot_pct "$pct")"
  echo "  -> ${pct}% of traffic to $NEW_SLOT (blue=$blue_w green=$green_w)"
  write_status "ramping" "\"traffic_to_new_slot_pct\": $pct"
  set_weights "$blue_w" "$green_w"

  sleep "$RAMP_PAUSE_SECONDS"

  if [[ "$pct" -lt 100 ]]; then
    # Re-verify under real (even if partial) traffic before increasing
    # further -- catches a regression (e.g. a slow leak) that only shows
    # up once the replica is actually handling requests, not just an
    # idle synthetic check.
    DEPLOY_STATUS_FILE="$STATUS_LOG" ./scripts/health-check.sh \
      --mode internal --slot "$NEW_SLOT" --compose-file "$COMPOSE_FILE" \
      --retries 3 --delay 3
  fi
done

echo "== [5/6] Cutover complete: 100% of traffic now on $NEW_SLOT =="
write_status "cutover_complete"

# Flip ACTIVE_SLOT + COMPOSE_PROFILES so a future reboot / plain
# `docker compose up -d` comes back up on NEW_SLOT, not the old one.
if grep -q '^ACTIVE_SLOT=' .env; then
  sed -i "s/^ACTIVE_SLOT=.*/ACTIVE_SLOT=$NEW_SLOT/" .env
else
  echo "ACTIVE_SLOT=$NEW_SLOT" >> .env
fi
if grep -q '^COMPOSE_PROFILES=' .env; then
  sed -i "s/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=$NEW_SLOT/" .env
else
  echo "COMPOSE_PROFILES=$NEW_SLOT" >> .env
fi

echo "== [6/6] Spinning down old slot ($ACTIVE_SLOT) =="
write_status "spinning_down_old" "\"old_slot\": \"$ACTIVE_SLOT\""
docker compose -f "$COMPOSE_FILE" stop "backend-$ACTIVE_SLOT" "frontend-$ACTIVE_SLOT"
docker compose -f "$COMPOSE_FILE" rm -f "backend-$ACTIVE_SLOT" "frontend-$ACTIVE_SLOT"

ACTIVE_SLOT="$NEW_SLOT"   # so a successful write_status below reports correctly
write_status "done"
echo "Blue-green deploy complete. Active slot is now: $NEW_SLOT"
