#!/usr/bin/env bash
# scripts/trace-request.sh
# -----------------------------------------------------------------------------
# FAST PATH FOR "A USER REPORTED AN ERROR -- WHAT HAPPENED?"
#
# Every API response this app returns (success or failure) carries an
# `X-Request-ID` header, and every error body includes a `"request_id"`
# field (see backend/middleware/request_context.py and
# backend/middleware/error_handling.py). Every structured log line in
# every container (backend/worker/beat) is JSON and stamps that SAME
# request_id on it (see backend/logging_config.py) -- and, once
# OTEL_ENABLED=true, the trace/span ID too (`otelTraceID`/`otelSpanID`).
#
# This script is the "I don't even need Jaeger open" fast path: give it
# the ID a user/support ticket/error toast gave you, and it greps the
# live `docker compose logs` output across every backend-side container
# for that exact ID and pretty-prints just those lines -- request
# arriving, every SQL/service log line in between, and the exact
# exception traceback if it errored -- in one shot, in order, whether
# that request/task ran in `backend`, spilled into `worker`, or both.
#
# Works identically for a `request_id` (from an error message/support
# ticket) OR a `trace_id`/`span_id` copied out of the Jaeger UI -- same
# script, same command, no need to remember which kind of ID you have.
#
# USAGE
#   scripts/trace-request.sh <request_id_or_trace_id> [--since 1h] [--follow]
#
# EXAMPLES
#   scripts/trace-request.sh 7c1e2f3a-9b21-4e3a-8f10-2b6c9d4e5f11
#   scripts/trace-request.sh 7c1e2f3a... --since 2h        # search further back
#   scripts/trace-request.sh 7c1e2f3a... --follow           # keep watching live
#
# Run this from the project root (same folder as docker-compose.yml).
# Requires nothing beyond `docker compose` itself; pretty-prints with
# python3 if it's on your PATH, falls back to raw log lines otherwise.
# -----------------------------------------------------------------------------
set -euo pipefail

usage() {
  sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
}

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 1
fi

SEARCH_ID="$1"
shift

SINCE="15m"
FOLLOW=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)
      SINCE="${2:?--since needs a value, e.g. 1h, 30m, 2026-07-25T10:00:00}"
      shift 2
      ;;
    --follow|-f)
      FOLLOW=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# The three processes that ever handle a request/task: the API itself,
# whichever worker replica picked up a Celery job it enqueued, and the
# scheduler that fired a periodic digest -- see docker-compose.yml.
SERVICES=(backend worker beat)

LOGS_CMD=(docker compose logs --no-color --timestamps --since "$SINCE")
if [[ "$FOLLOW" == true ]]; then
  LOGS_CMD+=(--follow)
fi
LOGS_CMD+=("${SERVICES[@]}")

echo "==> Searching ${SERVICES[*]} logs for: $SEARCH_ID  (since $SINCE)" >&2
if [[ "$FOLLOW" == true ]]; then
  echo "==> Following live -- Ctrl-C to stop." >&2
fi
echo >&2

pretty_print() {
  # Each `docker compose logs --timestamps` line looks like:
  #   backend-1  | 2026-07-25T10:00:00.123456789Z {"timestamp": "...", ...}
  # Split off everything before the first "{" (container prefix + Docker's
  # own timestamp) from the JSON payload our own JsonFormatter produced
  # (see backend/logging_config.py), then pretty-print the JSON body.
  while IFS= read -r line; do
    if [[ "$line" != *'{'* ]]; then
      # Not a JSON log line at all (e.g. a container's raw startup banner,
      # or LOG_FORMAT=text was used instead of json) -- show it as-is
      # rather than silently dropping it.
      echo "$line"
      continue
    fi
    prefix="${line%%\{*}"
    json="{${line#*\{}"
    if command -v python3 >/dev/null 2>&1; then
      python3 - "$prefix" "$json" <<'PY'
import json
import sys

prefix, raw = sys.argv[1], sys.argv[2]
try:
    rec = json.loads(raw)
except Exception:
    print(prefix + raw)
    raise SystemExit

ts = rec.get("timestamp", "")
level = rec.get("level", "")
logger = rec.get("logger", "")
msg = rec.get("message", "")
trace_id = rec.get("otelTraceID")
span_id = rec.get("otelSpanID")
skip = {"timestamp", "level", "logger", "message", "request_id",
        "exception", "otelTraceID", "otelSpanID"}
extras = {k: v for k, v in rec.items() if k not in skip}

container = prefix.strip().rstrip("|").strip()
header = f"[{container:<10}] {ts}  {level:<8} {logger}"
if trace_id:
    header += f"  trace={trace_id}"
print(header)
print(f"    {msg}" + (f"  {json.dumps(extras, default=str)}" if extras else ""))
if rec.get("exception"):
    for exc_line in rec["exception"].splitlines():
        print(f"    | {exc_line}")
PY
    else
      echo "$prefix$json"
    fi
  done
}

"${LOGS_CMD[@]}" 2>/dev/null | grep -F "$SEARCH_ID" | pretty_print
