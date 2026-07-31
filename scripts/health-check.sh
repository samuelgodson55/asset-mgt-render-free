#!/usr/bin/env bash
# scripts/health-check.sh
# -----------------------------------------------------------------------------
# THE criteria a deploy on the VM path is judged against -- ONE script, TWO
# modes, so the replica slot is held to exactly the same bar the live
# domain already was, not a lesser one:
#
#   --mode internal --slot <blue|green>
#       Run FROM the VM, against a specific slot's containers directly,
#       over the internal Docker network -- bypasses Caddy/production
#       traffic entirely. This is what scripts/blue-green-deploy.sh runs
#       against the INACTIVE slot before it ever receives a real request,
#       and again at each traffic-ramp step.
#
#   --mode external --url <https://domain>
#       Run against the live public domain -- what
#       .github/workflows/deploy-azure-vm.yml runs as its post-cutover
#       smoke test (the direct descendant of that workflow's original
#       "Smoke test" step, unchanged in substance: GET / and GET
#       /api/auth/me).
#
# Checks (mirroring backend/main.py's own /healthz vs /readyz split):
#   backend-healthz   liveness only -- process is up and answering HTTP.
#   backend-readyz    DB reachable AND schema matches this build (see
#                     database.py's get_schema_status()) -- this is what
#                     actually proves `alembic upgrade head` finished and
#                     this replica is safe to receive real requests.
#   frontend-static   nginx is serving the built SPA.
#   frontend-api-wiring   a full round trip -- THIS slot's nginx proxying
#                     to THIS slot's backend -- proving the pairing (not
#                     just each half alone) works. 200 or 401 both count
#                     (no cookie vs. a stale one); anything else (502,
#                     504, connection refused) does not.
#
# Every check retries with a delay before failing -- readyz in particular
# gets a much longer allowance, since a fresh replica may still be running
# `alembic upgrade head` or waiting on a brief DB blip when this first
# runs.
#
# Exit 0 only if EVERY check passes. Prints one JSON line per check to
# stdout (and appends the same line to $DEPLOY_STATUS_FILE, if set) so a
# human or another script can watch progress live -- see
# scripts/blue-green-deploy.sh and DEPLOYMENT_VM.md's "Monitoring a
# rollout" section.
# -----------------------------------------------------------------------------
set -euo pipefail

MODE=""
SLOT=""
COMPOSE_FILE="docker-compose.vm.yml"
URL=""
RETRIES=20
DELAY=5
READYZ_RETRIES=40
READYZ_DELAY=5

usage() {
  cat >&2 <<USAGE
Usage:
  $0 --mode internal --slot <blue|green> [--compose-file docker-compose.vm.yml] [--retries N] [--delay S]
  $0 --mode external --url <https://domain> [--retries N] [--delay S]
USAGE
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --slot) SLOT="$2"; shift 2 ;;
    --compose-file) COMPOSE_FILE="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --retries) RETRIES="$2"; shift 2 ;;
    --delay) DELAY="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

log_check() {
  local name="$1" status="$2" detail="$3"
  local safe_detail line
  # Keep the JSON valid even if a command's output ever contains a quote.
  safe_detail=$(printf '%s' "$detail" | tr -d '\n' | sed 's/"/\\"/g')
  line=$(printf '{"check":"%s","status":"%s","detail":"%s","ts":"%s"}' \
    "$name" "$status" "$safe_detail" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
  echo "$line"
  if [[ -n "${DEPLOY_STATUS_FILE:-}" ]]; then
    echo "$line" >> "$DEPLOY_STATUS_FILE"
  fi
}

# retry <attempts> <delay_seconds> <check_name> -- <command...>
# Runs <command...> up to <attempts> times, <delay_seconds> apart, logging
# exactly one pass/fail check result at the end via log_check.
retry() {
  local attempts="$1" delay="$2" name="$3"; shift 3
  [[ "${1:-}" == "--" ]] && shift
  local n=1
  until "$@"; do
    if (( n >= attempts )); then
      log_check "$name" "fail" "gave up after $n attempt(s)"
      return 1
    fi
    n=$((n + 1))
    sleep "$delay"
  done
  log_check "$name" "pass" "ok after $n attempt(s)"
  return 0
}

case "$MODE" in
  internal)
    [[ -n "$SLOT" ]] || usage
    BACKEND_SVC="backend-$SLOT"
    FRONTEND_SVC="frontend-$SLOT"

    dc_exec() { docker compose -f "$COMPOSE_FILE" exec -T "$@"; }

    check_backend_healthz() {
      dc_exec "$BACKEND_SVC" python3 -c "
import sys, urllib.request
r = urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)
sys.exit(0 if r.status == 200 else 1)
" >/dev/null 2>&1
    }

    check_backend_readyz() {
      # A 503 from /readyz is urllib.error.HTTPError, not a network
      # error -- treat anything other than exactly 200 as "not ready yet"
      # (which is what a 503 means -- see backend/main.py's readyz
      # docstring) rather than a hard failure of the retry loop itself.
      dc_exec "$BACKEND_SVC" python3 -c "
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3)
    sys.exit(0 if r.status == 200 else 1)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code == 200 else 1)
" >/dev/null 2>&1
    }

    check_frontend_static() {
      dc_exec "$FRONTEND_SVC" wget -q -O /dev/null "http://127.0.0.1/" >/dev/null 2>&1
    }

    check_frontend_api_wiring() {
      local out
      out=$(dc_exec "$FRONTEND_SVC" wget -S -O /dev/null "http://127.0.0.1/api/auth/me" 2>&1) || true
      echo "$out" | grep -qE "HTTP/[0-9.]+ +(200|401) "
    }

    echo "== Internal health checks: slot=$SLOT (backend=$BACKEND_SVC frontend=$FRONTEND_SVC) =="
    retry "$RETRIES" "$DELAY" "backend-healthz:$SLOT" -- check_backend_healthz
    retry "$READYZ_RETRIES" "$READYZ_DELAY" "backend-readyz:$SLOT" -- check_backend_readyz
    retry "$RETRIES" "$DELAY" "frontend-static:$SLOT" -- check_frontend_static
    retry "$RETRIES" "$DELAY" "frontend-api-wiring:$SLOT" -- check_frontend_api_wiring
    ;;

  external)
    [[ -n "$URL" ]] || usage
    URL="${URL%/}"

    check_root() {
      curl -f -s -S --connect-timeout 15 --max-time 60 -o /dev/null "$URL/"
    }
    check_auth_me() {
      local code
      code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 15 --max-time 30 "$URL/api/auth/me")
      [[ "$code" == "200" || "$code" == "401" ]]
    }

    echo "== External smoke test: $URL =="
    retry "$RETRIES" "$DELAY" "external-root" -- check_root
    retry "$RETRIES" "$DELAY" "external-auth-me" -- check_auth_me
    ;;

  *)
    usage
    ;;
esac

echo "All checks passed ($MODE${SLOT:+ slot=$SLOT}${URL:+ url=$URL})."
