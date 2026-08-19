#!/usr/bin/env bash
# Manage the optional local/VM Jaeger telemetry service without touching the
# application services. This script deliberately never runs `docker compose
# down`, never recreates backend/frontend/db/redis, and never changes the
# active blue/green slot on the Terraform VM.
#
# IMPORTANT:
#   OTEL_ENABLED is a process-start gate for the application's OpenTelemetry
#   instrumentation. Set it to true before starting/redeploying the app if
#   you want application spans. This script only manages Jaeger itself.
#
# Usage:
#   ./scripts/telemetry.sh on
#   ./scripts/telemetry.sh off
#   ./scripts/telemetry.sh status
#   ./scripts/telemetry.sh logs
#   ./scripts/telemetry.sh ui
#
# The target is detected automatically:
#   * /opt/snipeit -> Terraform VM deployment
#   * any other repository checkout -> local Docker Compose
#
# This intentionally has no "local"/"vm" suffix in the normal workflow.

set -Eeuo pipefail

ACTION="${1:-status}"

case "$ACTION" in
  on|off|status|logs|ui) ;;
  *)
    echo "Usage: $0 {on|off|status|logs|ui}" >&2
    exit 2
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but was not found in PATH." >&2
  exit 1
fi

# Resolve the repository directory so the script behaves the same when called
# as ./scripts/telemetry.sh from the repo root or with an absolute script path.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# The Terraform VM is always deployed at /opt/snipeit. Everything else is
# treated as a normal local checkout. This keeps the command identical in both
# environments while avoiding a fragile hostname/IP based detection rule.
if [[ "$REPO_ROOT" == "/opt/snipeit" ]]; then
  TARGET="vm"
  COMPOSE_FILE="docker-compose.vm.yml"
else
  TARGET="local"
  COMPOSE_FILE="docker-compose.yml"
fi

UI_URL="http://localhost:16686"

compose() {
  docker compose -f "$COMPOSE_FILE" --profile tracing "$@"
}

# Read only OTEL_ENABLED from .env. We intentionally do not `source` the whole
# .env because environment files can contain values that are unsafe to execute
# as shell code. Compose remains the authority for all container environment
# interpolation.
otel_enabled="false"
if [[ -f .env ]]; then
  raw_value="$(sed -nE 's/^[[:space:]]*OTEL_ENABLED[[:space:]]*=[[:space:]]*([^[:space:]#]+).*$/\1/p' .env | tail -n 1 || true)"
  if [[ "${raw_value,,}" == "true" ]]; then
    otel_enabled="true"
  fi
fi

jaeger_exists=false
jaeger_running=false

if compose ps -a --format '{{.Service}} {{.State}}' jaeger 2>/dev/null | grep -q '^jaeger '; then
  jaeger_exists=true
fi

if compose ps --format '{{.Service}} {{.State}}' jaeger 2>/dev/null | grep -q '^jaeger running$'; then
  jaeger_running=true
fi

case "$ACTION" in
  on)
    # Never silently enable application tracing. If the application was
    # started with OTEL_ENABLED=false, starting Jaeger alone cannot make that
    # already-running process begin exporting spans.
    if [[ "$otel_enabled" != "true" ]]; then
      cat >&2 <<'EOF'
Telemetry is currently disabled by OTEL_ENABLED=false (or it is unset).

This command intentionally does NOT change the application environment or
restart application services. Set OTEL_ENABLED=true in the deployment's
environment and start/redeploy the application normally first; then run:

  ./scripts/telemetry.sh on

This safety rule prevents the telemetry helper from changing application
behavior or touching an active VM blue/green deployment.
EOF
      exit 1
    fi

    echo "Starting Jaeger only (${TARGET}). Application services are untouched."
    compose up -d --no-deps jaeger
    echo
    echo "Jaeger is running. UI: ${UI_URL}"
    ;;

  off)
    echo "Stopping/removing Jaeger only (${TARGET}). Application services are untouched."

    # `stop` is harmless when the service is already stopped. `rm -f` removes
    # the stopped profile container so `docker compose ps` does not leave stale
    # telemetry state behind. Neither command touches non-Jaeger services.
    compose stop jaeger >/dev/null 2>&1 || true
    compose rm -f jaeger >/dev/null 2>&1 || true

    echo "Telemetry infrastructure is stopped."
    if [[ "$otel_enabled" == "true" ]]; then
      echo
      echo "NOTE: the running application was started with OTEL_ENABLED=true."
      echo "It remains operational, but its exporter will have no local Jaeger"
      echo "destination. To fully disable application instrumentation, set"
      echo "OTEL_ENABLED=false and use the normal application restart/deployment"
      echo "path. This helper deliberately does not restart application services."
    fi
    ;;

  status)
    echo "Telemetry target : ${TARGET}"
    echo "Compose file     : ${COMPOSE_FILE}"
    echo "OTEL_ENABLED     : ${otel_enabled}"
    echo "Jaeger container  : $([[ "$jaeger_exists" == true ]] && echo present || echo absent)"
    echo "Jaeger running    : $([[ "$jaeger_running" == true ]] && echo yes || echo no)"
    echo
    if [[ "$jaeger_running" == true ]]; then
      echo "Jaeger UI        : ${UI_URL}"
    fi
    ;;

  logs)
    # Logs are scoped to Jaeger explicitly. `--tail` prevents an accidental
    # multi-megabyte historical log dump on a shared VM.
    compose logs --tail 200 jaeger
    ;;

  ui)
    if [[ "$TARGET" == "local" ]]; then
        echo "Jaeger UI: ${UI_URL}"
        echo "Quick error search: ${UI_URL}/search?lookback=1h&limit=50&tags=%7B%22error%22%3Atrue%7D"
    else
      echo "Terraform VM Jaeger is SSH-only."
      echo
      echo "From your workstation:"
      echo "  ssh -L 16686:127.0.0.1:16686 <ssh-user>@<vm-host>"
      echo
      echo "Then open:"
      echo "  ${UI_URL}"
      echo
      echo "Quick error search:"
      echo "  ${UI_URL}/search?lookback=1h&limit=50&tags=%7B%22error%22%3Atrue%7D"
      echo
      echo "The VM Compose file does not publish Jaeger's UI/OTLP ports publicly."
    fi
    ;;
esac
