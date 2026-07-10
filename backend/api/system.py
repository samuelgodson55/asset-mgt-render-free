"""
api/system.py
-------------
GET  /system/health                 -- plain, unauthenticated liveness check
POST /system/notifications/run      -- manually run the overdue-checkout
                                        digest (see tasks/notification_tasks.py)

Both routes exist specifically to make this app work well on Render's (or
any platform's) FREE tier, where there's no separate cron-job/background-
worker service type available (see scheduler.py's module docstring):

  - GET /system/health is a cheap, side-effect-free endpoint an external
    uptime pinger (UptimeRobot, cron-job.org, etc.) can hit every few
    minutes to keep a free web service from spinning down on idle -- see
    README.md's "Deploying on Render's Free Plan" section. It's
    deliberately public (no auth) so a pinger can call it with zero
    configuration.
  - POST /system/notifications/run lets an external scheduler (or a
    Super Admin, manually, from a REST client) trigger the same overdue-
    checkout digest that scheduler.py's in-process thread also runs on a
    timer -- useful because that in-process timer pauses whenever the
    free instance is spun down, so it's not fully reliable on its own.
"""

import logging

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

import models
from config import settings
from database import get_db
from deps import _FULL_ADMIN_ROLES
from security import decode_access_token, SUPER_ADMIN_ID
from tasks.notification_tasks import send_overdue_notifications

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])

# auto_error=False: unlike deps.py's `security = HTTPBearer()`, a missing
# Authorization header here should NOT immediately 401 -- the X-Task-Token
# path below might still authorize the request instead.
_optional_bearer = HTTPBearer(auto_error=False)


@router.get("/health")
def health():
    """Unauthenticated liveness check -- see this module's docstring."""
    return {"status": "ok"}


def _require_super_admin_or_task_token(
    x_task_token: str | None = Header(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: Session = Depends(get_db),
) -> None:
    """
    Authorizes the request if EITHER:
      - settings.SYSTEM_TASK_TOKEN is configured and matches the
        X-Task-Token header (the external-scheduler path), or
      - the caller holds a valid Super Admin/Admin JWT in the normal
        Authorization: Bearer header (the in-app path -- mirrors
        deps.require_super_admin, duplicated here rather than imported
        since it needs the Bearer token to be OPTIONAL at the FastAPI
        dependency level, which deps.get_current_user's HTTPBearer() is
        not).
    Raises 401/403 if neither applies.
    """
    if settings.SYSTEM_TASK_TOKEN and x_task_token == settings.SYSTEM_TASK_TOKEN:
        return

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Provide a valid X-Task-Token header, or a Super Admin/Admin Authorization: Bearer token.",
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid authentication token.")

    is_hardcoded_super_admin = payload.get("role") == "super_admin" and str(payload.get("sub")) == str(SUPER_ADMIN_ID)
    if not is_hardcoded_super_admin:
        db_user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()
        if not db_user or db_user.is_deleted or not db_user.is_active:
            raise HTTPException(status_code=401, detail="This account is no longer active. Please log in again.")

    if payload.get("role") not in _FULL_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Forbidden: Operation requires Super Admin privileges.")


@router.post("/notifications/run", dependencies=[Depends(_require_super_admin_or_task_token)])
def run_notifications():
    """
    Runs the overdue-checkout digest immediately and returns a summary --
    see tasks/notification_tasks.py's send_overdue_notifications(). See
    `_require_super_admin_or_task_token` above for the two ways to call
    this: a Super Admin/Admin session, or the X-Task-Token shared secret
    (settings.SYSTEM_TASK_TOKEN) for an external scheduler.
    """
    result = send_overdue_notifications()
    logger.info("notifications_triggered_manually", extra=result)
    return result
