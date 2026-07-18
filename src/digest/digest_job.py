"""The progress digest (lot 2026-07-19, last piece of the notifications
work): a periodic readable summary of each ACTIVE case's advancement,
sent to the CLIENT side (principal + members with access, each in THEIR
language). It consumes the stored `progress_digest` preference
(weekly|daily|off, default weekly).

Positions held:
- STRICT WHITELIST of event types — nothing internal ever leaks
  (agent-nominative actions, notes, reminders: excluded by construction;
  the digest reads activity_log but only speaks step.completed,
  step.started, and document.validated landing on OK).
- The digest RE-LISTS everything in its window, including what already
  had a unitary mail (decided): its reader typically muted the flow —
  filtering would put holes exactly where they look.
- The cursor is a TABLE (digest_cursor, one row per agency): the front's
  settings PATCH replaces the whole JSONB and would wipe a key-based
  cursor. Advanced on every in-scope run — an event never appears twice,
  and a case with nothing new sends NOTHING (silence is the message).
- weekly fires on the Monday run of the daily job; daily fires each run.
- Out of scope: internal agencies, digest off, deleted/closed cases."""

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.digest import DigestCursor
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplateStep
from src.core.config import get_settings
from src.core.email import send_email, space_link
from src.core.email_templates import digest_email
from src.core.enums import CasePersonKind, CaseStatus
from src.core.i18n import resolve_notification_lang_client, resolve_step_name_for_notif
from src.core.notification_prefs import client_pref

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]

# The whitelist IS the privacy boundary: only these speak to the client.
DIGEST_EVENT_TYPES = ("step.completed", "step.started", "document.validated")


def run_notification_digest(
    db: Session, *, log: LogFn, dry_run: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    is_monday = now.weekday() == 0
    agencies = db.execute(select(Agency).where(Agency.is_internal.is_(False))).scalars().all()
    stats = {"agencies": 0, "mails": 0, "cases": 0, "dry_run": dry_run}
    for agency in agencies:
        mode = client_pref(agency, "progress_digest")
        if mode == "off" or (mode == "weekly" and not is_monday):
            continue
        stats["agencies"] += 1
        cursor = db.execute(
            select(DigestCursor).where(DigestCursor.agency_id == agency.id)
        ).scalar_one_or_none()
        since = (
            cursor.last_sent_at
            if cursor is not None
            else now - (timedelta(days=7) if mode == "weekly" else timedelta(days=1))
        )
        mails = _agency_digest(db, agency, mode, since, now, log, dry_run=dry_run)
        stats["mails"] += mails["mails"]
        stats["cases"] += mails["cases"]
        if not dry_run:
            if cursor is None:
                db.add(DigestCursor(agency_id=agency.id, last_sent_at=now))
            else:
                cursor.last_sent_at = now
            db.commit()
    log(
        f"digest: {stats['mails']} mail(s), {stats['cases']} case(s), "
        f"{stats['agencies']} agency(ies) in scope"
    )
    return stats


def _agency_digest(
    db: Session,
    agency: Agency,
    mode: str,
    since: datetime,
    now: datetime,
    log: LogFn,
    *,
    dry_run: bool,
) -> dict[str, int]:
    rows = db.execute(
        select(ActivityLog, ClientCase)
        .join(ClientCase, ClientCase.id == ActivityLog.case_id)
        .where(
            ClientCase.agency_id == agency.id,
            ClientCase.deleted_at.is_(None),
            ClientCase.status != CaseStatus.CLOSED.value,
            ActivityLog.action_type.in_(DIGEST_EVENT_TYPES),
            ActivityLog.created_at > since,
            ActivityLog.created_at <= now,
        )
        .order_by(ActivityLog.created_at)
    ).all()
    by_case: dict[Any, list[ActivityLog]] = defaultdict(list)
    cases: dict[Any, ClientCase] = {}
    for event, case in rows:
        by_case[case.id].append(event)
        cases[case.id] = case
    settings = get_settings()
    link = space_link(settings.frontend_url, "/space", agency.slug)
    sent = 0
    for case_id, events in by_case.items():
        case = cases[case_id]
        content = _case_content(db, events)
        if content is None:
            continue  # nothing whitelisted survived (e.g. validations not OK)
        completed_ids, started_ids, docs = content
        step_names = _step_names(db, completed_ids + started_ids)
        for email, lang in _client_recipients(db, case):
            completed = [
                resolve_step_name_for_notif(i18n, name, lang)
                for name, i18n in (step_names[pid] for pid in completed_ids if pid in step_names)
            ]
            started = [
                resolve_step_name_for_notif(i18n, name, lang)
                for name, i18n in (step_names[pid] for pid in started_ids if pid in step_names)
            ]
            if not completed and not started and not docs:
                continue  # never an empty mail
            mail = digest_email(agency.name, mode, completed, started, docs, link, lang)
            if not dry_run:
                try:
                    send_email(email, mail.subject, mail.text, mail.html)
                except Exception:  # noqa: BLE001 — best-effort boundary
                    logger.exception("digest mail failed (best-effort) to=%s", email)
                    continue
            sent += 1
    return {"mails": sent, "cases": len(by_case)}


def _case_content(
    db: Session, events: list[ActivityLog]
) -> tuple[list[str], list[str], int] | None:
    """(completed step_progress_ids, started ids, validated docs count) —
    ORDERED, deduplicated (a step completed twice in the window counts
    once, the last state wins the narrative)."""
    completed: list[str] = []
    started: list[str] = []
    docs = 0
    for event in events:
        details = event.details or {}
        pid = details.get("step_progress_id")
        if event.action_type == "step.completed" and pid and pid not in completed:
            completed.append(pid)
        elif event.action_type == "step.started" and pid and pid not in started:
            started.append(pid)
        elif event.action_type == "document.validated" and details.get("new") == "ok":
            docs += 1
    started = [pid for pid in started if pid not in completed]  # terminee > demarree
    if not completed and not started and not docs:
        return None
    return completed, started, docs


def _step_names(db: Session, progress_ids: list[str]) -> dict[str, tuple[str, Any]]:
    if not progress_ids:
        return {}
    rows = db.execute(
        select(CaseStepProgress.id, JourneyTemplateStep.name, JourneyTemplateStep.name_i18n)
        .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
        .where(CaseStepProgress.id.in_(progress_ids))
    ).all()
    return {str(row[0]): (row[1], row[2]) for row in rows}


def _client_recipients(db: Session, case: ClientCase) -> list[tuple[str, str]]:
    """(email, lang) for the principal + every member with an access —
    each in THEIR language, never the principal's."""
    recipients: list[tuple[str, str]] = []
    principal = db.execute(
        select(ExpatUser.email, ExpatUser.preferred_lang)
        .join(ClientCase, ClientCase.principal_expat_user_id == ExpatUser.id)
        .where(ClientCase.id == case.id)
    ).first()
    if principal is not None and principal[0]:
        recipients.append((str(principal[0]), resolve_notification_lang_client(principal[1])))
    members = db.execute(
        select(ExpatUser.email, ExpatUser.preferred_lang)
        .join(CasePerson, CasePerson.expat_user_id == ExpatUser.id)
        .where(
            CasePerson.case_id == case.id,
            CasePerson.kind != CasePersonKind.PRINCIPAL.value,
        )
    ).all()
    for email, lang in members:
        if email and all(email != existing for existing, _ in recipients):
            recipients.append((str(email), resolve_notification_lang_client(lang)))
    return recipients
