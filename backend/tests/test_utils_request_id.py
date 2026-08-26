"""
Direct unit tests for utils/request_id.py -- the shared sanitizer used by
both middleware/request_context.py (incoming X-Request-ID header) and
api/telemetry_api.py (browser-reported request_id in a client-error body).

See test_telemetry_api.py's "request_id sanitization" section for the
end-to-end behavior this enables at the API layer.
"""

from utils.request_id import MAX_REQUEST_ID_LENGTH, sanitize_request_id


def test_none_and_empty_string_are_rejected():
    assert sanitize_request_id(None) is None
    assert sanitize_request_id("") is None


def test_well_formed_ids_are_accepted_unchanged():
    for value in [
        "req-8F4A21_valid.id-1",
        "f2cfc58337ff8895e4d080f318cbc9a1",  # uuid4-hex shape
        "a",
        "A-Z_a-z_0-9_.:-",
    ]:
        assert sanitize_request_id(value) == value


def test_rejects_embedded_newline():
    # This is the concrete attack this module exists to stop: a value that
    # would look fine truncated in a UI but injects a fake extra line when
    # a raw log stream is tailed as text (see module docstring).
    assert sanitize_request_id("legit-looking\nX-Injected-Log-Line: fake") is None


def test_rejects_embedded_carriage_return_and_tab():
    assert sanitize_request_id("id\rwith-cr") is None
    assert sanitize_request_id("id\twith-tab") is None


def test_rejects_ansi_escape_sequence():
    assert sanitize_request_id("id\x1b[31mred\x1b[0m") is None


def test_rejects_whitespace():
    assert sanitize_request_id("has space") is None


def test_rejects_disallowed_punctuation():
    for value in ["id'; DROP TABLE x;--", 'id"quoted"', "id<script>", "id/../etc"]:
        assert sanitize_request_id(value) is None


def test_length_boundary_is_inclusive_then_rejects():
    at_limit = "a" * MAX_REQUEST_ID_LENGTH
    over_limit = "a" * (MAX_REQUEST_ID_LENGTH + 1)
    assert sanitize_request_id(at_limit) == at_limit
    assert sanitize_request_id(over_limit) is None
