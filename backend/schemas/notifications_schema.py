"""
schemas/notifications.py
--------------------------
Request/response shapes for the admin-editable "Digest Recipients" list
(see services/notification_service.py's get_digest_recipient_emails()/
set_digest_recipient_emails() and api/notifications.py).
"""

import re
from pydantic import BaseModel, Field, field_validator

# A generous but finite ceiling -- same rationale as MAX_LIMIT-style caps
# elsewhere in this project (e.g. services/user_service.py's MAX_LIMIT):
# stops a malformed/pasted-in-bulk request from creating an unbounded
# AppSetting row, without getting in the way of any realistic distribution
# list.
MAX_DIGEST_RECIPIENTS = 100

# Deliberately simple/permissive (no email-validator dependency anywhere
# else in this project -- see schemas/users.py, which validates email
# fields as a plain non-blank `str`) -- this is a "did you paste something
# that isn't even shaped like an email" sanity check, not full RFC 5322
# validation. Good enough to catch typos/pasted junk without adding a new
# dependency for one settings form.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DigestRecipientsUpdateRequest(BaseModel):
    """PUT /settings/digest-recipients body -- replaces the ENTIRE list in
    one call (matches how the admin.html panel edits it: load the current
    list, add/remove rows locally, save the whole thing back), rather than
    a single add/remove-one-address endpoint."""

    emails: list[str] = Field(default_factory=list, max_length=MAX_DIGEST_RECIPIENTS)

    @field_validator("emails")
    @classmethod
    def _validate_and_normalize(cls, values: list[str]) -> list[str]:
        cleaned = []
        seen = set()
        for raw in values:
            candidate = raw.strip().lower()
            if not candidate:
                continue
            if not _EMAIL_RE.match(candidate):
                raise ValueError(f"'{raw}' doesn't look like a valid email address.")
            if candidate not in seen:
                seen.add(candidate)
                cleaned.append(candidate)
        return cleaned
