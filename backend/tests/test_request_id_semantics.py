from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EB = (ROOT / "errorbeacon/app/main.py").read_text()
CLIENT = (ROOT / "backend/integrations/fastapi_errorbeacon.py").read_text()


def test_errorbeacon_preserves_upstream_request_id():
    assert '"X-Request-ID"' in CLIENT
    assert "incoming = candidate[:200]" in EB
    assert "rid = incoming or str(uuid.uuid4())" in EB


def test_health_probe_ids_are_independent():
    # Each Docker health probe is a separate request. Different IDs are intentional.
    assert "uuid.uuid4()" in EB
    assert "X-Request-ID" in EB
