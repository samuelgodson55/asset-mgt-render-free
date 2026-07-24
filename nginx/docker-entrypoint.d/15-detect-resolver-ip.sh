#!/bin/sh
# =============================================================================
# nginx/docker-entrypoint.d/15-detect-resolver-ip.sh
# -----------------------------------------------------------------------------
# The official nginx image automatically runs every executable *.sh script in
# /docker-entrypoint.d/ (in lexical order) before it starts nginx -- including
# its own bundled 20-envsubst-on-templates.sh, which is what turns
# nginx/default.conf.template into the real config (see that file's own
# comments). Naming this script "15-..." guarantees it runs BEFORE that
# templating step, so by the time ${RESOLVER_IP} gets substituted, this
# script has already had a chance to fill it in.
#
# WHY THIS EXISTS
# -----------------------------------------------------------------------------
# default.conf.template's `resolver ${RESOLVER_IP} ...` directive needs to
# know the platform's internal DNS server so it can re-resolve BACKEND_HOST
# on every request (instead of caching a possibly-stale IP forever). That
# address is different per platform:
#   - Docker Compose: 127.0.0.11 (Docker's own embedded DNS) -- already
#     pinned explicitly in docker-compose.yml, so this script is a no-op there.
#   - Render, Kubernetes, ECS, etc: each platform has its own internal
#     resolver address, and NOT ALL of them document a fixed, guaranteed-
#     stable IP for it (Render's docs, for instance, don't publish one --
#     see README.md's Render deployment checklist, which used to just say
#     "leave RESOLVER_IP at its default only if Render's docs confirm
#     that's correct" -- an unsatisfying thing to have to manually verify).
#
# Rather than hardcode a guess into render.yaml (or any other platform's
# config) that could silently go stale if that platform ever changes its
# internal resolver address, this script reads it straight from the
# container's own /etc/resolv.conf at boot -- which is exactly where the
# container runtime (Docker, containerd, whatever Render uses under the
# hood, etc.) already writes the resolver it wants this container to use.
# That's the same source nginx itself would fall back to if you omitted the
# `resolver` directive entirely for a plain DNS lookup, so reading it
# ourselves and feeding it back into an explicit `resolver` directive is
# just making that same information available to the *dynamic*, one-
# variable-in-proxy_pass style of resolution this proxy relies on (a plain
# `resolver` directive doesn't get consulted unless something in the config
# forces a runtime lookup -- which is exactly what proxy_pass targeting a
# variable does; see default.conf.template's own comments on that).
#
# If you (or a platform's own tooling) already set RESOLVER_IP explicitly --
# e.g. because you've confirmed the exact right value for your environment --
# this script gets out of the way entirely and leaves it alone.
#
# IMPORTANT -- DO NOT `chmod +x` THIS FILE:
# nginx's docker-entrypoint.sh SOURCES (`. "$f"`) any *.sh file here that
# ISN'T executable, but EXECS (runs as a separate child process) any that
# IS. Sourcing is required for our `export RESOLVER_IP=...` below to
# actually stick around for the later 20-envsubst-on-templates.sh step --
# an executable script's env changes die with that script's own process
# and would never be seen by anything that runs after it.
# =============================================================================

set -e

if [ -n "$RESOLVER_IP" ]; then
    echo "15-detect-resolver-ip.sh: RESOLVER_IP already set to '$RESOLVER_IP' -- leaving it alone."
    exit 0
fi

detected="$(awk '/^nameserver[[:space:]]/{print $2; exit}' /etc/resolv.conf 2>/dev/null || true)"

if [ -z "$detected" ]; then
    # Last-resort fallback so the container still starts with a working
    # config instead of nginx failing to boot on a missing `resolver`
    # value -- Docker's own embedded DNS address, which is at least a
    # sane default in the most common case (local Docker Compose).
    detected="127.0.0.11"
    echo "15-detect-resolver-ip.sh: could not read a nameserver from /etc/resolv.conf -- falling back to '$detected'."
else
    echo "15-detect-resolver-ip.sh: auto-detected RESOLVER_IP='$detected' from /etc/resolv.conf."
fi

export RESOLVER_IP="$detected"
