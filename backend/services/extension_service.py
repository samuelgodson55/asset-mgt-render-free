"""
services/extension_service.py
--------------------------------
The due-date extension request workflow:

  POST /checkouts/{id}/extension-requests           -- create a request
  GET  /checkouts/extension-requests                -- list requests (privileged)
  POST /checkouts/extension-requests/{id}/decision   -- approve/deny (privileged)

...plus a second, simpler path for a Manager/Admin/Super Admin who wants to
just grant more time directly, without a request-and-decision round trip:

  POST /checkouts/{id}/extend                        -- direct grant (privileged)

WHO CAN ASK FOR MORE TIME
---------------------------
  - A regular User (staff/customer/manager/admin) can request an extension
    on THEIR OWN active checkout -- self-service, same permission model as
    GET /users/me/items.
  - An Ad-Hoc Individual (Outsider) has no login at all, so they can't hit
    this endpoint themselves. Instead, a Manager/Admin/Super Admin logs the
    request ON THEIR BEHALF (e.g. after a phone call or email) -- see
    create_extension_request()'s branch for `checkout.outsider`.

WHO CAN GRANT MORE TIME
--------------------------
Only a Manager, Admin, or Super Admin can approve or deny a request (see
api/checkouts.py's `require_privileged_role` dependency on the decision
route) -- approving one is the ONLY way an AssetCheckout's due_date changes
after it was first set at dispatch time, UNLESS that same
Manager/Admin/Super Admin uses extend_checkout_directly() below to just set
a new due date themselves straight from the Custody Ledger drawer (User
Directory / Ad-Hoc Directory on admin.html / manager.html) -- useful when
they're the one initiating the extension (e.g. on a phone call with the
holder) rather than reacting to a request someone else filed. Both paths
write the same AuditLog action family and notify the holder the same way;
the only difference is whether a pending ExtensionRequest row exists first.
"""

import datetime
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from models import utc_now
from schemas.checkouts import ExtensionRequestCreate, ExtensionDecisionRequest, DirectExtensionRequest, BulkExtendRequest
from tasks.notification_tasks import send_email_task
from config import settings

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

_FULL_ADMIN_ROLES = ("super_admin", "admin")


def _as_utc_datetime(d: datetime.date) -> datetime.datetime:
    """Turns a plain calendar date (from the `<input type="date">` field) into
    an end-of-day, timezone-aware datetime -- same convention as every other
    due_date already stored on AssetCheckout (see models.py's TIMEZONE
    HANDLING note)."""
    return datetime.datetime.combine(d, datetime.time.max, tzinfo=datetime.timezone.utc)


def _coerce_aware(dt: datetime.datetime | None) -> datetime.datetime | None:
    """
    On PostgreSQL (production), every `DateTime(timezone=True)` column always
    comes back as a timezone-aware datetime already stamped UTC -- see
    models.py's TIMEZONE HANDLING note. SQLite (used for local dev/tests --
    see .env.example) doesn't have a real TIMESTAMPTZ type, so the same
    column can come back NAIVE there instead. Rather than let that surface
    as a `TypeError: can't compare offset-naive and offset-aware datetimes`
    the moment someone runs this against SQLite, treat a naive value as
    "already UTC" (which it is, in practice -- see utc_now()) and label it
    as such before comparing.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _notify(to, subject: str, body: str) -> None:
    """
    Enqueues one email via tasks.send_email_task instead of calling
    notification_service.send_email() inline -- see that task's docstring
    for why (short version: it keeps a slow/misconfigured SMTP server from
    holding this request/response open, which is what used to make the
    Request Extension modal and the Extension Requests panel both feel
    like they hung before clearing).

    `.delay()` itself can raise if Redis/the broker is unreachable -- kept
    fail-soft here (log + move on) for the exact same reason
    notification_service.send_email() never raises: a checkout's due date
    already changed and was already committed by the time this is called,
    so a broker hiccup should never turn into a 500 on an otherwise-
    successful extension request/decision.
    """
    try:
        send_email_task.delay(to=to, subject=subject, body=body)
    except Exception:
        logger.warning("Failed to enqueue notification email %r", subject, exc_info=True)


def _notification_recipients(db) -> list[str]:
    """
    Builds the list of email addresses who should hear about a NEW
    extension request: every Admin AND every Manager, system-wide (Managers
    no longer have department-scoping, so any Manager can act on any
    checkout, same as an Admin), plus any extra addresses configured in
    ADMIN_NOTIFICATION_EMAILS.
    """
    admins_and_managers = db.query(models.User).filter(
        models.User.is_deleted.is_(False),
        models.User.is_active.is_(True),
        models.User.role.in_(("admin", "manager")),
    ).all()

    recipients = [u.email for u in admins_and_managers]

    recipients.extend(settings.admin_notification_email_list)
    return list(dict.fromkeys(recipients))  # de-dupe, preserve order


def create_extension_request(db: Session, checkout_id: int, req: ExtensionRequestCreate, user: dict) -> dict:
    checkout = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.id == checkout_id, models.AssetCheckout.status == "active"
    ).first()
    if not checkout:
        raise HTTPException(status_code=404, detail="Active checkout record not found.")

    is_privileged = user["role"] in (*_FULL_ADMIN_ROLES, "manager")

    if checkout.outsider_id is not None:
        # Ad-Hoc Individuals have no login -- only a Manager/Admin/Super
        # Admin can log a request on their behalf.
        if not is_privileged:
            raise HTTPException(
                status_code=403,
                detail="Only a Manager or Admin can log an extension request for an Ad-Hoc Individual.",
            )
        requested_by_label = f"Ad-Hoc: {checkout.outsider.name} (logged by {user['email']})" if checkout.outsider else f"Ad-Hoc Individual (logged by {user['email']})"
    else:
        # A normal User checkout -- either the holder themselves (self-
        # service), or a privileged account requesting on their behalf.
        if not is_privileged and str(checkout.user_id) != str(user["sub"]):
            raise HTTPException(
                status_code=403,
                detail="You may only request an extension on your own checkout.",
            )
        if str(checkout.user_id) == str(user["sub"]):
            requested_by_label = f"{user['name']} ({user['email']})"
        else:
            requested_by_label = f"{checkout.user.name} ({checkout.user.email}) -- logged by {user['email']}" if checkout.user else f"Logged by {user['email']}"

    new_due_date = _as_utc_datetime(req.new_due_date)
    existing_due_date = _coerce_aware(checkout.due_date)
    if existing_due_date and new_due_date <= existing_due_date:
        raise HTTPException(
            status_code=400,
            detail="The requested new due date must be later than the current due date.",
        )

    extension_request = models.ExtensionRequest(
        checkout_id=checkout.id,
        requested_by_label=requested_by_label,
        previous_due_date=checkout.due_date,
        requested_new_due_date=new_due_date,
        reason=req.reason,
        status="pending",
    )
    db.add(extension_request)

    db.add(models.AuditLog(
        operator=user["email"], action="EXTENSION_REQUESTED", target_type="AssetCheckout", target_id=checkout.id,
        details=(
            f"Extension requested on '{checkout.asset.name if checkout.asset else 'Unknown Asset'}' "
            f"by {requested_by_label} -- new due date requested: {req.new_due_date.isoformat()}."
        ),
    ))
    db.commit()
    db.refresh(extension_request)

    logger.info(
        "Extension request created", extra={
            "user": user["email"], "checkout_id": checkout.id, "extension_request_id": extension_request.id,
        },
    )

    recipients = _notification_recipients(db)
    if recipients:
        asset_name = checkout.asset.name if checkout.asset else "Unknown Asset"
        _notify(
            to=recipients,
            subject=f"[Snipe-IT Lite] Extension requested: {asset_name}",
            body=(
                f"{requested_by_label} has requested to extend the due date on '{asset_name}'.\n\n"
                f"Current due date: {checkout.due_date.strftime('%Y-%m-%d') if checkout.due_date else 'None'}\n"
                f"Requested new due date: {req.new_due_date.isoformat()}\n"
                f"Reason: {req.reason or '(none given)'}\n\n"
                "Review and decide this request from the Extension Requests panel on your dashboard."
            ),
        )

    return {
        "id": extension_request.id,
        "checkout_id": checkout.id,
        "status": extension_request.status,
        "requested_new_due_date": req.new_due_date.isoformat(),
        "message": "Extension request submitted for review.",
    }


def list_extension_requests(db: Session, user: dict, status: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    """
    GET /checkouts/extension-requests -- privileged only. Managers now see
    every request system-wide, same as an Admin/Super Admin (no more
    department-scoping).
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    query = db.query(models.ExtensionRequest)
    if status:
        query = query.filter(models.ExtensionRequest.status == status)

    total = query.count()
    rows = query.order_by(models.ExtensionRequest.created_at.desc()).offset(offset).limit(limit).all()

    items = []
    for r in rows:
        checkout = r.checkout
        if checkout and checkout.user:
            assignee_name, entity_id, entity_type = checkout.user.name, checkout.user.id, "user"
        elif checkout and checkout.outsider:
            assignee_name, entity_id, entity_type = checkout.outsider.name, checkout.outsider.id, "outsider"
        else:
            assignee_name, entity_id, entity_type = "Unknown", None, None

        items.append({
            "id": r.id,
            "checkout_id": r.checkout_id,
            "asset_name": checkout.asset.name if checkout and checkout.asset else "Unknown Asset",
            "assignee_name": assignee_name,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "requested_by_label": r.requested_by_label,
            "previous_due_date": r.previous_due_date.strftime("%Y-%m-%d") if r.previous_due_date else None,
            "requested_new_due_date": r.requested_new_due_date.strftime("%Y-%m-%d"),
            "reason": r.reason,
            "status": r.status,
            "decided_by": r.decided_by,
            # TIMEZONE FIX -- see services/user_service.py's
            # get_my_assigned_items() for the full explanation of why
            # these are `.isoformat()` and not pre-formatted
            # `.strftime(...)` strings.
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            "decision_note": r.decision_note,
            "created_at": r.created_at.isoformat(),
        })

    return {"items": items, "total": total, "limit": limit, "offset": offset}


# How far back a decided (approved/denied) request stays eligible to show
# up in the self-service "recent decisions" banner below -- see
# list_my_recent_extension_decisions()'s docstring for the full rationale.
DECISION_ALERT_WINDOW_DAYS = 14


def list_my_recent_extension_decisions(db: Session, user: dict, limit: int = 10) -> dict:
    """
    Self-service, in-app counterpart to the email decide_extension_request()
    already sends the checkout holder below -- someone doesn't always see
    (or have) email, and shouldn't have to go dig through the Audit Trail
    to find out their request was approved/denied. Powers the dismissible
    banner on staff.html/customer.html (and admin/manager dashboards, since
    those roles can self-service-request extensions on their own checkouts
    too) -- see js/components/extensions.js's loadMyExtensionDecisionsAlert().

    Scoped to AssetCheckout.user_id (the actual holder the item is loaned
    to -- the same recipient the email goes to), NOT to
    ExtensionRequest.requested_by_label, so this still resolves correctly
    even when a Manager/Admin logged the original request on someone
    else's behalf -- the holder still gets notified once it's decided,
    same as the email does.

    Bounded to the last DECISION_ALERT_WINDOW_DAYS so a years-old decision
    from long before this feature shipped doesn't suddenly resurface for
    everyone the first time they load the dashboard after an upgrade. The
    frontend also remembers a per-request dismissal (same
    localStorage-signature pattern as every other alert banner in this
    app -- see js/ui.js's isAlertDismissed()/setAlertDismissed()), so
    within that window a decision still only ever needs to be seen once.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    cutoff = utc_now() - datetime.timedelta(days=DECISION_ALERT_WINDOW_DAYS)

    requests = (
        db.query(models.ExtensionRequest)
        .join(models.AssetCheckout, models.ExtensionRequest.checkout_id == models.AssetCheckout.id)
        .filter(
            models.AssetCheckout.user_id == int(user["sub"]),
            models.ExtensionRequest.status.in_(("approved", "denied")),
            models.ExtensionRequest.decided_at.isnot(None),
            models.ExtensionRequest.decided_at >= cutoff,
        )
        .order_by(models.ExtensionRequest.decided_at.desc())
        .limit(limit)
        .all()
    )

    items = []
    for r in requests:
        checkout = r.checkout
        items.append({
            "id": r.id,
            "checkout_id": r.checkout_id,
            "asset_name": checkout.asset.name if checkout and checkout.asset else "Unknown Asset",
            "status": r.status,
            "requested_new_due_date": r.requested_new_due_date.strftime("%Y-%m-%d") if r.requested_new_due_date else None,
            "due_date": checkout.due_date.strftime("%Y-%m-%d") if checkout and checkout.due_date else None,
            "decision_note": r.decision_note,
            # TIMEZONE FIX -- see services/user_service.py's
            # get_my_assigned_items() for the full explanation of why this
            # is `.isoformat()` and not a pre-formatted `.strftime(...)`
            # string; the frontend banner formats it for display.
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        })

    return {"items": items, "total": len(items)}


def decide_extension_request(db: Session, request_id: int, decision: ExtensionDecisionRequest, user: dict) -> dict:
    """
    Approves or denies a pending extension request. Approving is the ONLY
    code path that updates AssetCheckout.due_date after dispatch -- it sets
    it to `decision.override_due_date` if the Manager/Admin chose a
    different date than what was originally requested, otherwise to
    exactly what was requested.
    """
    extension_request = db.query(models.ExtensionRequest).filter(models.ExtensionRequest.id == request_id).first()
    if not extension_request:
        raise HTTPException(status_code=404, detail="Extension request not found.")
    if extension_request.status != "pending":
        raise HTTPException(status_code=400, detail=f"This request was already {extension_request.status}.")

    checkout = extension_request.checkout
    if not checkout:
        raise HTTPException(status_code=404, detail="The underlying checkout record no longer exists.")

    final_due_date = extension_request.requested_new_due_date
    if decision.approve and decision.override_due_date:
        final_due_date = _as_utc_datetime(decision.override_due_date)

    extension_request.decided_by = user["email"]
    extension_request.decided_at = utc_now()
    extension_request.decision_note = decision.note

    asset_name = checkout.asset.name if checkout.asset else "Unknown Asset"

    if decision.approve:
        extension_request.status = "approved"
        checkout.due_date = final_due_date
        audit_details = (
            f"Approved extension on '{asset_name}' requested by {extension_request.requested_by_label} -- "
            f"new due date: {final_due_date.strftime('%Y-%m-%d')}."
        )
        action = "EXTENSION_APPROVED"
    else:
        extension_request.status = "denied"
        audit_details = f"Denied extension on '{asset_name}' requested by {extension_request.requested_by_label}."
        action = "EXTENSION_DENIED"

    db.add(models.AuditLog(
        operator=user["email"], action=action, target_type="AssetCheckout", target_id=checkout.id, details=audit_details,
    ))
    db.commit()

    logger.info(
        "Extension request decided", extra={
            "user": user["email"], "checkout_id": checkout.id, "extension_request_id": extension_request.id,
            "decision": extension_request.status,
        },
    )

    # Notify the requester back, if they're a linked User with an email
    # address. (Ad-Hoc Individuals recorded via `requested_by_label` have
    # no account/inbox this app knows about -- see Outsider.contact_details
    # for their phone/email, which isn't necessarily an email at all, so we
    # deliberately don't try to guess-parse it here.)
    if checkout.user and checkout.user.email:
        if decision.approve:
            subject = f"[Snipe-IT Lite] Extension approved: {asset_name}"
            body = (
                f"Your extension request on '{asset_name}' was approved.\n\n"
                f"New due date: {final_due_date.strftime('%Y-%m-%d')}\n"
                f"Note from {user['email']}: {decision.note or '(none)'}"
            )
        else:
            subject = f"[Snipe-IT Lite] Extension denied: {asset_name}"
            body = (
                f"Your extension request on '{asset_name}' was denied.\n\n"
                f"Current due date remains: {checkout.due_date.strftime('%Y-%m-%d') if checkout.due_date else 'None'}\n"
                f"Note from {user['email']}: {decision.note or '(none)'}"
            )
        _notify(to=checkout.user.email, subject=subject, body=body)

    return {
        "id": extension_request.id,
        "status": extension_request.status,
        "due_date": checkout.due_date.strftime("%Y-%m-%d") if checkout.due_date else None,
        "message": f"Extension request {extension_request.status}.",
    }


def extend_checkout_directly(db: Session, checkout_id: int, req: DirectExtensionRequest, user: dict) -> dict:
    """
    POST /checkouts/{id}/extend -- a Manager/Admin/Super Admin sets a new
    due date on an active checkout immediately, with no separate
    ExtensionRequest/approval round trip. Lives right next to "Process
    Return" in the Custody Ledger drawer (User Directory AND Ad-Hoc
    Directory both use it -- see components/custody.js) so a Manager/Admin
    can grant more time on the spot while they're already looking at
    someone's checked-out items, instead of first having to log a request
    on the holder's behalf and then immediately approve it themselves.

    Managers now have no department-scoping (same as decide_extension_request()):
    a Manager can do this for any active checkout, same as an Admin/Super
    Admin.
    """
    checkout = db.query(models.AssetCheckout).filter(
        models.AssetCheckout.id == checkout_id, models.AssetCheckout.status == "active"
    ).first()
    if not checkout:
        raise HTTPException(status_code=404, detail="Active checkout record not found.")

    new_due_date = _as_utc_datetime(req.new_due_date)
    existing_due_date = _coerce_aware(checkout.due_date)
    if existing_due_date and new_due_date <= existing_due_date:
        raise HTTPException(
            status_code=400,
            detail="The new due date must be later than the current due date.",
        )

    previous_due_date = checkout.due_date
    checkout.due_date = new_due_date
    asset_name = checkout.asset.name if checkout.asset else "Unknown Asset"
    holder_label = checkout.user.name if checkout.user else (checkout.outsider.name if checkout.outsider else "Unknown holder")

    db.add(models.AuditLog(
        operator=user["email"], action="EXTENSION_GRANTED", target_type="AssetCheckout", target_id=checkout.id,
        details=(
            f"{user['email']} granted a direct extension on '{asset_name}' for {holder_label} -- "
            f"due date changed from {previous_due_date.strftime('%Y-%m-%d') if previous_due_date else 'None'} "
            f"to {new_due_date.strftime('%Y-%m-%d')}."
            + (f" Reason: {req.reason}" if req.reason else "")
        ),
    ))
    db.commit()

    logger.info(
        "Checkout extended directly", extra={
            "user": user["email"], "checkout_id": checkout.id, "new_due_date": new_due_date.isoformat(),
        },
    )

    if checkout.user and checkout.user.email:
        _notify(
            to=checkout.user.email,
            subject=f"[Snipe-IT Lite] Due date extended: {asset_name}",
            body=(
                f"{user['email']} extended the due date on '{asset_name}'.\n\n"
                f"New due date: {new_due_date.strftime('%Y-%m-%d')}\n"
                f"Reason: {req.reason or '(none given)'}"
            ),
        )

    return {
        "checkout_id": checkout.id,
        "due_date": checkout.due_date.strftime("%Y-%m-%d"),
        "message": "Due date extended.",
    }


def extend_checkouts_bulk(db: Session, req: BulkExtendRequest, user: dict) -> dict:
    """
    POST /checkouts/bulk-extend -- applies ONE new due date to MANY active
    checkouts at once, powering the Custody Ledger drawer's "Bulk Extend
    Selected" action (components/custody.js) -- the same checkbox-selection
    UI already used for Bulk Process Returns, just for extensions instead.

    Each checkout is extended independently through extend_checkout_directly()
    -- same validation (must be active, new date must be later than the
    current one), same audit trail entry, same holder-notification email --
    so this is genuinely "do the single-extend action N times" rather than a
    separate code path. A failure on one checkout_id (already returned, or
    this particular due date isn't actually later than its current one) is
    recorded per-item and does NOT stop the rest of the batch from going
    through -- a bulk action over a handful of unrelated people's equipment
    shouldn't succeed-or-fail as all-or-nothing.
    """
    single_req = DirectExtensionRequest(new_due_date=req.new_due_date, reason=req.reason)
    results = []
    for checkout_id in req.checkout_ids:
        try:
            outcome = extend_checkout_directly(db, checkout_id, single_req, user)
            results.append({"checkout_id": checkout_id, "success": True, "due_date": outcome["due_date"]})
        except HTTPException as e:
            db.rollback()
            results.append({"checkout_id": checkout_id, "success": False, "error": e.detail})

    succeeded = sum(1 for r in results if r["success"])
    return {
        "results": results,
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "message": f"Extended {succeeded} of {len(results)} selected item(s).",
    }
