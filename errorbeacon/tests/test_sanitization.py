from shared.errorbeacon_sanitization import clean, redact, sanitize_url


def test_redact_common_secret_shapes():
    text = (
        "Bearer abc.def.ghi password=hunter2 "
        "postgresql://user:secret@db:5432/app"
    )
    result = redact(text)
    assert "hunter2" not in result
    assert "Bearer abc.def.ghi" not in result
    assert "postgresql://user:secret@db:5432/app" not in result


def test_sanitize_url_redacts_sensitive_query_values():
    result = sanitize_url("/api/reset?token=abc123&name=sam&session_id=xyz")
    assert "token=abc123" not in result
    assert "session_id=xyz" not in result
    assert "name=sam" in result


def test_clean_redacts_nested_sensitive_keys():
    payload = {"user": {"password": "secret", "name": "sam"}}
    assert clean(payload)["user"]["password"] == "[REDACTED]"
    assert clean(payload)["user"]["name"] == "sam"


def test_clean_applies_depth_limit():
    value = {"a": {"b": {"c": "secret"}}}
    result = clean(value, max_depth=2)
    assert result["a"]["b"] == "[TRUNCATED_DEPTH]"


def test_clean_limits_collection_width():
    value = {str(i): i for i in range(10)}
    assert len(clean(value, max_items=3)) == 3
