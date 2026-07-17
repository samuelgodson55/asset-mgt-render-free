#!/bin/sh
# backend/docker-entrypoint.sh
# -----------------------------------------------------------------------------
# WHY THIS EXISTS (fixes "Backup failed: [Errno 13] Permission denied:
# '/app/backups/snipeit_backup_....sql.gz'" when running via docker-compose):
#
# The Dockerfile's `chown -R appuser:appuser /app` runs at IMAGE BUILD time
# and only ever touches the image's own filesystem layer. docker-compose.yml
# mounts named volumes ("backup_data" at /app/backups, "export_data" at
# EXPORT_RESULT_DIR -- the latter shared with the `worker` container so it
# can write finished CSV/PDF exports for `backend` to serve back out, see
# backend/tasks/export_tasks.py) AT CONTAINER START, which shadows whatever
# was there in the image -- and a fresh named volume is created by the
# Docker daemon owned by root:root, regardless of what the image underneath
# it looked like. So by the time uvicorn starts as `appuser`, these
# directories are root-owned and it can't write into them.
#
# This entrypoint runs as root (see Dockerfile: USER appuser was removed so
# the container starts as root again), fixes ownership of both mounted
# volumes on every single startup -- cheap and idempotent -- and then drops
# privileges down to appuser for the actual app process itself.
#
# Privilege drop uses Python (already in the image) instead of a wrapper
# tool like gosu/su-exec: os.execvp() replaces this process in place, so the
# app itself ends up running as PID 1 and still receives SIGTERM/SIGINT
# directly for a clean shutdown -- no extra binary to install, no signal-
# forwarding wrapper process in between.
set -e

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
mkdir -p "$BACKUP_DIR"
chown -R appuser:appuser "$BACKUP_DIR"

EXPORT_RESULT_DIR="${EXPORT_RESULT_DIR:-/app/export_results}"
mkdir -p "$EXPORT_RESULT_DIR"
chown -R appuser:appuser "$EXPORT_RESULT_DIR"

exec python3 -c '
import os, pwd, sys
pw = pwd.getpwnam("appuser")
os.setgroups([])
os.setgid(pw.pw_gid)
os.setuid(pw.pw_uid)
os.execvp(sys.argv[1], sys.argv[1:])
' "$@"
