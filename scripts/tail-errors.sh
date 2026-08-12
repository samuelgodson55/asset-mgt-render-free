#!/usr/bin/env bash
# scripts/tail-errors.sh
# -----------------------------------------------------------------------------
# ZERO-DOWNTIME FAST PATH: watch for errors AS THEY HAPPEN, across every
# backend-side container, and surface the exact `request_id` (and trace
# ID, once OTEL_ENABLED=true) you need to hand to scripts/trace-request.sh
# a second later -- instead of waiting for a user to report something and
# then going hunting through logs after the fact.
#
# Run this in its own terminal (or tmux pane) while you're deploying,
# load-testing, or just keeping an eye on things:
#
#   scripts/tail-errors.sh
#
# Every line printed is a real ERROR/CRITICAL log record (an unhandled
# exception, a failed export job, an SMTP send failure, ...) with its
# request_id/trace_id pulled to the front. Copy either ID straight into:
#
#   scripts/trace-request.sh <that id>
#
# to pull the FULL story (every log line from that one request, in
# order, across every container it touched) in one more command.
#
# Requires nothing beyond `docker compose` itself; pretty-prints with
# python3 if it's on your PATH, falls back to raw log lines otherwise.
# -----------------------------------------------------------------------------
set -euo pipefail

RED='\033[31m'
DIM='\033[2m'
RESET='\033[0m'

SERVICES=(backend worker beat)

echo "==> Watching ${SERVICES[*]} for ERROR/CRITICAL log lines. Ctrl-C to stop." >&2
echo >&2

docker compose logs --no-color --timestamps --follow "${SERVICES[@]}" 2>/dev/null \
  | grep -E '"level":\s*"(ERROR|CRITICAL)"' \
  | while IFS= read -r line; do
      if [[ "$line" != *'{'* ]]; then
        echo "$line"
        continue
      fi
      prefix="${line%%\{*}"
      json="{${line#*\{}"
      if command -v python3 >/dev/null 2>&1; then
        python3 - "$prefix" "$json" <<'PY'
import json
import sys

RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

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
rid = rec.get("request_id") or "-"
trace_id = rec.get("otelTraceID")
container = prefix.strip().rstrip("|").strip()

header = f"{RED}[{container:<10}] {ts}  {level:<8} req={rid}"
if trace_id:
    header += f" trace={trace_id}"
header += f"{RESET}"
print(header)
print(f"  {logger}: {msg}")
if rec.get("exception"):
    print(f"{DIM}", end="")
    for exc_line in rec["exception"].splitlines():
        print(f"    | {exc_line}")
    print(f"{RESET}", end="")
print()
PY
      else
        echo -e "${RED}${prefix}${json}${RESET}"
      fi
    done
