import integrations.fastapi_errorbeacon as eb


def test_errorbeacon_forwards_application_request_id(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr(eb, "requests", type("R", (), {"post": staticmethod(fake_post)}))
    eb.URL = "http://errorbeacon:8000"
    eb.KEY = "test-key"
    eb.send({"request_id": "req-123", "message": "boom"})

    assert captured["headers"]["X-API-Key"] == "test-key"
    assert captured["headers"]["X-Request-ID"] == "req-123"


def test_errorbeacon_does_not_add_empty_request_id_header(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(headers=headers)

    monkeypatch.setattr(eb, "requests", type("R", (), {"post": staticmethod(fake_post)}))
    eb.KEY = "test-key"
    eb.send({"message": "background event"})

    assert "X-Request-ID" not in captured["headers"]
