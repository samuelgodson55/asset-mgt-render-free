#!/usr/bin/env bash
# scripts/ensure-caddy-weights.sh
# -----------------------------------------------------------------------------
# Guarantees caddy/weights.conf exists as a real FILE (not a directory) before
# anything tries to start/reload caddy against it, seeding it at "0 100"
# (100% green, 0% blue -- the fixed active slot, matches the meaning of
# ACTIVE_SLOT=green/COMPOSE_PROFILES=green in .env) if it's missing.
#
# WHY THIS IS ITS OWN SCRIPT, not just inlined where it's needed: it used to
# live only inside scripts/blue-green-deploy.sh, run once at the start of
# every rollout. That's correct for the "weights.conf survived until the next
# deploy" case, but it left a real gap: on a VM REBOOT (Azure host
# maintenance, an AutomaticByPlatform patch reboot -- both expected on this
# deployment target, see cloud-init.yaml's `patch_mode`), systemd's
# snipeit.service runs a plain `docker compose up -d` with NO self-heal at
# all. If /mnt/docker-data (the data disk holding caddy/weights.conf) came up
# mounted a beat later than Docker itself, or weights.conf was otherwise
# missing/corrupted at that moment, Compose's default behavior for a
# bind-mount source that doesn't exist is to auto-create it AS A DIRECTORY --
# caddy then fails to start (`import` can't read a directory), and because
# `cloudflared` depends on caddy (`condition: service_started`), it and
# anything sequenced after it in that same `up -d` are left stuck in
# `Created` -- never started, not even attempted -- taking BOTH the app and
# SSH access down at once (SSH rides the same Cloudflare Tunnel). This is
# exactly the failure mode documented in docs/DEPLOYMENT_VM.md's Quick
# Recovery Runbook. Calling this script from BOTH blue-green-deploy.sh (a
# deploy) and cloud-init.yaml's systemd unit as an ExecStartPre (every boot)
# closes the gap instead of only covering one of the two moments this file
# can go missing.
#
# Usage: ensure-caddy-weights.sh <path-to-repo-root, e.g. /opt/snipeit>
# Exits 0 whether or not it had to do anything. Never fails the caller just
# because the file already existed and was fine (the overwhelmingly common
# case, on both a normal deploy and a normal reboot).
# -----------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="${1:?Usage: ensure-caddy-weights.sh <repo-root>}"
COMPOSE_FILE="docker-compose.vm.yml"
WEIGHTS_FILE="caddy/weights.conf"

cd "$REPO_ROOT"

if [[ -f "$WEIGHTS_FILE" ]]; then
  exit 0
fi

echo "$WEIGHTS_FILE missing or not a regular file -- seeding 100% traffic to green (the fixed active slot)"
mkdir -p "$(dirname "$WEIGHTS_FILE")"
# See blue-green-deploy.sh's original comment (still applies verbatim): a
# bind-mount source Docker auto-created as a directory needs rmdir, not just
# overwriting, before it can become a real file.
[[ -d "$WEIGHTS_FILE" ]] && { rmdir "$WEIGHTS_FILE" 2>/dev/null || true; }
echo "lb_policy weighted_round_robin 0 100" > "$WEIGHTS_FILE"

# Only force-recreate caddy if it's already running under a stale directory
# mount -- on a cold boot (this script running as ExecStartPre, BEFORE
# `docker compose up -d` has created anything yet this boot) there's nothing
# to recreate, and `docker compose ps -q caddy` correctly comes back empty.
if [[ -n "$(docker compose -f "$COMPOSE_FILE" ps -q caddy 2>/dev/null)" ]]; then
  echo "caddy container already exists -- force-recreating so it picks up the real file, not the stale directory mount"
  docker compose -f "$COMPOSE_FILE" up -d --force-recreate --no-deps caddy
fi
