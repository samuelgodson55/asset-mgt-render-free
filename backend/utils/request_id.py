"""
utils/request_id.py
--------------------
Shared "is this string safe to trust as a log-correlation ID" check for
anything in `backend/` that accepts a request ID from outside this process.

WHY THIS IS ITS OWN MODULE
---------------------------
This logic used to live only in middleware/request_context.py, applied
solely to the incoming `X-Request-ID` *header* (untrusted -- any browser
can set it, see that module's docstring for the full trust discussion).
api/telemetry_api.py's `client_error()` accepts a second, independently
untrusted request ID: `payload.request_id`, a JSON *body* field the
browser can also set to anything, sent alongside a self-reported
`request_id_confidence` flag claiming ("correlated") that this ID names
the specific request that actually failed. Trusting that claim without
also validating the string itself would let an attacker's claimed
"correlated" ID -- not just an honest breadcrumb -- flow unsanitized into
structured JSON logs, ErrorBeacon's stored incident data, and the
Telegram alert card, all as a plain 200-char string wITH NO charset
restriction (Pydantic's `Field(max_length=200)` bounds length only).

Any *value* of an ID -- not just its origin (header vs. body) -- needs the
same charset/length sanity check before it's trusted as a log key, so this
lives in one place both callers import, instead of a second regex slowly
drifting from the first.
"""

import re

# Generous enough for a UUID4 (36 chars) or a short human-readable
# reference (e.g. the "REQ-8F4A21" shape used in tests/fixtures) with
# headroom, but small enough to keep a malicious/broken caller from
# stuffing an oversized value into every log line for the request.
MAX_REQUEST_ID_LENGTH = 128

# Letters, digits, and the handful of separators that show up in UUIDs and
# human-readable reference IDs. Deliberately excludes whitespace/control
# characters (log-injection risk in non-JSON log sinks, e.g. `docker
# compose logs` piped straight to a terminal -- ANSI escape sequences or
# embedded newlines could otherwise spoof/obscure adjacent log lines) and
# anything else that isn't needed for a correlation ID.
#
# NOTE: uses \A...\Z, not ^...$ -- in Python, `$` matches immediately
# before a trailing newline as well as at the true end of string, so
# "legit-id\n" would otherwise slip past this check. \A/\Z anchor to the
# actual start/end of the string with no such exception, which matters
# here precisely because "does this contain a newline" is one of the
# things this regex exists to catch.
_SAFE_REQUEST_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,%d}\Z" % MAX_REQUEST_ID_LENGTH)


def sanitize_request_id(raw: str | None) -> str | None:
    """Return `raw` unchanged if it's safe to trust as a log key / span
    attribute, else None.

    Callers should treat a None return exactly as if nothing had been
    supplied at all -- e.g. middleware/request_context.py falls back to
    minting a fresh UUID4, and api/telemetry_api.py falls back to treating
    the report as uncorrelated regardless of what `request_id_confidence`
    claimed, since an unsafe string can't be trusted to *be* the ID it
    claims to be, let alone to correctly identify the request that failed.
    """
    if not raw:
        return None
    if not _SAFE_REQUEST_ID_RE.match(raw):
        return None
    return raw
