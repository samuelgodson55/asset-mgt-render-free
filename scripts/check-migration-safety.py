#!/usr/bin/env python3
"""Fail CI when a NEW Alembic upgrade is destructive.

Zero-downtime deployments keep the previous revision serving while the new
revision comes online. Upgrade migrations therefore need to be backward
compatible with the previous code. Historical migrations 0007 and 0008 are
explicitly allowlisted because they predate this guard and have already been
reviewed/accepted by the existing schema history.

Future schema changes should use:
  expand -> deploy compatible code -> migrate/use -> contract later

A destructive change can still be made deliberately, but it must first be
split into compatible releases instead of bypassing this check.
"""

from __future__ import annotations

import ast
import pathlib
import sys

VERSIONS = pathlib.Path("backend/alembic/versions")
ALLOWLIST = {"0007_split_contact_details.py", "0008_super_admin_totp.py"}
DESTRUCTIVE = {"drop_column", "drop_table", "drop_constraint", "alter_column"}

violations: list[str] = []

for path in sorted(VERSIONS.glob("*.py")):
    if path.name in ALLOWLIST or path.name == "__init__.py":
        continue
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "upgrade":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                if isinstance(child.func.value, ast.Name) and child.func.value.id == "op" and child.func.attr in DESTRUCTIVE:
                    violations.append(f"{path}:{child.lineno}: op.{child.func.attr}() in upgrade()")

if violations:
    print("DESTRUCTIVE ALEMBIC UPGRADE DETECTED", file=sys.stderr)
    print("Use an expand/contract migration across separate releases:", file=sys.stderr)
    print("  1. add new schema without breaking old code", file=sys.stderr)
    print("  2. deploy compatible code", file=sys.stderr)
    print("  3. backfill/switch usage", file=sys.stderr)
    print("  4. remove old schema only in a later release", file=sys.stderr)
    print("\n".join(violations), file=sys.stderr)
    raise SystemExit(1)

print("Migration safety check passed: no destructive operations in new upgrade() functions.")
