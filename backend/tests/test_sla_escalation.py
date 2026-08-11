"""
tests/test_sla_escalation.py
-------------------------------
Coverage for tasks/sla_tasks.py's two Celery Beat jobs
(`escalate_pending_extension_requests`/`escalate_pending_quotations`) --
the SLA-nudge digest for `ExtensionRequest`/`Quotation` rows stuck in a
"pending" state with no automatic escalation of their own. Exercises the
"nudge, then cool down" rule in `_due_for_escalation()` directly (fast,
no DB), then each task end-to-end against purpose-built fixtures, mirroring
tests/test_reports.py's `db_session` + direct `models.*(...)` construction
style rather than going through the HTTP API (there's no user-facing
endpoint that creates a week-old pending row).

WHY `monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)`
------------------------------------------------------------------
tasks/sla_tasks.py does `from database import SessionLocal` at IMPORT
time, binding that name into its own module namespace once, at process
startup -- long before any test's `db_engine` fixture ever runs. The
`db_engine` fixture (see conftest.py) only swaps `database.SessionLocal`
itself; it can't retroactively change a name sla_tasks.py already copied
into its own namespace. So each task-level test below re-points
`sla_tasks.SessionLocal` directly at the fixture's own SQLite
sessionmaker -- otherwise `SessionLocal()` inside the task would open a
connection to the real (unconfigured, unreachable in this env) Postgres
URL instead of the test database the fixture just seeded. This is the
exact same gap noted in the task write-up: it would affect
tasks/notification_tasks.py too, if that module had tests today.
"""

import datetime

import models
import tasks.sla_tasks as sla_tasks
from services.notification_service import set_digest_recipient_emails
from schemas.notifications_schema import DigestRecipientsUpdateRequest


# ---------------------------------------------------------------------------
# _due_for_escalation() -- pure function, no DB needed
# ---------------------------------------------------------------------------
def test_due_for_escalation_false_before_sla_elapsed():
    now = models.utc_now()
    pending_since = now - datetime.timedelta(hours=5)
    assert sla_tasks._due_for_escalation(pending_since, None, sla_hours=24) is False


def test_due_for_escalation_true_first_time_past_sla_with_no_prior_nudge():
    now = models.utc_now()
    pending_since = now - datetime.timedelta(hours=48)
    assert sla_tasks._due_for_escalation(pending_since, None, sla_hours=24) is True


def test_due_for_escalation_false_during_cooldown_after_a_nudge():
    now = models.utc_now()
    pending_since = now - datetime.timedelta(hours=72)
    last_reminded_at = now - datetime.timedelta(hours=2)  # nudged 2h ago
    assert sla_tasks._due_for_escalation(pending_since, last_reminded_at, sla_hours=24) is False


def test_due_for_escalation_true_again_once_repeat_cooldown_passes(monkeypatch):
    monkeypatch.setattr(sla_tasks.settings, "APPROVAL_SLA_ESCALATION_REPEAT_HOURS", 24)
    now = models.utc_now()
    pending_since = now - datetime.timedelta(hours=72)
    last_reminded_at = now - datetime.timedelta(hours=25)  # just past the 24h cooldown
    assert sla_tasks._due_for_escalation(pending_since, last_reminded_at, sla_hours=24) is True


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------
def _make_checkout(db, *, due_hours_ago=100):
    asset_type = db.query(models.AssetType).filter(models.AssetType.is_deleted.is_(False)).first()
    staff = db.query(models.User).filter(models.User.role == "staff").first()
    now = models.utc_now()
    checkout = models.AssetCheckout(
        asset_id=asset_type.id,
        user_id=staff.id,
        quantity=1,
        checkout_date=now - datetime.timedelta(hours=due_hours_ago + 24),
        due_date=now + datetime.timedelta(days=30),
        status="active",
    )
    db.add(checkout)
    db.commit()
    db.refresh(checkout)
    return checkout


def _configure_digest_recipients(db, emails):
    set_digest_recipient_emails(
        db, DigestRecipientsUpdateRequest(emails=emails), {"email": "test-runner@corp.io"},
    )


# ---------------------------------------------------------------------------
# escalate_pending_extension_requests
# ---------------------------------------------------------------------------
def test_escalate_pending_extension_requests_nudges_overdue_row(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)
    monkeypatch.setattr(sla_tasks.settings, "EXTENSION_REQUEST_SLA_HOURS", 24)
    monkeypatch.setattr(sla_tasks.settings, "NOTIFICATIONS_ENABLED", True)

    sent = []
    monkeypatch.setattr(
        sla_tasks.notification_service, "send_email",
        lambda to, subject, body: sent.append((list(to), subject, body)) or True,
    )

    db = db_session
    _configure_digest_recipients(db, ["ops@example.com"])
    checkout = _make_checkout(db)

    now = models.utc_now()
    overdue = models.ExtensionRequest(
        checkout_id=checkout.id,
        requested_by_label="Chidinma Okafor (c.okafor@corp.io)",
        previous_due_date=checkout.due_date,
        requested_new_due_date=checkout.due_date + datetime.timedelta(days=7),
        status="pending",
        created_at=now - datetime.timedelta(hours=48),  # past the 24h SLA
    )
    not_yet_due = models.ExtensionRequest(
        checkout_id=checkout.id,
        requested_by_label="Femi Adeyemi (f.adeyemi@corp.io)",
        previous_due_date=checkout.due_date,
        requested_new_due_date=checkout.due_date + datetime.timedelta(days=3),
        status="pending",
        created_at=now - datetime.timedelta(hours=2),  # well within the SLA
    )
    db.add_all([overdue, not_yet_due])
    db.commit()

    result = sla_tasks.escalate_pending_extension_requests()

    assert result == {"escalated_count": 1, "digest_emails_sent": 1}
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == ["ops@example.com"]
    assert "1 extension request" in subject
    assert "Chidinma Okafor" in body
    assert "Femi Adeyemi" not in body  # not yet past SLA -- must not be in the digest

    db.refresh(overdue)
    db.refresh(not_yet_due)
    assert overdue.sla_last_reminded_at is not None
    assert not_yet_due.sla_last_reminded_at is None


def test_escalate_pending_extension_requests_no_recipients_still_counts_and_stamps(db_session, db_engine, monkeypatch):
    # No Digest Recipients configured and ADMIN_NOTIFICATION_EMAILS is
    # empty (see conftest.py's test env) -- _notification_recipients()
    # returns [], so send_email() must never be called (digest_emails_sent
    # stays 0), but the row still crossed its SLA threshold: it's counted
    # in escalated_count and still stamped with sla_last_reminded_at, same
    # as the code's plain `if recipients:` gate that only skips the SEND,
    # not the "this was evaluated" bookkeeping.
    monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)
    monkeypatch.setattr(sla_tasks.settings, "EXTENSION_REQUEST_SLA_HOURS", 24)

    called = []
    monkeypatch.setattr(sla_tasks.notification_service, "send_email", lambda **kw: called.append(kw) or True)

    db = db_session
    checkout = _make_checkout(db)
    overdue = models.ExtensionRequest(
        checkout_id=checkout.id,
        requested_by_label="Ad-Hoc: Femi Adeyemi (Lagos Fintech Ltd.)",
        previous_due_date=checkout.due_date,
        requested_new_due_date=checkout.due_date + datetime.timedelta(days=7),
        status="pending",
        created_at=models.utc_now() - datetime.timedelta(hours=48),
    )
    db.add(overdue)
    db.commit()

    result = sla_tasks.escalate_pending_extension_requests()

    assert result == {"escalated_count": 1, "digest_emails_sent": 0}
    assert called == []
    db.refresh(overdue)
    assert overdue.sla_last_reminded_at is not None


def test_escalate_pending_extension_requests_skips_already_decided(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)
    monkeypatch.setattr(sla_tasks.settings, "EXTENSION_REQUEST_SLA_HOURS", 24)
    monkeypatch.setattr(sla_tasks.settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(sla_tasks.notification_service, "send_email", lambda **kw: True)

    db = db_session
    _configure_digest_recipients(db, ["ops@example.com"])
    checkout = _make_checkout(db)
    decided = models.ExtensionRequest(
        checkout_id=checkout.id,
        requested_by_label="Chidinma Okafor (c.okafor@corp.io)",
        previous_due_date=checkout.due_date,
        requested_new_due_date=checkout.due_date + datetime.timedelta(days=7),
        status="approved",  # no longer "pending" -- must never match the query
        created_at=models.utc_now() - datetime.timedelta(hours=200),
        decided_by="r.adeyemi@corp.io",
        decided_at=models.utc_now(),
    )
    db.add(decided)
    db.commit()

    result = sla_tasks.escalate_pending_extension_requests()
    assert result == {"escalated_count": 0, "digest_emails_sent": 0}


def test_escalate_pending_extension_requests_respects_repeat_cooldown(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)
    monkeypatch.setattr(sla_tasks.settings, "EXTENSION_REQUEST_SLA_HOURS", 24)
    monkeypatch.setattr(sla_tasks.settings, "APPROVAL_SLA_ESCALATION_REPEAT_HOURS", 24)
    monkeypatch.setattr(sla_tasks.settings, "NOTIFICATIONS_ENABLED", True)

    called = []
    monkeypatch.setattr(sla_tasks.notification_service, "send_email", lambda **kw: called.append(kw) or True)

    db = db_session
    _configure_digest_recipients(db, ["ops@example.com"])
    checkout = _make_checkout(db)
    recently_nudged = models.ExtensionRequest(
        checkout_id=checkout.id,
        requested_by_label="Chidinma Okafor (c.okafor@corp.io)",
        previous_due_date=checkout.due_date,
        requested_new_due_date=checkout.due_date + datetime.timedelta(days=7),
        status="pending",
        created_at=models.utc_now() - datetime.timedelta(hours=72),
        sla_last_reminded_at=models.utc_now() - datetime.timedelta(hours=1),  # just nudged
    )
    db.add(recently_nudged)
    db.commit()

    result = sla_tasks.escalate_pending_extension_requests()
    assert result == {"escalated_count": 0, "digest_emails_sent": 0}
    assert called == []


# ---------------------------------------------------------------------------
# escalate_pending_quotations
# ---------------------------------------------------------------------------
def test_escalate_pending_quotations_nudges_overdue_row(db_session, db_engine, monkeypatch):
    monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)
    monkeypatch.setattr(sla_tasks.settings, "QUOTATION_SLA_HOURS", 24)
    monkeypatch.setattr(sla_tasks.settings, "NOTIFICATIONS_ENABLED", True)

    sent = []
    monkeypatch.setattr(
        sla_tasks.notification_service, "send_email",
        lambda to, subject, body: sent.append((list(to), subject, body)) or True,
    )

    db = db_session
    _configure_digest_recipients(db, ["ops@example.com"])
    requester = db.query(models.User).filter(models.User.role == "customer").first()

    now = models.utc_now()
    overdue = models.Quotation(
        user_id=requester.id,
        status="submitted",
        reference_number="QT-000999",
        submitted_at=now - datetime.timedelta(hours=48),
    )
    within_sla = models.Quotation(
        user_id=requester.id,
        status="submitted",
        reference_number="QT-000998",
        submitted_at=now - datetime.timedelta(hours=1),
    )
    db.add_all([overdue, within_sla])
    db.commit()

    result = sla_tasks.escalate_pending_quotations()

    assert result == {"escalated_count": 1, "digest_emails_sent": 1}
    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == ["ops@example.com"]
    assert "1 quotation" in subject
    assert "QT-000999" in body
    assert "QT-000998" not in body

    db.refresh(overdue)
    db.refresh(within_sla)
    assert overdue.sla_last_reminded_at is not None
    assert within_sla.sla_last_reminded_at is None


def test_escalate_pending_quotations_skips_drafts_and_approved(db_session, db_engine, monkeypatch):
    # Only `status == "submitted"` is a candidate -- a "draft" (never
    # submitted) or already-"approved"/"fulfilled" quotation must never be
    # escalated, matching approve_quotation()'s own state machine.
    monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)
    monkeypatch.setattr(sla_tasks.settings, "QUOTATION_SLA_HOURS", 24)
    monkeypatch.setattr(sla_tasks.settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(sla_tasks.notification_service, "send_email", lambda **kw: True)

    db = db_session
    _configure_digest_recipients(db, ["ops@example.com"])
    requester = db.query(models.User).filter(models.User.role == "customer").first()
    now = models.utc_now()

    draft = models.Quotation(user_id=requester.id, status="draft")
    approved = models.Quotation(
        user_id=requester.id,
        status="approved",
        reference_number="QT-000997",
        submitted_at=now - datetime.timedelta(hours=200),
        approved_at=now - datetime.timedelta(hours=100),
    )
    db.add_all([draft, approved])
    db.commit()

    result = sla_tasks.escalate_pending_quotations()
    assert result == {"escalated_count": 0, "digest_emails_sent": 0}


def test_escalate_pending_quotations_uses_admin_notification_emails_too(db_session, db_engine, monkeypatch):
    # No Digest Recipients configured, but ADMIN_NOTIFICATION_EMAILS is --
    # same "union of both lists" audience every other alert in this app
    # uses (see _notification_recipients()).
    monkeypatch.setattr(sla_tasks, "SessionLocal", db_engine)
    monkeypatch.setattr(sla_tasks.settings, "QUOTATION_SLA_HOURS", 24)
    monkeypatch.setattr(sla_tasks.settings, "NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(sla_tasks.settings, "ADMIN_NOTIFICATION_EMAILS", "oncall@example.com")

    sent = []
    monkeypatch.setattr(
        sla_tasks.notification_service, "send_email",
        lambda to, subject, body: sent.append(list(to)) or True,
    )

    db = db_session
    requester = db.query(models.User).filter(models.User.role == "customer").first()
    overdue = models.Quotation(
        user_id=requester.id,
        status="submitted",
        reference_number="QT-000996",
        submitted_at=models.utc_now() - datetime.timedelta(hours=48),
    )
    db.add(overdue)
    db.commit()

    result = sla_tasks.escalate_pending_quotations()
    assert result == {"escalated_count": 1, "digest_emails_sent": 1}
    assert sent == [["oncall@example.com"]]
