"""The two scheduled pipelines — SYNC (scheduler rule), run through
src/core/job_wrapper.run_job.

THE ABSOLUTE INVARIANT (Eloïse's promise): nothing is ever sent without
human approval. The dispatch SELECT is syntactically unable to pick a
TO_APPROVE row — there is no other send path in the codebase (the
WhatsApp mark-sent endpoint requires APPROVED too).
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyTemplateStep
from shared.models.reminder import Reminder
from src.core.config import get_settings
from src.core.email import send_email, space_link
from src.core.email_templates import reminder_email, reminder_escalation_email
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


def _owner_delivery(db: Session, case_id: uuid.UUID, agency: Agency) -> tuple[str, str] | None:
    """(email, lang) of the case owner (agency side) — the escalation target.
    None only if the case has no owner (then the reminder cannot escalate)."""
    row = db.execute(
        select(Agent.email)
        .join(ClientCase, ClientCase.owner_agent_id == Agent.id)
        .where(ClientCase.id == case_id)
    ).first()
    if row is None or row[0] is None:
        return None
    return str(row[0]), resolve_notification_lang_agent(agency.default_language)


def _recipient(
    db: Session, reminder: Reminder, agency: Agency
) -> tuple[str, str, str | None] | None:
    """(email, language, escalated_from). `escalated_from` is None for a
    direct delivery; it carries the ORIGINAL contact's name when an EXTERNAL
    recipient is unreachable (no email) and the reminder is re-routed to the
    case owner — so a reminder NEVER dies in silence. Returns None only when
    even the owner is missing (defensive)."""
    if reminder.recipient_type == RecipientType.EXPAT.value:
        row = db.execute(
            select(ExpatUser.email, ExpatUser.preferred_lang)
            .join(ClientCase, ClientCase.principal_expat_user_id == ExpatUser.id)
            .where(ClientCase.id == reminder.case_id)
        ).first()
        if row is None or row[0] is None:
            return None
        return str(row[0]), resolve_notification_lang_client(row[1]), None
    if reminder.recipient_type == RecipientType.AGENT.value:  # already owner-directed
        owner = _owner_delivery(db, reminder.case_id, agency)
        return (owner[0], owner[1], None) if owner is not None else None
    # EXTERNAL: deliver if reachable, else ESCALATE to the case owner.
    contact = db.get(ExternalContact, reminder.recipient_external_id)
    if contact is not None and contact.email is not None:
        return contact.email, resolve_notification_lang_agent(agency.default_language), None
    owner = _owner_delivery(db, reminder.case_id, agency)
    if owner is None:
        return None
    return owner[0], owner[1], (contact.name if contact is not None else "ce prestataire")


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
        escalated_from: str | None = None
        if reminder.channel == ReminderChannel.MAIL.value:
            recipient = _recipient(db, reminder, agency)
            if recipient is None:
                # No reachable recipient AND no owner to escalate to — the
                # only case left approved (loud log, never a silent drop).
                log(f"reminder {reminder.id}: no reachable recipient nor owner, left approved")
                continue
            to, lang, escalated_from = recipient
            if escalated_from is not None:
                # Unreachable external → the reminder REMONTE to the case owner.
                content = reminder_escalation_email(
                    agency.name, escalated_from, reminder.message_body, lang
                )
                # Record that it now targets the owner (agent), no external FK.
                reminder.recipient_type = RecipientType.AGENT.value
                reminder.recipient_external_id = None
            else:
                # The BRANDED client-space link — expat recipients only.
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
        details: dict[str, Any] = {"reminder_id": str(reminder.id), "channel": reminder.channel}
        if escalated_from is not None:
            details["escalated_from"] = escalated_from
        db.add(
            ActivityLog(
                case_id=reminder.case_id,
                actor_type=ActorType.SYSTEM.value,
                actor_id=None,
                action_type="reminder.escalated" if escalated_from else "reminder.sent",
                details=details,
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
