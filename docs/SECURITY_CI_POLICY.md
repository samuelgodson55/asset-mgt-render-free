# CI security-gate policy

This is the one place that states, for every scanner this repo runs in CI,
what happens when it finds something. The point of writing it down here is
so nobody has to reverse-engineer intent from a step's exit code, and so a
future scanner gets slotted into an existing, deliberate policy instead of
defaulting to `|| true` because that's what was fastest to get CI green.

## The policy

| Severity | Action |
|---|---|
| **Critical** | Block the build. No exceptions. |
| **High** | Block the build, unless there's an explicit, dated, reviewed exception on file for that specific finding. |
| **Medium** | Report (printed in full, every run). Non-blocking. |
| **Low** | Report. Non-blocking. |

An "exception" is never a blanket `|| true` or a permanently-silenced
finding -- every mechanism below requires a reason and a review/expiry date,
and an expired one reverts to blocking automatically. The goal is that
nothing gets silently ignored forever; it either gets fixed, or someone
signs off on why it's staying open and for how long.

## How each scanner in `.github/workflows/ci.yml` maps to this table

| Scanner | Job | Severity source | Enforcement |
|---|---|---|---|
| Trivy (container images) | `build-and-scan-images` | Trivy's own CVSS-derived CRITICAL/HIGH split | CRITICAL blocks (`exit-code: 1`, `ignore-unfixed: true`); HIGH is a separate report-only step. HIGH is documented as report-only pending a backlog triage -- see that step's own comment. |
| `pip-audit` (Python deps) | `backend`, `errorbeacon` | pip-audit has **no** severity field at all (see `scripts/security/pip_audit_gate.py`'s docstring) | Substitutes fixability for severity: a finding with a `fix_versions` entry blocks (equivalent to this table's Critical/High row -- it's actionable *right now*); a finding with none yet is report-only (equivalent to Medium/Low -- there's no lever to pull today). Exceptions: `.security/pip-audit-exceptions.json`, one entry per finding ID, each with `reason` + `review_by`; expired entries fail the gate script outright until re-reviewed. |
| `npm audit` (frontend-app deps) | `frontend-app` | npm's own advisory database's real critical/high/moderate/low rating | `--audit-level=high` step blocks on critical/high (this table's top two rows exactly); a separate `|| true` step reports the full moderate/low set. Exceptions: an explicit, commented `overrides` pin in `frontend-app/package.json` naming the advisory and a follow-up date -- npm has no per-advisory ignore flag, so this is the closest equivalent to pip-audit's `--ignore-vuln` exceptions file. |
| Gitleaks (secrets) | `secret-scan` | N/A -- a real secret is always Critical | Blocks unconditionally on any match not in `.gitleaksignore` (which itself requires the exact historical fingerprint, not a pattern -- see that file's own header). |
| CodeQL (SAST) | `sast-codeql` | GitHub code-scanning alert severity (critical/high/medium/low, assigned per-query) | **Report-only for now** -- findings upload to the repo's Security → Code scanning tab, nothing in this workflow's exit code blocks on them yet. This is a deliberate, temporary exception to the table above, not an oversight: CodeQL is brand-new to this repo as of 2026-08-24 with no triaged baseline, so gating a PR on whatever backlog it surfaces on day one would block unrelated work before anyone's looked at a single finding. **Follow-up (tracked as the new P1 in `docs/SECURITY_GAPS.md`):** once the initial findings are triaged (fixed, or explicitly accepted with a reason), turn this into an enforced Critical/High-blocks gate the same way Trivy's HIGH backlog is meant to be closed out -- via a branch-protection "required status check" on the code-scanning alert level, since `codeql-action/analyze` itself doesn't fail on findings by design. |

## Adding a new scanner

When you add a new CI security scanner, before wiring it in:

1. Confirm what severity data it actually exposes (don't assume -- pip-audit
   looked like it should have a `--fail-on-severity` flag and doesn't; check
   before designing a gate around one).
2. Map that severity to this table's four rows (or, if the tool's native
   scale doesn't line up cleanly, document the substitute the way the
   pip-audit row above documents fixability-as-severity).
3. Decide, explicitly, whether it starts report-only (new tool, unknown
   backlog -- see the CodeQL row) or blocking from day one (tool already has
   a clean baseline), and write that decision down here, not just in the
   workflow file's comments.
4. If it needs an exceptions mechanism, give exceptions a reason and an
   expiry, not just an ID -- see `.security/pip-audit-exceptions.json` for
   the shape to copy.
