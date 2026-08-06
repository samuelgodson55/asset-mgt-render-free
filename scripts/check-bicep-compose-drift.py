#!/usr/bin/env python3
"""check-bicep-compose-drift.py

WHY THIS EXISTS: this is scripts/check-compose-drift.py's sibling for the
THIRD deploy path -- infra/main.bicep (Azure Container Apps). That script
already guards docker-compose.yml <-> docker-compose.vm.yml env-var-key
parity for backend/worker/beat/frontend; this one guards the same
backend/frontend env-var KEYS between the compose files and main.bicep's
Container Apps definitions, so a key added on one cloud path doesn't
silently go missing on the other. See check-compose-drift.py's own
docstring for the original bug (BACKUP_GDRIVE_CREDENTIALS_JSON present in
docker-compose.yml but missing from docker-compose.vm.yml) that motivated
building a guard for this class of drift in the first place -- the exact
same class of bug was found again while writing THIS script:
DISPLAY_TIMEZONE/CURRENCY_CODE were real, terraform/bicep-wired settings
(infra-vm's variables.tf + infra/main.bicep's sharedEnv) that neither
compose file ever actually read in its own `environment:` block, so
setting either one via terraform.tfvars/bicep params was silently
ignored. Both compose files now read them (see docker-compose.yml's own
comment on that fix); this script is what keeps that fix from quietly
regressing.

WHAT'S DIFFERENT FROM check-compose-drift.py'S MODEL: main.bicep has no
separate worker/beat Container App at all -- RUN_EMBEDDED_WORKER=true
(see backendApp's own env block) means the ACA `backend` app runs the
embedded Celery worker+beat itself, the same way docker-compose.yml's
`backend` service could in theory but chooses not to (see that service's
own "-B" comment). So the fair compose-side comparison for ACA's
`backend` role is the UNION of docker-compose's backend+worker+beat env
keys, not just backend alone -- in practice this repo's worker/beat env
blocks are already subsets of backend's own (worker/beat deliberately
receive a trimmed-down copy, per docker-compose.yml's own per-key
comments), so unioning them in changes nothing today, but keeps this
script honest if that ever stops being true. ACA's `frontend` app is a
much closer 1:1 match to compose's `frontend`/`frontend-blue`/
`frontend-green`, so that comparison stays simple.

WHAT IT CHECKS: for each of backend/frontend, the set of environment
variable KEYS (not values) main.bicep's Container Apps definitions pass
must be identical to what the compose files pass for the same role,
unless explicitly allow-listed below with a reason -- same model as
check-compose-drift.py.

HOW IT READS main.bicep: deliberately simple regex/bracket-balance
scanning (not a real Bicep parser), matching check-compose-drift.py's own
"stay dependency-free and readable" philosophy. It resolves `sharedEnv`/
`sharedSecretEnvRefs` var references inside a resource's `env: concat(...)`
expression back to those vars' own literal `{ name: 'KEY', ... }` entries,
plus any keys inlined directly in that resource's own env expression.
This is reliable for this file's consistent shape, but is NOT a general
Bicep evaluator -- a structural rewrite of main.bicep's env plumbing
(new intermediate vars, a differently-named concat, etc.) may need a
matching update here.

Usage: python3 scripts/check-bicep-compose-drift.py
Exit code 0 = no unexplained drift. Exit code 1 = drift found, printed.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
VM_COMPOSE = REPO_ROOT / "docker-compose.vm.yml"
BICEP_FILE = REPO_ROOT / "infra" / "main.bicep"

# Compose-side service name(s) that make up each conceptual role, same
# shape as check-compose-drift.py's own SERVICE_MAP (and literally reused
# for the `backend`/`frontend` entries) -- `worker`/`beat` are folded into
# `backend` here because that's what main.bicep's RUN_EMBEDDED_WORKER
# actually does at runtime (see module docstring above).
COMPOSE_ROLE_MAP = {
    "backend": {
        "dev": ["backend", "worker", "beat"],
        "vm": ["backend-blue", "backend-green", "worker", "beat"],
    },
    "frontend": {
        "dev": ["frontend"],
        "vm": ["frontend-blue", "frontend-green"],
    },
}

# The Container App resource in main.bicep that plays each conceptual role.
BICEP_RESOURCE_MAP = {
    "backend": "backendApp",
    "frontend": "frontendApp",
}

# Keys intentionally only on the compose (docker-compose.yml/.vm.yml) side.
ALLOWED_ONLY_IN_COMPOSE = {
    "backend": {
        # Deprecated single-hour form -- see check-compose-drift.py's own
        # ALLOWED_ONLY_IN_DEV entry for this same key; docker-compose.vm.yml
        # never emits it either, and neither does any bicep/terraform path.
        "BACKUP_HOUR_UTC",
        # database.py's adaptive DB-pool sizing explicit-override knob --
        # both compose files set this because they run worker/beat as
        # separate always-on processes (docker-compose.vm.yml also briefly
        # doubles up backend during blue-green), which the ACA path's
        # BACKEND_MAX_REPLICAS-based derivation doesn't need to account for
        # (see config.py's DB_EXPECTED_PROCESSES docstring).
        "DB_EXPECTED_PROCESSES",
        # How many uvicorn worker processes backend/start.sh launches --
        # ACA has no equivalent bicep param for this (Container Apps'
        # own `scale`/replica model is the horizontal-scaling knob there
        # instead), so it's left to start.sh's own UVICORN_WORKERS
        # default rather than exposed as a setting on this path.
        "UVICORN_WORKERS",
        # Debug-only "print every span to stdout too" knob (see
        # backend/telemetry.py) -- off by default everywhere, and not
        # worth a bicep param since Application Insights (this path's
        # `otelAzureMonitorEnabled`) is the real trace sink on ACA.
        "OTEL_CONSOLE_EXPORTER",
        # config.py's OTEL_EXPORTER_OTLP_PROTOCOL default ("http/protobuf")
        # already matches what this path needs -- no bicep param exists
        # to override it because nothing on ACA needs to.
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        # Mode 2 (Google Workspace service account + Shared Drive) Google
        # Drive backup credential -- infra/main.bicep's own
        # `gdriveBackupEnabled` param comment explicitly says this mode is
        # "deliberately NOT exposed as a bicep param here (it requires a
        # Google Workspace Shared Drive, not applicable to this app's
        # typical personal-Drive use case)". Only Mode 1 (OAuth,
        # BACKUP_GDRIVE_OAUTH_*) is wired through on the ACA path.
        "BACKUP_GDRIVE_CREDENTIALS_JSON",
    },
    "frontend": {
        # nginx/docker-entrypoint.d/15-detect-resolver-ip.sh reads its own
        # resolver IP from /etc/resolv.conf at boot when this is unset --
        # main.bicep's frontendApp deliberately leaves it unset for
        # exactly that reason (see that resource's own RESOLVER_IP
        # comment), same as it does for Render.
        "RESOLVER_IP",
    },
}

# Keys intentionally only on the main.bicep (ACA) side.
ALLOWED_ONLY_IN_BICEP = {
    "backend": {
        # Launches the embedded Celery worker/beat inside the SAME
        # container (see backendApp's own env comment) -- this is what
        # replaces having separate worker/beat Container Apps at all, so
        # naturally has no compose-side equivalent key. See module
        # docstring above for why COMPOSE_ROLE_MAP folds worker/beat into
        # `backend` to compensate for this on the other side of the diff.
        "RUN_EMBEDDED_WORKER",
        # Self-service Quotation Catalog stock-visibility toggle -- has a
        # bicep param (`catalogShowStockToStaffCustomer`) and an ACA-side
        # GitHub Actions var (.github/workflows/sync-secrets-aca.yml), but
        # no matching infra-vm/variables.tf Terraform variable and no
        # docker-compose.vm.yml wiring -- ACA-only today. If this ever
        # gets a VM-path Terraform variable, add the matching
        # DISPLAY_TIMEZONE/CURRENCY_CODE-style compose wiring (see those
        # two keys' own comment in docker-compose.yml) and remove this
        # entry instead of leaving it allow-listed.
        "CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER",
        # database.py's adaptive DB-pool sizing replica-derived input --
        # only meaningful where DB_EXPECTED_PROCESSES (compose-only, see
        # that key's own entry in ALLOWED_ONLY_IN_COMPOSE above) is left
        # unset, which is exactly the ACA/bicep case (config.py's
        # BACKEND_MAX_REPLICAS docstring).
        "BACKEND_MAX_REPLICAS",
    },
    "frontend": {
        # Live blue-green rollout status dashboard, proxied by nginx
        # straight through to Azure Blob Storage per-request (see
        # frontendApp's own DEPLOY_STATUS_ACCOUNT/DEPLOY_STATUS_SAS
        # comment) -- the VM path's equivalent dashboard is served by
        # `caddy` reading a local bind-mounted file instead (see
        # docker-compose.vm.yml's `caddy` service), so frontend's own
        # nginx container never needs these two there.
        "DEPLOY_STATUS_ACCOUNT",
        "DEPLOY_STATUS_SAS",
    },
}


def compose_service_env_keys(compose_path: Path, service: str) -> set[str]:
    """Extract one service's top-level `environment:` block's keys.

    Identical logic to check-compose-drift.py's own service_env_keys() --
    duplicated (not imported) so this script stays a single standalone
    file, matching that script's own zero-dependency, copy-paste-over-
    shared-module style (see docker-compose.yml's own comment on why this
    repo avoids that kind of indirection for beginner-readability).
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


def compose_role_keys(role: str) -> set[str]:
    """Union of env keys across every compose service that plays `role`,
    across BOTH compose files -- e.g. `backend` pulls docker-compose.yml's
    backend+worker+beat, UNION docker-compose.vm.yml's backend-blue+
    backend-green+worker+beat. See COMPOSE_ROLE_MAP's own comment for why
    worker/beat are folded in here."""
    keys: set[str] = set()
    for service in COMPOSE_ROLE_MAP[role]["dev"]:
        keys |= compose_service_env_keys(DEV_COMPOSE, service)
    for service in COMPOSE_ROLE_MAP[role]["vm"]:
        keys |= compose_service_env_keys(VM_COMPOSE, service)
    return keys


def _extract_balanced(text: str, start: int) -> int:
    """`start` indexes an opening `(` or `[` in `text`. Return the index
    just past its matching close, tracking BOTH bracket kinds together
    since Bicep freely nests `concat([...], [...])` -- an unmatched-kind
    close (e.g. a `)` closing a `[` opened earlier in a mismatched way)
    would be a real Bicep syntax error, not something this script needs
    to handle, so depth-only tracking (not kind-specific) is sufficient
    and keeps this a few lines instead of a real parser."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unbalanced brackets starting at index {start}")


def _literal_env_keys(expr_text: str) -> set[str]:
    """Every literal `{ name: 'KEY', ... }` entry directly inside a Bicep
    expression snippet -- does NOT follow references to other vars (that's
    `_resolve_var_refs`'s job), so this only ever picks up keys actually
    spelled out at this exact point in the file."""
    return set(re.findall(r"name:\s*'([A-Z_][A-Z0-9_]*)'", expr_text))


def bicep_var_block(text: str, varname: str) -> str:
    """The full bracketed value of a top-level `var <varname> = [...]`
    declaration."""
    m = re.search(rf"\nvar {re.escape(varname)} = ", text)
    if not m:
        raise ValueError(f"could not find `var {varname} = ` in {BICEP_FILE}")
    start = m.end()
    while text[start] not in "([":
        start += 1
    return text[start:_extract_balanced(text, start)]


def bicep_resource_block(text: str, resource_var: str) -> str:
    """The full text of a top-level `resource <resource_var> '...' = {...}`
    declaration, up to (not including) the next top-level `resource`/
    `output` statement."""
    m = re.search(rf"\nresource {re.escape(resource_var)} ", text)
    if not m:
        raise ValueError(f"could not find `resource {resource_var} ` in {BICEP_FILE}")
    tail = text[m.end():]
    m2 = re.search(r"\n(resource|output)\s", tail)
    end = m.end() + (m2.start() if m2 else len(tail))
    return text[m.start():end]


def bicep_container_env_keys(resource_block: str, var_env_keys: dict[str, set[str]]) -> set[str]:
    """A resource block's own container `env:` field, resolved to a flat
    key set -- literal inline keys, PLUS (if the expression textually
    references a known shared var like `sharedEnv`/`sharedSecretEnvRefs`)
    that var's own keys. See module docstring's "HOW IT READS main.bicep"
    section for the reasoning/limits of this approach."""
    m = re.search(r"\benv:\s*", resource_block)
    if not m:
        raise ValueError("could not find `env:` field in resource block")
    start = m.end()
    while resource_block[start] not in "([":
        start += 1
    env_expr = resource_block[start:_extract_balanced(resource_block, start)]

    keys = _literal_env_keys(env_expr)
    for varname, var_keys in var_env_keys.items():
        if re.search(rf"\b{re.escape(varname)}\b", env_expr):
            keys |= var_keys
    return keys


def main() -> int:
    if not DEV_COMPOSE.exists() or not VM_COMPOSE.exists() or not BICEP_FILE.exists():
        print(f"::error::expected {DEV_COMPOSE}, {VM_COMPOSE}, and {BICEP_FILE} to all exist")
        return 1

    bicep_text = BICEP_FILE.read_text()

    # Vars a resource's `env: concat(...)` expression can reference by
    # name -- resolved once up front, same way check-compose-drift.py
    # resolves each compose service once before diffing.
    var_env_keys = {
        "sharedEnv": _literal_env_keys(bicep_var_block(bicep_text, "sharedEnv")),
        "sharedSecretEnvRefs": _literal_env_keys(bicep_var_block(bicep_text, "sharedSecretEnvRefs")),
    }

    had_drift = False
    for role, resource_var in BICEP_RESOURCE_MAP.items():
        resource_block = bicep_resource_block(bicep_text, resource_var)
        bicep_keys = bicep_container_env_keys(resource_block, var_env_keys)
        compose_keys = compose_role_keys(role)

        only_compose = compose_keys - bicep_keys - ALLOWED_ONLY_IN_COMPOSE.get(role, set())
        only_bicep = bicep_keys - compose_keys - ALLOWED_ONLY_IN_BICEP.get(role, set())

        if only_compose or only_bicep:
            had_drift = True
            print(f"::error::bicep/compose drift in role '{role}' (main.bicep's {resource_var}):")
            if only_compose:
                print(f"  in docker-compose.yml/docker-compose.vm.yml but missing from infra/main.bicep: {sorted(only_compose)}")
            if only_bicep:
                print(f"  in infra/main.bicep but missing from both compose files: {sorted(only_bicep)}")
            print(
                "  -> either add the missing key to the other side, or if this is "
                "intentional, add it to ALLOWED_ONLY_IN_COMPOSE/ALLOWED_ONLY_IN_BICEP in "
                "scripts/check-bicep-compose-drift.py with a one-line reason."
            )

    if had_drift:
        print(
            "\n::error::infra/main.bicep and the docker-compose files disagree on which "
            "env vars the backend/frontend images accept -- this is the same bug class "
            "check-compose-drift.py guards against between the two compose files, just on "
            "the ACA deploy path too. See this script's module docstring."
        )
        return 1

    print("No unexplained env var drift between infra/main.bicep and the compose files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
