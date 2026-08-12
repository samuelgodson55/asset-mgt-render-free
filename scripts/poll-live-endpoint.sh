#!/usr/bin/env bash
# scripts/poll-live-endpoint.sh
# -----------------------------------------------------------------------------
# THE CLIENT'S-EYE VIEW of a blue-green rollout, separate from and
# complementary to `scripts/deploy-status/` (VM) and `aca-blue-green.sh
# status --watch` (ACA). Those two show the DEPLOYMENT'S OWN state -- which
# slot/revision is being weighted, whether ITS health checks pass. Neither
# proves a real request against the live domain was never dropped, delayed,
# or errored while that was happening -- only an independent poller hitting
# the SAME URL a real user would, on its own clock, running the whole time
# traffic is moving, can prove that. This script is that independent poller.
#
# It does nothing blue-green-deploy.sh or aca-blue-green.sh doesn't already
# gate on internally -- this is deliberately NOT another health/readiness
# check, and deliberately does not talk to Caddy weights, Container Apps
# revisions, or docker compose at all. It only ever does one thing: hit a
# public URL on a timer, exactly the way a browser tab left open during the
# swap would, and keep an honest record of every response it got.
#
# USAGE
#   scripts/poll-live-endpoint.sh --url <https://domain/path> [options]
#
# OPTIONS
#   --url URL          Required. The live, public endpoint to poll -- the
#                       same one real users hit (VM: your domain's `/`;
#                       ACA: the `frontend` app's public FQDN). Do NOT point
#                       this at a slot/revision's own direct FQDN -- that
#                       defeats the point, since production traffic doesn't
#                       go there either.
#   --interval SECONDS Seconds between requests. Default: 1.
#   --duration SECONDS Stop automatically after this many seconds. Default:
#                       run until Ctrl-C. Set this to comfortably exceed
#                       your ramp's total wall-clock time (RAMP_PAUSE_SECONDS
#                       x 5 steps on the VM path, step-wait-seconds x however
#                       many entries in canary-steps-csv on ACA) so the poll
#                       is still running for the entire swap, not just part
#                       of it.
#   --out FILE          Where to write the CSV log. Default:
#                       poll-<timestamp>.csv in the current directory.
#   --expect-codes CSV  HTTP status codes that count as a PASS. Default:
#                       "200". Add others (e.g. "200,301,302") only if your
#                       endpoint legitimately returns them at rest -- don't
#                       widen this just to make failures go away.
#
# OUTPUT
#   One CSV line per request: timestamp,http_code,time_total_seconds,result
#   `result` is PASS, FAIL (got a response, wrong code), or ERROR (curl
#   itself failed -- connection refused/reset, DNS, timeout: the request
#   never got a response at all, which is a worse sign than a clean non-200).
#   Printed live to the terminal AND appended to --out as it runs, so you
#   can watch it during the swap and still have the file afterward.
#
#   On exit (Ctrl-C or --duration elapsed), prints a summary: total
#   requests, pass/fail/error counts, and -- if anything other than PASS
#   happened -- the exact timestamped lines for each one, so you can line
#   them up against scripts/deploy-status/status.json's phase transitions
#   (VM) or the GITHUB_STEP_SUMMARY traffic-weight table (ACA) and see
#   exactly which ramp step (if any) a failure lines up with.
#
#   Exit code: 0 if every request PASSed, 1 if anything didn't. Safe to use
#   as a CI gate on its own, but its real job is producing the evidence
#   artifact the capstone brief asks for -- attach the CSV (and this
#   summary) to your submission alongside the deploy-status/ACA status
#   screenshots.
#
# EXAMPLES
#   # Manual: run in its own terminal, Ctrl-C once the deploy is done
#   scripts/poll-live-endpoint.sh --url https://assets.example.com/
#
#   # Timed to safely outlast a 5-step, 20s-per-step VM ramp (100s minimum)
#   scripts/poll-live-endpoint.sh --url https://assets.example.com/ \
#     --duration 150 --out evidence/vm-rollout-$(date +%Y%m%d-%H%M%S).csv
#
#   # Against the ACA production frontend, matched to a 5-step x 15s ramp
#   scripts/poll-live-endpoint.sh --url https://frontend.<env>.azurecontainerapps.io/ \
#     --duration 120
# -----------------------------------------------------------------------------
set -uo pipefail # deliberately NOT -e -- a single failed request must not kill the poller

URL=""
INTERVAL=1
DURATION=0        # 0 = run until Ctrl-C
OUT=""
EXPECT_CODES="200"

usage() {
  cat >&2 <<USAGE
Usage: $0 --url <https://domain/path> [--interval SECONDS] [--duration SECONDS] [--out FILE] [--expect-codes "200,301"]
USAGE
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --expect-codes) EXPECT_CODES="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n "$URL" ]] || usage
[[ -n "$OUT" ]] || OUT="poll-$(date -u +%Y%m%dT%H%M%SZ).csv"

# Comma-separated expected codes -> a `|`-joined regex for a quick membership
# test below (bash has no native "is X in this list" for strings).
EXPECT_REGEX="^($(echo "$EXPECT_CODES" | tr ',' '|'))$"

mkdir -p "$(dirname "$OUT")" 2>/dev/null || true
if [[ ! -f "$OUT" ]]; then
  echo "timestamp,http_code,time_total_seconds,result" > "$OUT"
fi

TOTAL=0
PASS=0
FAIL=0
ERR=0
declare -a FAILURE_LINES=()

START_EPOCH=$(date +%s)

print_summary() {
  echo
  echo "==> Polled $URL every ${INTERVAL}s for ${TOTAL} requests"
  echo "    PASS:  $PASS"
  echo "    FAIL:  $FAIL   (got a response, unexpected status code)"
  echo "    ERROR: $ERR   (no response at all -- connection refused/reset, DNS, timeout)"
  if (( FAIL + ERR > 0 )); then
    echo
    echo "    Non-PASS requests (line these up against status.json's phase"
    echo "    transitions or the ACA traffic-weight summary to see which ramp"
    echo "    step, if any, this happened at):"
    for line in "${FAILURE_LINES[@]}"; do
      echo "      $line"
    done
    echo
    echo "    Evidence written to: $OUT"
    exit 1
  else
    echo
    echo "    Zero dropped/failed requests for the full polling window."
    echo "    Evidence written to: $OUT"
    exit 0
  fi
}
trap print_summary EXIT INT TERM

echo "==> Polling $URL every ${INTERVAL}s. Ctrl-C to stop and print a summary." >&2
echo "    Expecting HTTP: $EXPECT_CODES" >&2
echo "    Logging to: $OUT" >&2
echo >&2

while true; do
  if (( DURATION > 0 )) && (( $(date +%s) - START_EPOCH >= DURATION )); then
    break
  fi

  TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # `-o /dev/null -w` gives us the status code and timing with no response
  # body handling -- exactly what a Ctrl-C-friendly loop needs. No `-f`
  # (see scripts/health-check.sh / aca-blue-green.sh's curl_check for why
  # that flag is the wrong call here too): a non-2xx is data we want to
  # SEE and log, not an exit code curl swallows before we can record it.
  RESPONSE=$(curl -s -o /dev/null -w "%{http_code} %{time_total}" \
              --connect-timeout 5 --max-time 10 "$URL" 2>/dev/null)
  CURL_EXIT=$?

  TOTAL=$((TOTAL + 1))

  if [[ $CURL_EXIT -ne 0 ]]; then
    RESULT="ERROR"
    ERR=$((ERR + 1))
    LINE="$TS,curl_exit_$CURL_EXIT,-,$RESULT"
    echo "  [$TS] ERROR -- curl exit $CURL_EXIT (no response)"
  else
    CODE="${RESPONSE%% *}"
    TIME_TOTAL="${RESPONSE##* }"
    if [[ "$CODE" =~ $EXPECT_REGEX ]]; then
      RESULT="PASS"
      PASS=$((PASS + 1))
    else
      RESULT="FAIL"
      FAIL=$((FAIL + 1))
      echo "  [$TS] FAIL -- got $CODE, expected one of: $EXPECT_CODES (${TIME_TOTAL}s)"
    fi
    LINE="$TS,$CODE,$TIME_TOTAL,$RESULT"
  fi

  echo "$LINE" >> "$OUT"
  if [[ "$RESULT" != "PASS" ]]; then
    FAILURE_LINES+=("$LINE")
  fi

  sleep "$INTERVAL"
done
