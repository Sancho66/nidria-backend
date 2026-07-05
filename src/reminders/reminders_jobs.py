"""The two scheduled pipelines — SYNC (scheduler rule), run through
src/core/job_wrapper.run_job.

THE ABSOLUTE INVARIANT (Eloïse's promise): nothing is ever sent without
human approval. The dispatch SELECT is syntactically unable to pick a
TO_APPROVE row — there is no other send path in the codebase (the
WhatsApp mark-sent endpoint requires APPROVED too).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyTemplateStep
from shared.models.reminder import Reminder
from src.core.config import get_settings
from src.core.email import send_email, space_link
from src.core.email_templates import reminder_email
from src.core.enums import (
    ActorType,
    RecipientType,
    ReminderChannel,
    ReminderStatus,
    StepStatus,
)
from src.core.i18n import resolve_notification_lang_agent, resolve_notification_lang_client

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]


def _recipient(db: Session, reminder: Reminder, agency: Agency) -> tuple[str, str] | None:
    """(email, resolved language) of the reminder's recipient. EXPAT → their
    stored preference (client rule); EXTERNAL contact → the agency default
    (no stored language on a no-login contact)."""
    if reminder.recipient_type == RecipientType.EXPAT.value:
        row = db.execute(
            select(ExpatUser.email, ExpatUser.preferred_lang)
            .join(ClientCase, ClientCase.principal_expat_user_id == ExpatUser.id)
            .where(ClientCase.id == reminder.case_id)
        ).first()
        if row is None or row[0] is None:
            return None
        return str(row[0]), resolve_notification_lang_client(row[1])
    contact = db.get(ExternalContact, reminder.recipient_external_id)
    if contact is None or contact.email is None:
        return None
    return contact.email, resolve_notification_lang_agent(agency.default_language)


def dispatch_due_reminders(db: Session, *, log: LogFn, dry_run: bool = False) -> dict[str, Any]:
    """Send due APPROVED reminders (mail + in_app; whatsapp is manual).

    FOR UPDATE SKIP LOCKED: two overlapping ticks — or a manual trigger
    during a tick — never process the same row twice.
    """
    now = datetime.now(UTC)
    # Join ClientCase + deleted_at IS NULL: a soft-deleted case never
    # dispatches — its APPROVED reminders are skipped at SELECT time, so
    # the scheduler neither mails nor counts them (the most dangerous
    # leak: a deleted case must not keep sending).
    base = (
        select(Reminder, Agency)
        .join(ClientCase, ClientCase.id == Reminder.case_id)
        .join(Agency, Agency.id == ClientCase.agency_id)
        .where(
            Reminder.status == ReminderStatus.APPROVED.value,
            Reminder.scheduled_at <= now,
            Reminder.channel.in_([ReminderChannel.MAIL.value, ReminderChannel.IN_APP.value]),
            ClientCase.deleted_at.is_(None),
        )
    )
    if dry_run:
        due = len(db.execute(base).all())
        log(f"dry-run: {due} reminder(s) due, nothing sent")
        return {"due": due, "sent": 0, "dry_run": True}

    rows = db.execute(base.with_for_update(skip_locked=True, of=Reminder)).all()
    settings = get_settings()
    sent = 0
    for reminder, agency in rows:
        if reminder.channel == ReminderChannel.MAIL.value:
            recipient = _recipient(db, reminder, agency)
            if recipient is None:
                # Creation-time validation prevents this; defensive skip.
                log(f"reminder {reminder.id}: no recipient email, left approved")
                continue
            to, lang = recipient
            # The BRANDED client-space link — expat recipients only (an
            # external contact has no client space to open).
            link = (
                space_link(settings.frontend_url, "/space", agency.slug)
                if reminder.recipient_type == RecipientType.EXPAT.value
                else None
            )
            content = reminder_email(agency.name, reminder.message_body, link, lang)
            send_email(to, content.subject, content.text, content.html)
        # IN_APP: the SENT reminder itself IS the notification read by
        # the expat space (no notifications table).
        reminder.status = ReminderStatus.SENT.value
        db.add(
            ActivityLog(
                case_id=reminder.case_id,
                actor_type=ActorType.SYSTEM.value,
                actor_id=None,
                action_type="reminder.sent",
                details={"reminder_id": str(reminder.id), "channel": reminder.channel},
            )
        )
        sent += 1
        log(f"sent reminder {reminder.id} via {reminder.channel}")
    db.commit()
    return {"due": len(rows), "sent": sent}


def create_auto_reminders(db: Session, *, log: LogFn, dry_run: bool = False) -> dict[str, Any]:
    """J+20/J+30 follow-ups on stalled steps — created TO_APPROVE, never
    more: the system proposes, a human approves. Idempotence is PHYSICAL
    (unique on (step_progress_id, auto_threshold_days)); the NOT EXISTS
    here keeps repeat ticks quiet, the constraint is the belt.
    Per-agency toggle: agency.settings["auto_reminders_enabled"].
    Actor: SYSTEM."""
    settings = get_settings()
    now = datetime.now(UTC)
    created = 0
    would_create = 0
    for threshold in settings.auto_reminder_thresholds_days:
        cutoff = now - timedelta(days=threshold)
        already = exists(
            select(Reminder.id).where(
                Reminder.step_progress_id == CaseStepProgress.id,
                Reminder.auto_threshold_days == threshold,
            )
        )
        stmt = (
            select(CaseStepProgress, JourneyTemplateStep, ClientCase, Agency)
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .join(Agency, Agency.id == ClientCase.agency_id)
            .join(
                JourneyTemplateStep,
                JourneyTemplateStep.id == CaseStepProgress.template_step_id,
            )
            .where(
                CaseStepProgress.status.in_([StepStatus.TODO.value, StepStatus.IN_PROGRESS.value]),
                # updated_at as the "last movement" proxy.
                CaseStepProgress.updated_at <= cutoff,
                # No auto follow-up on a soft-deleted case.
                ClientCase.deleted_at.is_(None),
                ~already,
            )
        )
        for progress, step, case, agency in db.execute(stmt).all():
            if not (agency.settings or {}).get("auto_reminders_enabled", True):
                continue
            if dry_run:
                would_create += 1
                continue
            db.add(
                Reminder(
                    case_id=case.id,
                    step_progress_id=progress.id,
                    channel=ReminderChannel.MAIL.value,
                    scheduled_at=now,
                    status=ReminderStatus.TO_APPROVE.value,
                    recipient_type=RecipientType.EXPAT.value,
                    message_body=(
                        f"Automatic follow-up: step '{step.name}' has not "
                        f"progressed for {threshold} days."
                    ),
                    auto_threshold_days=threshold,
                )
            )
            db.add(
                ActivityLog(
                    case_id=case.id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    action_type="reminder.auto_created",
                    details={
                        "step_progress_id": str(progress.id),
                        "threshold": threshold,
                    },
                )
            )
            created += 1
            log(f"auto follow-up J+{threshold} for step {progress.id}")
    db.commit()
    stats: dict[str, Any] = {"created": created}
    if dry_run:
        stats |= {"would_create": would_create, "dry_run": True}
    return stats
