#!/usr/bin/env python3
"""check-compose-drift.py

WHY THIS EXISTS: this repo has two docker-compose files on purpose --
docker-compose.yml (local dev: `build:`s the images itself, hot-reload
volumes) and docker-compose.vm.yml (the VM path: pulls the prebuilt
${DOCKERHUB_BACKEND_IMAGE}/${DOCKERHUB_FRONTEND_IMAGE} images). That split is
fine -- what's NOT fine is the two files silently disagreeing about which
environment variables the backend/worker/beat/frontend services accept,
because that's effectively the SAME shared image being configured
differently (or under-configured) depending only on which compose file
happens to run it.

This is not a hypothetical: BACKUP_GDRIVE_CREDENTIALS_JSON was added to
docker-compose.yml (and backend/config.py, backend/services/backup_service.py,
render.yaml) but never to docker-compose.vm.yml, so Mode 2 Google Drive
backups silently only ever worked in local dev, never on a real VM deploy,
with no error anywhere pointing at why. See docker-compose.vm.yml's comment
on that same variable for the fix. This script is the guard against that
specific class of bug recurring for the next env var someone adds.

WHAT IT CHECKS: for each of backend/worker/beat/frontend, the set of
environment variable KEYS (not values -- values are legitimately allowed to
differ, e.g. LOG_FORMAT: json only on the VM path) must be identical between
the two files, unless explicitly allow-listed below with a reason.

Usage: python3 scripts/check-compose-drift.py
Exit code 0 = no unexplained drift. Exit code 1 = drift found, printed.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
VM_COMPOSE = REPO_ROOT / "docker-compose.vm.yml"
SERVICES = ["backend", "worker", "beat", "frontend"]

# Keys that are INTENTIONALLY only present on one side, with why -- anything
# not listed here that differs between the two files fails the check.
ALLOWED_ONLY_IN_DEV = {
    "backend": {
        # Deprecated single-hour form, replaced by BACKUP_HOURS_UTC
        # (plural) -- kept accepted in local dev for anyone with an old
        # .env, but the VM path's terraform/cloud-init only ever emits the
        # current plural form, so there's nothing for docker-compose.vm.yml
        # to read here.
        "BACKUP_HOUR_UTC",
    },
}
ALLOWED_ONLY_IN_VM = {
    # (none currently -- add here, with a reason, if the VM path ever
    # legitimately needs a key local dev doesn't, e.g. something that only
    # makes sense against a real reverse proxy/tunnel.)
}


def service_env_keys(compose_path: Path, service: str) -> set[str]:
    """Extract the top-level `environment:` block's keys for one service.

    Deliberately simple line-based scanning (not a full YAML parser) so this
    has zero dependencies and stays readable -- both compose files use a
    consistent `service_name:` -> `    environment:` -> `      KEY: value`
    shape throughout, so indentation-based scoping is reliable here even
    though it wouldn't be for arbitrary YAML.
    """
    lines = compose_path.read_text().splitlines()
    keys: set[str] = set()
    in_service = in_env = False
    service_indent = env_indent = None

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if re.match(rf"^  {re.escape(service)}:\s*$", line):
            in_service, service_indent = True, indent
            continue

        if in_service and stripped and not stripped.startswith("#") and indent <= service_indent:
            in_service = in_env = False

        if in_service and re.match(r"^\s*environment:\s*$", line):
            in_env, env_indent = True, indent
            continue

        if in_env:
            if stripped and not stripped.startswith("#") and indent <= env_indent:
                in_env = False
                continue
            m = re.match(r"^\s*-?\s*([A-Z_][A-Z0-9_]*)\s*[:=]", stripped)
            if m:
                keys.add(m.group(1))

    return keys


def main() -> int:
    if not DEV_COMPOSE.exists() or not VM_COMPOSE.exists():
        print(f"::error::expected both {DEV_COMPOSE} and {VM_COMPOSE} to exist")
        return 1

    had_drift = False
    for service in SERVICES:
        dev_keys = service_env_keys(DEV_COMPOSE, service)
        vm_keys = service_env_keys(VM_COMPOSE, service)

        only_dev = dev_keys - vm_keys - ALLOWED_ONLY_IN_DEV.get(service, set())
        only_vm = vm_keys - dev_keys - ALLOWED_ONLY_IN_VM.get(service, set())

        if only_dev or only_vm:
            had_drift = True
            print(f"::error::compose drift in service '{service}':")
            if only_dev:
                print(f"  in docker-compose.yml but missing from docker-compose.vm.yml: {sorted(only_dev)}")
            if only_vm:
                print(f"  in docker-compose.vm.yml but missing from docker-compose.yml: {sorted(only_vm)}")
            print(
                "  -> either add the missing key to the other file, or if this is "
                "intentional, add it to ALLOWED_ONLY_IN_DEV/ALLOWED_ONLY_IN_VM in "
                "scripts/check-compose-drift.py with a one-line reason."
            )

    if had_drift:
        print(
            "\n::error::docker-compose.yml and docker-compose.vm.yml disagree on which "
            "env vars the shared backend/frontend images accept -- this is exactly the "
            "bug class that left Mode 2 Google Drive backups silently broken on the VM "
            "path. See this script's module docstring."
        )
        return 1

    print("No unexplained env var drift between docker-compose.yml and docker-compose.vm.yml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
