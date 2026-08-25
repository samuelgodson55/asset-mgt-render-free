#!/usr/bin/env python3
"""
scripts/security/pip_audit_gate.py
-----------------------------------------------------------------------------
SECURITY GAP FIX (docs/SECURITY_GAPS.md, P1 "`pip-audit` never fails CI").

Previously: `pip-audit -r backend/requirements.txt --desc || true`. The
`|| true` meant a CRITICAL CVE in a pinned dependency printed to a log
nobody was required to read -- it could never fail a PR or block a merge,
in contrast to the Trivy image scan two jobs later, which DOES block on
CRITICAL findings.

WHY THIS ISN'T A ONE-LINE `--fail-on high` FLAG (as the gap writeup first
sketched): pip-audit has no such flag, and more fundamentally, neither of
its two vulnerability backends (PyPI's own advisory DB, the default; OSV)
attach a CVSS/severity rating to what they return -- see
`pip_audit._service.osv.OsvService.query` and `..._service.pypi`: both only
ever populate `id`, `description`, `fix_versions`, `aliases`, `published`.
There is no severity field anywhere in a `VulnerabilityResult` to gate on.
Reimplementing severity by querying OSV's raw API ourselves, in parallel to
pip-audit, would double the surface area and network dependency of this
step for a rating pip-audit itself doesn't trust enough to expose.

THE POLICY THIS SCRIPT ENFORCES INSTEAD (documented here, not a `|| true`):
pip-audit only ever reports a *known, matched* vulnerability in a *pinned*
version -- there's no "low-confidence" tier the way a SAST tool has. So the
actionable signal this repo gates on is FIXABILITY, not a severity label
pip-audit can't give us:
  - A finding WITH a fix_version available is something a maintainer can
    resolve right now by bumping a pin -- left open, it silently becomes a
    live, exploitable gap once someone reads the CVE, so it BLOCKS the
    build. (Mirrors this repo's own Trivy step, which already uses
    `ignore-unfixed` for the identical reason.)
  - A finding with NO fix_version yet has nothing to bump to -- blocking
    the build wouldn't be "fix it", it'd be "wait indefinitely with no
    lever to pull". These are REPORTED (printed in full, every run) but
    non-blocking.
  - A specific CVE ID can be granted an EXPLICIT, TIME-BOXED exception
    (see the --ignore-file below) -- this is the "or require explicit
    exception" half of this repo's severity policy, applied to the one
    tier (fixable-but-open) that would otherwise always block.

EXCEPTIONS FILE (--ignore-file, e.g. .security/pip-audit-exceptions.json):
A JSON document, one array per requirements group, each entry a reviewed,
dated, justified exception:
    {
      "backend": [
        {"id": "PYSEC-2024-XXXX", "reason": "...", "added": "2026-08-24",
         "review_by": "2026-11-24", "approved_by": "..."}
      ]
    }
Every entry MUST have a `review_by` date in the future -- an expired
exception is treated as if it doesn't exist (the finding blocks again)
rather than silently staying suppressed forever, and this script fails
loudly telling you which entry needs re-review. This is deliberately
stricter than a bare allowlist: an exception here is a scheduled
conversation, not a permanent opt-out.
-----------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys


def load_exceptions(path: str | None, group: str) -> tuple[set[str], list[str]]:
    """Returns (active_ignore_ids, error_messages_for_expired_or_malformed)."""
    if not path:
        return set(), []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return set(), []

    today = datetime.date.today()
    active: set[str] = set()
    errors: list[str] = []
    for entry in data.get(group, []):
        vuln_id = entry.get("id")
        review_by = entry.get("review_by")
        reason = entry.get("reason")
        if not vuln_id or not review_by or not reason:
            errors.append(
                f"[{group}] malformed exception entry (needs id/reason/review_by): {entry!r}"
            )
            continue
        try:
            review_date = datetime.date.fromisoformat(review_by)
        except ValueError:
            errors.append(f"[{group}] exception for {vuln_id} has an unparseable review_by: {review_by!r}")
            continue
        if review_date < today:
            errors.append(
                f"[{group}] exception for {vuln_id} EXPIRED on {review_by} -- "
                "re-review it (bump the pin, or extend review_by with a fresh justification) "
                "in the exceptions file before this can be ignored again."
            )
            continue
        active.add(vuln_id)
    return active, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-r", "--requirement", action="append", required=True,
                         help="requirements file to audit (repeatable)")
    parser.add_argument("--group", required=True,
                         help="key into --ignore-file's JSON for this component, e.g. 'backend'")
    parser.add_argument("--ignore-file", default=None,
                         help="path to the JSON exceptions file (see module docstring)")
    args = parser.parse_args()

    active_exceptions, exception_errors = load_exceptions(args.ignore_file, args.group)
    if exception_errors:
        print("::error::pip-audit exceptions file problem(s):")
        for msg in exception_errors:
            print(f"  - {msg}")
        return 1

    cmd = ["pip-audit"]
    for req in args.requirement:
        cmd += ["-r", req]
    cmd += ["--desc", "on", "-f", "json"]
    for vuln_id in sorted(active_exceptions):
        cmd += ["--ignore-vuln", vuln_id]

    print(f"::group::Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    print("::endgroup::")

    if proc.returncode == 0:
        print(f"pip-audit ({args.group}): no unresolved, non-exempted vulnerabilities found.")
        if active_exceptions:
            print(f"  (active, non-expired exceptions in effect: {', '.join(sorted(active_exceptions))})")
        return 0

    # pip-audit's own JSON was already printed above for a human to read.
    # Split findings into "has a fix" (blocking) vs "no fix yet" (report-only)
    # purely to report which tier is which -- the actual audit call above
    # already excluded --ignore-vuln entries, so anything pip-audit still
    # reports here is a real, non-exempted finding.
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # pip-audit failed before producing JSON (e.g. dependency resolution
        # error) -- treat as a hard failure, nothing to triage by tier.
        print("::error::pip-audit did not produce parseable JSON -- treating as a failure.")
        return proc.returncode

    fixable, unfixable = [], []
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            entry = f"{dep['name']}=={dep['version']}: {vuln['id']}"
            (fixable if vuln.get("fix_versions") else unfixable).append(entry)

    if unfixable:
        print(f"::warning::{args.group}: {len(unfixable)} finding(s) with no fix_version yet "
              "(report-only, not blocking):")
        for e in unfixable:
            print(f"  - {e}")

    if fixable:
        print(f"::error::{args.group}: {len(fixable)} finding(s) with a fix available and no "
              "active exception -- this blocks the build. Bump the pin, or add a time-boxed "
              f"entry to {args.ignore_file or '(no --ignore-file configured)'} with a reason "
              "and review_by date if it needs to stay open longer.")
        for e in fixable:
            print(f"  - {e}")
        return 1

    # Only unfixable (report-only) findings remain -- don't block.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
