# Covers shared/errorbeacon_sanitization.py -- the module both errorbeacon
# and backend/integrations/fastapi_errorbeacon.py use to scrub request
# bodies/URLs/tracebacks before they're ever sent to ErrorBeacon or logged,
# so secrets (passwords, bearer tokens, DB connection strings, session
# tokens) never leave the process. Also covers clean()'s depth/width
# limits, which exist purely to bound payload size (a runaway nested
# object or huge dict shouldn't blow up the ingest request), separately
# from redaction itself.
from shared.errorbeacon_sanitization import clean, redact, sanitize_url


def test_redact_common_secret_shapes():
    # Three distinct secret shapes in one string: a JWT-looking bearer
    # token, a key=value password pair, and a DB connection string with an
    # embedded credential -- redact() must catch all three patterns.
    text = (
        "Bearer abc.def.ghi password=hunter2 "
        "postgresql://user:secret@db:5432/app"
    )
    result = redact(text)
    assert "hunter2" not in result
    assert "Bearer abc.def.ghi" not in result
    assert "postgresql://user:secret@db:5432/app" not in result


def test_sanitize_url_redacts_sensitive_query_values():
    # Only known-sensitive query param names (token, session_id, ...) get
    # redacted -- ordinary params like `name` must survive untouched so
    # the resulting URL is still useful for debugging.
    result = sanitize_url("/api/reset?token=abc123&name=sam&session_id=xyz")
    assert "token=abc123" not in result
    assert "session_id=xyz" not in result
    assert "name=sam" in result


def test_clean_redacts_nested_sensitive_keys():
    # clean() must recurse into nested dicts to find sensitive keys, not
    # just scan the top level.
    payload = {"user": {"password": "secret", "name": "sam"}}
    assert clean(payload)["user"]["password"] == "[REDACTED]"
    assert clean(payload)["user"]["name"] == "sam"


def test_clean_applies_depth_limit():
    # Beyond max_depth, clean() replaces the remaining nested structure
    # with a placeholder instead of continuing to recurse -- caps how deep
    # a maliciously/accidentally deeply-nested payload can make this walk.
    value = {"a": {"b": {"c": "secret"}}}
    result = clean(value, max_depth=2)
    assert result["a"]["b"] == "[TRUNCATED_DEPTH]"


def test_clean_limits_collection_width():
    # Same idea as the depth limit, but for breadth: a dict/list with more
    # than max_items entries gets truncated rather than fully included.
    value = {str(i): i for i in range(10)}
    assert len(clean(value, max_items=3)) == 3
