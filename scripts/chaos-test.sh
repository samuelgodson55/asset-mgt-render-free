#!/usr/bin/env bash
set -euo pipefail
# Git Bash/MSYS on Windows rewrites leading-slash CLI arguments (e.g. /readyz)
# when invoking native Windows executables such as docker.exe. Disable that
# conversion so the path reaches the container unchanged. No-op on Linux/macOS.
export MSYS_NO_PATHCONV=1
case "$(uname -s 2>/dev/null || true)" in MINGW*|MSYS*|CYGWIN*) NULL_DEVICE=NUL ;; *) NULL_DEVICE=/dev/null ;; esac
# P1 local dependency-failure test. Run only against the dedicated local Docker Compose stack.
# Public traffic is tested through nginx (:8080); backend readiness is tested
# directly on the local-only FastAPI diagnostic port (:8001 by default). This
# prevents an nginx SPA fallback from ever masking a broken /readyz endpoint.
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
BASE_URL="${BASE_URL:-http://localhost:8080}"
BACKEND_DIRECT_PORT="${BACKEND_DIRECT_PORT:-8001}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:${BACKEND_DIRECT_PORT}}"
DC=(docker compose -f "$COMPOSE_FILE")
cleanup(){ echo "== Cleanup: restoring local dependencies =="; "${DC[@]}" up -d db redis backend worker beat errorbeacon frontend >/dev/null 2>&1 || true; }
trap cleanup EXIT

public_code(){ curl -sS -o "$NULL_DEVICE" -w '%{http_code}' --max-time 5 "$1" || true; }
backend_code(){ local path="$1"; curl -sS -o "$NULL_DEVICE" -w '%{http_code}' --max-time 5 "$BACKEND_URL$path" || true; }
backend_body(){ local path="$1"; curl -sS --max-time 5 "$BACKEND_URL$path" 2>/dev/null || true; }
wait_backend(){ local expected="$1" attempts="${2:-30}" code; for ((i=1;i<=attempts;i++)); do code=$(backend_code /readyz); [[ "$code" == "$expected" ]] && return 0; sleep 2; done; echo "FAILED: backend /readyz expected HTTP $expected (last=$code, url=$BACKEND_URL/readyz)" >&2; echo '--- backend direct response ---' >&2; backend_body /readyz >&2 || true; echo '--- backend logs ---' >&2; "${DC[@]}" logs --tail=80 backend >&2 || true; return 1; }
wait_public(){ local path="$1" expected="$2" attempts="${3:-30}" code; for ((i=1;i<=attempts;i++)); do code=$(public_code "$BASE_URL$path"); [[ "$code" == "$expected" ]] && return 0; sleep 2; done; echo "FAILED: $BASE_URL$path expected HTTP $expected (last=$code)" >&2; return 1; }
wait_service_running(){ local service="$1" attempts="${2:-30}" state; for ((i=1;i<=attempts;i++)); do state=$("${DC[@]}" ps -q "$service" | xargs -r docker inspect -f '{{.State.Status}}' 2>/dev/null || true); [[ "$state" == running ]] && return 0; sleep 2; done; echo "FAILED: service $service did not remain running (last=$state)" >&2; return 1; }
wait_redis_ready(){ for ((i=1;i<=30;i++)); do "${DC[@]}" exec -T redis redis-cli ping 2>/dev/null | grep -q PONG && return 0; sleep 2; done; echo 'FAILED: Redis did not recover' >&2; return 1; }
wait_errorbeacon(){ local expected="$1" attempts="${2:-30}" code; for ((i=1;i<=attempts;i++)); do code=$("${DC[@]}" exec -T errorbeacon python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status)" 2>/dev/null || true); [[ "$code" == "$expected" ]] && return 0; sleep 2; done; echo "FAILED: ErrorBeacon health expected HTTP $expected (last=$code)" >&2; return 1; }
wait_errorbeacon_idle(){ local attempts="${1:-40}" body alert_q ai_q; for ((i=1;i<=attempts;i++)); do body=$("${DC[@]}" exec -T errorbeacon python3 -c "import urllib.request,json; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).read().decode())" 2>/dev/null || true); alert_q=$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("queue_depth",999999))' <<<"$body" 2>/dev/null || echo 999999); ai_q=$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("ai_queue_depth",999999))' <<<"$body" 2>/dev/null || echo 999999); [[ "$alert_q" == 0 && "$ai_q" == 0 ]] && return 0; sleep 1; done; echo "WARNING: ErrorBeacon queues did not fully drain before test completion (alert=$alert_q ai=$ai_q)" >&2; return 0; }

# Preflight: make sure the stack and the direct diagnostic port are actually up.
wait_public /healthz 200 30
for ((i=1;i<=30;i++)); do code=$(backend_code /healthz); [[ "$code" == 200 ]] && break; sleep 2; done
[[ "${code:-0}" == 200 ]] || { echo "FAILED: backend direct diagnostic endpoint did not become HTTP 200 at $BACKEND_URL/healthz" >&2; "${DC[@]}" logs --tail=80 backend >&2 || true; exit 1; }
wait_backend 200

echo '== Chaos 1: Redis outage =='
"${DC[@]}" stop redis >/dev/null
wait_public /healthz 200
wait_backend 200
wait_service_running worker 10
wait_service_running beat 10
LOGIN_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$BASE_URL/api/auth/login" -H 'Content-Type: application/json' --data '{"identifier":"chaos-test-invalid@example.invalid","password":"invalid"}' || true)
[[ "$LOGIN_CODE" != 503 && "$LOGIN_CODE" != 000 ]] || { echo "FAILED: login unavailable during Redis outage (HTTP $LOGIN_CODE)" >&2; exit 1; }
"${DC[@]}" start redis >/dev/null
wait_redis_ready
wait_service_running worker 20
wait_service_running beat 20

echo '== Chaos 2: Celery worker outage =='
"${DC[@]}" stop worker >/dev/null
wait_public /healthz 200
wait_backend 200
"${DC[@]}" start worker >/dev/null
wait_service_running worker 20

echo '== Chaos 3: ErrorBeacon outage =='
"${DC[@]}" stop errorbeacon >/dev/null
wait_public /healthz 200
wait_backend 200
TELEM_TIME=$(curl -sS -o "$NULL_DEVICE" -w '%{time_total}' --max-time 3 -X POST "$BASE_URL/api/telemetry/client-error" -H 'Content-Type: application/json' --data '{"message":"controlled ErrorBeacon outage test","path":"/chaos-test"}' || true)
python3 - "$TELEM_TIME" <<'PY'
import sys
try: value=float(sys.argv[1])
except Exception: raise SystemExit('FAILED: telemetry request did not complete while ErrorBeacon was down')
if value>1.5: raise SystemExit(f'FAILED: telemetry path blocked by ErrorBeacon outage ({value:.3f}s)')
PY
"${DC[@]}" start errorbeacon >/dev/null
wait_errorbeacon 200

echo '== Chaos 4: PostgreSQL outage =='
"${DC[@]}" stop db >/dev/null
wait_public /healthz 200
wait_backend 503 20
"${DC[@]}" start db >/dev/null
wait_backend 200 45

echo '== Correlation: frontend proxy -> backend -> ErrorBeacon =='
CORR_JSON=$(curl -sS --max-time 10 -X POST "$BASE_URL/api/telemetry/client-error" -H 'Content-Type: application/json' --data '{"message":"controlled correlation smoke test","path":"/chaos-test","context":{"test":true}}')
CORR_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("request_id") or "")' <<<"$CORR_JSON")
[[ "$CORR_ID" =~ ^[0-9a-fA-F-]{20,}$ ]] || { echo "FAILED: invalid correlation request_id: $CORR_JSON" >&2; exit 1; }
FOUND=0
for ((i=1;i<=20;i++)); do FOUND=$("${DC[@]}" exec -T errorbeacon python3 - "$CORR_ID" <<'PY'
import sqlite3,sys
c=sqlite3.connect('/data/errorbeacon.db'); print('1' if c.execute('SELECT 1 FROM incidents WHERE request_id=? LIMIT 1',(sys.argv[1],)).fetchone() else '0'); c.close()
PY
); [[ "$FOUND" == 1 ]] && break; sleep 1; done
[[ "$FOUND" == 1 ]] || { echo "FAILED: ErrorBeacon did not persist correlation request_id=$CORR_ID" >&2; exit 1; }
echo "Correlation request_id=$CORR_ID verified end-to-end."
wait_errorbeacon_idle 45
echo 'ALL P1 CHAOS CHECKS PASSED'
