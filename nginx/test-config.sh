#!/usr/bin/env bash
# =============================================================================
# nginx/test-config.sh
# -----------------------------------------------------------------------------
# WHY THIS EXISTS
# The frontend Docker image builds successfully (`docker build`) even when
# an nginx config TEMPLATE contains an error, because envsubst and nginx
# itself only run when the CONTAINER STARTS -- not at build time (see
# .github/workflows/ci.yml's "Build frontend image" step, which never
# actually starts the thing it built). That gap let a config-breaking change
# reach a real deploy before anyone noticed: `docker compose up` and Render
# both just showed a crash-looping container instead of the site --
# nginx: [emerg] unknown "1" variable
# (the specific bug this script's assertions are named after -- see
# git blame / the "clean URLs" changes in this file's history: nginx
# location regex captures like $1 can't be re-read inside a nested `if`
# block; see legacy-mode's redirect checks below).
#
# This script closes that gap WITHOUT needing a Docker build at all (it's a
# 10-second plain-process check per template, not a multi-minute image
# build): it runs the exact same envsubst step the official nginx image's
# own docker-entrypoint.d/20-envsubst-on-templates.sh runs, `nginx -t`s the
# result, and then actually boots nginx and curls every path each image
# variant depends on.
#
# TWO TEMPLATES, TWO MODES -- this repo ships nginx/default.conf.template
# (legacy multi-page site, frontend-legacy-only image) and
# nginx/default.react.conf.template (React SPA, frontend-react-only image)
# -- see frontend/Dockerfile's own top-of-file comment for why there are
# two separate images/templates rather than one combined one. This script
# tests BOTH, one after another, so a change to either can't silently break
# the other.
#
# USAGE
#   nginx/test-config.sh                 # tests both templates
#   nginx/test-config.sh legacy          # tests default.conf.template only
#   nginx/test-config.sh react           # tests default.react.conf.template only
# Needs `nginx` and `envsubst` (the `gettext-base` package) on PATH -- both
# already present in CI (see the workflow's "Install nginx + gettext" step)
# and in most Linux dev boxes; on macOS: `brew install nginx gettext`.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for bin in nginx envsubst curl; do
  command -v "$bin" >/dev/null 2>&1 || {
    echo "FAIL: '$bin' not found on PATH -- see this script's header for how to install it." >&2
    exit 1
  }
done

TOTAL_FAILURES=0

# Runs the full render -> nginx -t -> boot -> curl cycle for ONE template.
# $1 = "legacy" | "react"
run_mode() {
  local MODE="$1"
  local TEMPLATE PORT
  if [ "$MODE" = "legacy" ]; then
    TEMPLATE="$SCRIPT_DIR/default.conf.template"
    PORT=8080
  else
    TEMPLATE="$SCRIPT_DIR/default.react.conf.template"
    PORT=8081
  fi

  local WORKDIR
  WORKDIR="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "nginx -c '$WORKDIR/nginx.conf' -s stop >/dev/null 2>&1 || true; rm -rf '$WORKDIR'" RETURN

  echo "=============================================================="
  echo "== testing $MODE ($TEMPLATE) =="
  echo "=============================================================="

  # ---- Stage 1: render the template exactly like docker-entrypoint.d does --
  # The official nginx image's 20-envsubst-on-templates.sh restricts envsubst
  # to ONLY the variable names that are actually set in the environment (via
  # `envsubst "$(printf '${%s} ' $(env | cut -d= -f1))"`) rather than blindly
  # substituting every "$word"-looking token it finds -- reproduced exactly
  # here so this test can't pass on a lucky substitution that the real
  # container wouldn't also make.
  export PORT
  export BACKEND_HOST=backend
  export BACKEND_PORT=8000
  export RESOLVER_IP=127.0.0.11
  export ENABLE_API_DOCS=false
  # Mirrors frontend/Dockerfile's own ENV defaults for these two -- see that
  # Dockerfile's comment for the full history. Empty, not absent: envsubst's
  # restricted substitution (below) only touches variable names that are
  # actually IN the environment, so leaving these two out of this `export`
  # list would leave "${DEPLOY_STATUS_ACCOUNT}"/"${DEPLOY_STATUS_SAS}" as
  # literal text in the rendered config, which nginx's own parser then
  # rejects as a reference to a `set` that never happened -- exactly the
  # `nginx -t` failure ("unknown \"deploy_status_account\" variable") this
  # script exists to catch before it reaches a real VM/local/Render boot.
  export DEPLOY_STATUS_ACCOUNT=""
  export DEPLOY_STATUS_SAS=""
  local defined_envs
  defined_envs="$(printf '${%s} ' $(env | cut -d= -f1))"
  envsubst "$defined_envs" < "$TEMPLATE" > "$WORKDIR/default.conf"

  # ---- Stage 2: `nginx -t` the rendered config ------------------------------
  # Needs a minimal http{} wrapper -- these templates are written to be
  # `include`d from conf.d, not loaded as a standalone top-level config (they
  # use the `resolver` directive, which is only valid inside http{}/server{},
  # and the real image's own nginx.conf provides that wrapper -- see this
  # repo's nginx/ directory, which only ships the conf.d templates because
  # the base nginx:alpine image supplies everything else).
  mkdir -p "$WORKDIR"/{html,client_temp,proxy_temp,fastcgi_temp,uwsgi_temp,scgi_temp,logs}
  if [ "$MODE" = "legacy" ]; then
    # Real content: the actual legacy static site this template serves.
    cp -r "$SCRIPT_DIR/../frontend/"* "$WORKDIR/html/" 2>/dev/null || true
  else
    # No real Vite build here (this script doesn't run `npm run build`) --
    # a minimal stand-in SPA is enough to exercise the config itself
    # (index.html + one hashed asset), which is all this script checks.
    mkdir -p "$WORKDIR/html/assets"
    echo '<!doctype html><html><body>Ledger</body></html>' > "$WORKDIR/html/index.html"
    echo 'console.log("stub");' > "$WORKDIR/html/assets/index-stub123.js"
  fi
  # mktemp -d creates a dir only the owner (typically root, if this script is
  # run as root) can enter -- but when nginx itself is started as root, it
  # forks its worker processes as an unprivileged user (www-data, by
  # whatever `user` directive is compiled in / configured), and THAT user
  # needs to read these files to serve them. Without this, every single
  # request 500s with "Permission denied", which has nothing to do with
  # whether the template itself is correct.
  chmod -R o+rX "$WORKDIR"
  # Each template declares its own `server { listen \$PORT; root ...;
  # index ...; }` block already, so it's `include`d at the http{} level here
  # (one level up from where conf.d normally sits) -- matching exactly how
  # the real image's /etc/nginx/nginx.conf includes /etc/nginx/conf.d/*.conf.
  cat > "$WORKDIR/nginx.conf" <<EOF
worker_processes 1;
pid $WORKDIR/nginx.pid;
events { worker_connections 256; }
http {
    access_log $WORKDIR/logs/access.log;
    error_log $WORKDIR/logs/error.log;
    client_body_temp_path $WORKDIR/client_temp;
    proxy_temp_path $WORKDIR/proxy_temp;
    fastcgi_temp_path $WORKDIR/fastcgi_temp;
    uwsgi_temp_path $WORKDIR/uwsgi_temp;
    scgi_temp_path $WORKDIR/scgi_temp;
    include $WORKDIR/default.conf;
}
EOF
  sed -i "s#root /usr/share/nginx/html;#root $WORKDIR/html;#" "$WORKDIR/default.conf"

  echo "== nginx -t =="
  nginx -t -c "$WORKDIR/nginx.conf"

  # ---- Stage 3: boot it for real and exercise every path --------------------
  echo "== booting nginx on 127.0.0.1:$PORT =="
  nginx -c "$WORKDIR/nginx.conf"
  local i
  for i in $(seq 1 20); do
    curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
    sleep 0.5
    if [ "$i" -eq 20 ]; then
      echo "FAIL: nginx never came up -- see $WORKDIR/logs/error.log" >&2
      cat "$WORKDIR/logs/error.log" >&2 || true
      TOTAL_FAILURES=$((TOTAL_FAILURES + 1))
      return
    fi
  done

  local FAILURES=0

  expect_status() {
    local path="$1" want="$2"
    local got
    got="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$path")"
    if [ "$got" != "$want" ]; then
      echo "FAIL: GET $path -> $got (expected $want)" >&2
      FAILURES=$((FAILURES + 1))
    else
      echo "ok:   GET $path -> $got"
    fi
  }

  expect_redirect_to() {
    local path="$1" want_location="$2"
    local headers got_status got_location
    headers="$(curl -s -D - -o /dev/null "http://127.0.0.1:$PORT$path")"
    got_status="$(echo "$headers" | head -1 | tr -d '\r' | awk '{print $2}')"
    got_location="$(echo "$headers" | grep -i '^location:' | tr -d '\r' | awk '{print $2}')"
    if [ "$got_status" != "301" ] || [ "$got_location" != "$want_location" ]; then
      echo "FAIL: GET $path -> $got_status Location:$got_location (expected 301 Location:$want_location)" >&2
      FAILURES=$((FAILURES + 1))
    else
      echo "ok:   GET $path -> 301 Location:$got_location"
    fi
  }

  if [ "$MODE" = "legacy" ]; then
    # Clean URLs resolve directly (no redirect, no URL change in the browser).
    expect_status "/" 200
    expect_status "/admin" 200
    expect_status "/manager" 200
    expect_status "/staff" 200
    expect_status "/customer" 200

    # Old-style *.html links canonicalize onto the clean URL. This is exactly
    # the case that broke in the past: the `if ($1 = "index")` version of this
    # rule passed `nginx -t` in isolation on some setups but failed with
    # "unknown \"1\" variable" once envsubst-rendered and loaded for real.
    expect_redirect_to "/admin.html" "http://127.0.0.1:$PORT/admin"
    expect_redirect_to "/manager.html" "http://127.0.0.1:$PORT/manager"
    expect_redirect_to "/staff.html" "http://127.0.0.1:$PORT/staff"
    expect_redirect_to "/customer.html" "http://127.0.0.1:$PORT/customer"
    expect_redirect_to "/index.html" "http://127.0.0.1:$PORT/"

    # Query strings survive the redirect.
    expect_redirect_to "/manager.html?foo=bar" "http://127.0.0.1:$PORT/manager?foo=bar"

    # Real static assets and a genuine 404 still behave normally.
    expect_status "/js/auth.js" 200
    expect_status "/nonexistent-page" 404
  else
    # The site root and the hashed stub asset resolve directly.
    expect_status "/" 200
    expect_status "/assets/index-stub123.js" 200

    # A client-side route react-router owns (NOT a real file on disk)
    # falls back to index.html with a 200, not a 404 -- this is the whole
    # point of default.react.conf.template's SPA `location /` fallback,
    # and the one behavior default.conf.template deliberately does NOT
    # have (a truly missing legacy page should 404, not silently serve
    # the SPA shell).
    expect_status "/checkouts" 200
    expect_status "/assets/42" 200

    # A request for a real missing FILE under /assets/ still 404s -- the
    # SPA fallback only ever catches app ROUTES via the bare `location /`
    # try_files chain; the hashed-asset regex location above it is a
    # dedicated `location` block, so a miss there falls through to the
    # SAME `location /` try_files chain and still resolves to index.html.
    # This is expected/correct SPA behavior (a bad asset URL should still
    # let the app boot and handle it client-side), documented here rather
    # than asserted as a 404 so a future reader isn't surprised by it.
  fi

  if [ "$FAILURES" -gt 0 ]; then
    echo "FAILED ($MODE): $FAILURES check(s) did not match. See error.log below:" >&2
    cat "$WORKDIR/logs/error.log" >&2 || true
    TOTAL_FAILURES=$((TOTAL_FAILURES + FAILURES))
  else
    echo "All $MODE nginx checks passed."
  fi
}

REQUESTED="${1:-both}"
case "$REQUESTED" in
  legacy) run_mode legacy ;;
  react) run_mode react ;;
  both) run_mode legacy; run_mode react ;;
  *)
    echo "Usage: $0 [legacy|react|both]" >&2
    exit 1
    ;;
esac

if [ "$TOTAL_FAILURES" -gt 0 ]; then
  echo "FAILED: $TOTAL_FAILURES total check(s) did not match across all tested templates." >&2
  exit 1
fi

echo "All nginx config checks passed."
