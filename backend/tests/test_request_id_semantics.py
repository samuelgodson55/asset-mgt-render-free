from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# errorbeacon/app/main.py was split into a package (see
# errorbeacon/app/main/__init__.py's docstring for the module layout) --
# concatenate every submodule so these source-level assertions still see
# the whole thing, regardless of which file a given snippet now lives in.
EB = "\n".join(p.read_text() for p in sorted((ROOT / "errorbeacon/app/main").glob("*.py")))
CLIENT = (ROOT / "backend/integrations/fastapi_errorbeacon.py").read_text()


def test_errorbeacon_preserves_upstream_request_id():
    assert '"X-Request-ID"' in CLIENT
    assert "incoming = candidate[:200]" in EB
    assert "rid = incoming or str(uuid.uuid4())" in EB


def test_health_probe_ids_are_independent():
    # Each Docker health probe is a separate request. Different IDs are intentional.
    assert "uuid.uuid4()" in EB
    assert "X-Request-ID" in EB
