# Shared pytest fixtures/setup for errorbeacon/tests. Currently only does
# path bootstrapping (no fixtures yet), but pytest auto-discovers this file
# by name, so it's the right place to add shared fixtures later.
import sys
from pathlib import Path

# errorbeacon/app imports things like `from shared.errorbeacon_sanitization
# import ...` (a top-level package living at the repo root, shared with
# backend/ -- see shared/__init__.py). Tests are run from inside
# errorbeacon/ (e.g. `cd errorbeacon && pytest tests -v`), so without this,
# `sys.path` would only contain errorbeacon/ itself and those imports would
# fail with ModuleNotFoundError. Insert the repo root once, at collection
# time, before any test module imports app code.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

