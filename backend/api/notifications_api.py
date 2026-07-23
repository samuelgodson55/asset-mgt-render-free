"""
api/notifications.py
----------------------
Admin/Super Admin-only settings surface for the daily overdue/due-soon
digest's recipient list (see services/notification_service.py's
get_digest_recipient_emails()/set_digest_recipient_emails() and
tasks/notification_tasks.py, which is the only thing that actually reads
this list to decide who gets the digest). Gated the same way the VAT
setting is (api/quotations.py's /settings/vat) -- `require_super_admin`,
since this is an operational/notification-routing setting, not something
every role needs to read.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from deps import require_super_admin
from schemas.notifications_schema import DigestRecipientsUpdateRequest
import services.notification_service as notification_service

router = APIRouter(tags=["notifications"])


@router.get("/settings/digest-recipients")
def get_digest_recipients(db: Session = Depends(get_db), user: dict = Depends(require_super_admin)):
    """The current list of email addresses that receive the daily overdue/due-soon digest -- Admin/Super Admin only."""
    return {"emails": notification_service.get_digest_recipient_emails(db)}


@router.put("/settings/digest-recipients")
def update_digest_recipients(
    payload: DigestRecipientsUpdateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(require_super_admin),
):
    """Replaces the entire digest recipients list. Admin/Super Admin only -- takes effect on the next scheduled digest run, no restart needed."""
    return notification_service.set_digest_recipient_emails(db, payload, user)
