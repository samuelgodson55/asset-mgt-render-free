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
#
# BUG FIX ("readyz: not ready ... could not open certificate file
# '/root/.postgresql/postgresql.crt': Permission denied" against Azure
# Database for PostgreSQL, forever -- the container never becomes ready):
# os.setuid()/os.setgid() below change the process's UID/GID, but nothing
# about them touches the HOME environment variable -- it stays whatever it
# already was, which is "/root" (this script starts as root -- see above).
# libpq (used by psycopg2, the database.py connection under the hood) reads
# $HOME to find its default client-certificate path, ~/.postgresql/
# postgresql.crt, as part of its normal SSL handshake setup -- that lookup
# happens whether or not you're actually using a client cert. Right after
# setuid(), the process is running AS appuser but $HOME still says "/root":
# appuser has no permission to even stat appuser's home directory, so
# instead of the usual harmless "no such file" (which libpq treats as "no
# client cert configured" and ignores), the stat itself fails with EACCES,
# which libpq surfaces as a hard connection error -- exactly what
# database.py's get_schema_status() was catching and reporting as
# "Could not reach the database to check its migration state" on every
# single readiness check. Setting HOME (and USER, for anything else that
# reads it) to appuser's own home directory before exec fixes the lookup
# path itself, so it resolves to a directory appuser actually owns.
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
os.environ["HOME"] = pw.pw_dir
os.environ["USER"] = "appuser"
os.environ["LOGNAME"] = "appuser"
os.execvp(sys.argv[1], sys.argv[1:])
' "$@"
