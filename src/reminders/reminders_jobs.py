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

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_participant import CaseStepParticipant
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.reminder import Reminder
from src.core.config import get_settings
from src.core.email import send_email, space_link
from src.core.email_templates import (
    auto_reminder_body,
    reminder_email,
    reminder_escalation_email,
)
from src.core.enums import (
    ActorType,
    RecipientType,
    ReminderChannel,
    ReminderStatus,
    StepStatus,
)
from src.core.i18n import resolve_notification_lang_agent, resolve_notification_lang_client
from src.core.notification_prefs import client_pref
from src.reminders.reminders_targeting import targeted_member

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


def _targeted_member_user(db: Session, reminder: Reminder) -> ExpatUser | None:
    """The member the reminder's step points at (see reminders_targeting) —
    None routes to the principal path."""
    if reminder.step_progress_id is None:
        return None
    requirements = (
        db.execute(
            select(CaseStepRequirement).where(
                CaseStepRequirement.case_step_progress_id == reminder.step_progress_id
            )
        )
        .scalars()
        .all()
    )
    persons = {
        p.id: p
        for p in db.execute(select(CasePerson).where(CasePerson.case_id == reminder.case_id))
        .scalars()
        .all()
    }
    person = targeted_member(list(requirements), persons)
    if person is None or person.expat_user_id is None:
        return None
    member = db.get(ExpatUser, person.expat_user_id)
    if member is None or not member.email:
        return None
    return member


def _recipient(
    db: Session, reminder: Reminder, agency: Agency
) -> tuple[str, str, str | None] | None:
    """(email, language, escalated_from). `escalated_from` is None for a
    direct delivery; it carries the ORIGINAL contact's name when an EXTERNAL
    recipient is unreachable (no email) and the reminder is re-routed to the
    case owner — so a reminder NEVER dies in silence. Returns None only when
    even the owner is missing (defensive)."""
    if reminder.recipient_type == RecipientType.EXPAT.value:
        # Routing (2026-07-18): the step's pending requirements may all
        # point at ONE member with an access — the reminder goes to HER,
        # in HER language. Otherwise the principal, as before. Never both.
        member = _targeted_member_user(db, reminder)
        if member is not None:
            return member.email, resolve_notification_lang_client(member.preferred_lang), None
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
        email_suppressed = False
        if (
            reminder.channel == ReminderChannel.MAIL.value
            and reminder.recipient_type == RecipientType.EXPAT.value
            and client_pref(agency, "reminders") == "off"
        ):
            # La pref de l'agence coupe l'EMAIL client, jamais le rappel :
            # le cycle de vie continue (SENT + trace), l'agence garde tout.
            email_suppressed = True
        elif reminder.channel == ReminderChannel.MAIL.value:
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
                # Record that it now targets the owner (agent). The external
                # FK is KEPT as provenance: the auto-pass idempotence matches
                # on it — a rewritten line still blocks its threshold.
                reminder.recipient_type = RecipientType.AGENT.value
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
        if email_suppressed:
            details["email_suppressed"] = True  # la pref agence a coupe l'email
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


def resolve_auto_reminder_thresholds(
    journey: JourneyTemplate, agency: Agency, default: list[int]
) -> list[int]:
    """NID-18 fallback chain for the two auto-reminder thresholds (days):
    the CASE's journey pair (both set) → the agency's settings pair (both
    set) → the system default ([20, 30]). NULL/absent = inherit the next
    level. The journey pair is validated (1 ≤ d1 < d2 ≤ 365) at write time,
    so a resolved pair is always ordered."""
    d1, d2 = journey.auto_reminder_days_1, journey.auto_reminder_days_2
    if d1 is not None and d2 is not None:
        return [d1, d2]
    settings = agency.settings or {}
    a1, a2 = settings.get("auto_reminder_days_1"), settings.get("auto_reminder_days_2")
    if isinstance(a1, int) and isinstance(a2, int):
        return [a1, a2]
    return list(default)


def create_auto_reminders(db: Session, *, log: LogFn, dry_run: bool = False) -> dict[str, Any]:
    """Auto follow-ups on stalled steps — created TO_APPROVE, never more:
    the system proposes, a human approves. TWO passes on the SAME clock
    (step_progress.updated_at as the last-movement proxy): the client one
    (principal/member), and since P2 the PROVIDER one — every external
    participant of a stalled step gets its own proposed follow-up, in the
    AGENCY's language (the manual-reminder rule), the dispatch escalation
    (no email → case owner) applying unchanged.

    NID-18: the two thresholds are PER-JOURNEY — resolve_auto_reminder_
    thresholds(case's journey → agency settings → system default). A stalled
    step gets, per threshold it has crossed, one follow-up. Idempotence is
    PHYSICAL (unique on (step, threshold, recipient_type, provider)); we
    also pre-load the existing (step, threshold[, provider]) keys so repeat
    ticks stay quiet. Per-agency toggle: agency.settings
    ["auto_reminders_enabled"]. Actor: SYSTEM."""
    settings = get_settings()
    default_thresholds = settings.auto_reminder_thresholds_days
    now = datetime.now(UTC)
    created = 0
    would_create = 0
    # The smallest threshold the system can ever resolve is the validation
    # floor (1 day) — a step touched more recently can't have crossed any
    # threshold, so it never becomes a candidate.
    floor_cutoff = now - timedelta(days=1)

    # Idempotence, pre-loaded once (mirrors the former NOT EXISTS): client =
    # (step, threshold) for ANY reminder; provider = (step, threshold, ext).
    existing = db.execute(
        select(
            Reminder.step_progress_id,
            Reminder.auto_threshold_days,
            Reminder.recipient_external_id,
        ).where(Reminder.auto_threshold_days.is_not(None))
    ).all()
    # Client keys are external_id-NULL rows only; provider keys carry the
    # contact. They are DISJOINT (the unique is on (step, threshold,
    # recipient_type, provider)) — a provider follow-up must never suppress
    # the client one at the same (step, threshold), nor the reverse.
    seen_any: set[tuple[Any, int]] = {(r[0], r[1]) for r in existing if r[2] is None}
    seen_provider: set[tuple[Any, int, Any]] = {
        (r[0], r[1], r[2]) for r in existing if r[2] is not None
    }

    def _age_reached(updated_at: datetime, threshold: int) -> bool:
        return updated_at <= now - timedelta(days=threshold)

    # --- client pass (principal) ------------------------------------------------------
    stmt = (
        select(
            CaseStepProgress, JourneyTemplateStep, ClientCase, Agency, ExpatUser, JourneyTemplate
        )
        .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
        .join(Agency, Agency.id == ClientCase.agency_id)
        .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
        # The CASE's journey carries the per-journey thresholds (NID-18).
        .join(JourneyTemplate, JourneyTemplate.id == ClientCase.journey_template_id)
        # The recipient (case principal) — its preferred_lang drives the
        # SYSTEM-authored body's language.
        .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
        .where(
            CaseStepProgress.status.in_([StepStatus.TODO.value, StepStatus.IN_PROGRESS.value]),
            CaseStepProgress.updated_at <= floor_cutoff,
            ClientCase.deleted_at.is_(None),
            # Demo dossiers ([Exemple], is_demo) never enter the approval queue.
            ClientCase.is_demo.is_(False),
        )
    )
    for progress, step, case, agency, expat, journey in db.execute(stmt).all():
        if not (agency.settings or {}).get("auto_reminders_enabled", True):
            continue
        for threshold in resolve_auto_reminder_thresholds(journey, agency, default_thresholds):
            if not _age_reached(progress.updated_at, threshold):
                continue
            if (progress.id, threshold) in seen_any:
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
                    # Translated into the CLIENT's language.
                    message_body=auto_reminder_body(
                        step.name,
                        threshold,
                        resolve_notification_lang_client(expat.preferred_lang),
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
                    details={"step_progress_id": str(progress.id), "threshold": threshold},
                )
            )
            seen_any.add((progress.id, threshold))
            created += 1
            log(f"auto follow-up J+{threshold} for step {progress.id}")

    # --- provider pass (P2): same clock, same toggle, same per-journey thresholds,
    # per external participant. The join on ExternalContact.case_id == ClientCase.id
    # IS the "contact of the case" validation of the manual flow (a foreign-case
    # contact wired on a participant creates nothing). UNCHANGED by NID-18 except
    # the thresholds are now the case's journey's.
    provider_stmt = (
        select(
            CaseStepProgress,
            JourneyTemplateStep,
            ClientCase,
            Agency,
            ExternalContact,
            JourneyTemplate,
        )
        .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
        .join(Agency, Agency.id == ClientCase.agency_id)
        .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
        .join(JourneyTemplate, JourneyTemplate.id == ClientCase.journey_template_id)
        .join(CaseStepParticipant, CaseStepParticipant.case_step_progress_id == CaseStepProgress.id)
        .join(ExternalContact, ExternalContact.id == CaseStepParticipant.external_id)
        .where(
            CaseStepParticipant.type == "external",
            ExternalContact.case_id == ClientCase.id,
            CaseStepProgress.status.in_([StepStatus.TODO.value, StepStatus.IN_PROGRESS.value]),
            CaseStepProgress.updated_at <= floor_cutoff,
            ClientCase.deleted_at.is_(None),
            ClientCase.is_demo.is_(False),
        )
    )
    for progress, step, case, agency, contact, journey in db.execute(provider_stmt).all():
        if not (agency.settings or {}).get("auto_reminders_enabled", True):
            continue
        for threshold in resolve_auto_reminder_thresholds(journey, agency, default_thresholds):
            if not _age_reached(progress.updated_at, threshold):
                continue
            if (progress.id, threshold, contact.id) in seen_provider:
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
                    recipient_type=RecipientType.EXTERNAL.value,
                    recipient_external_id=contact.id,
                    # The manual-flow language rule: a provider reads the
                    # AGENCY's language, never the client's.
                    message_body=auto_reminder_body(
                        step.name,
                        threshold,
                        resolve_notification_lang_agent(agency.default_language),
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
                        "recipient_external_id": str(contact.id),
                    },
                )
            )
            seen_provider.add((progress.id, threshold, contact.id))
            created += 1
            log(f"auto follow-up J+{threshold} for provider {contact.id} on step {progress.id}")
    db.commit()
    stats: dict[str, Any] = {"created": created}
    if dry_run:
        stats |= {"would_create": would_create, "dry_run": True}
    return stats
