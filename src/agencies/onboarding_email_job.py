"""The onboarding mail at J+10 min (spec Eric 13/08, P1) — SYNC pipeline,
run through job_wrapper.run_job like every other job.

WHY A SWEEP AND NOT A DEFERRED JOB: the scheduler is an in-process
APScheduler with the DEFAULT (memory) jobstore and CronTrigger only
(`src/core/scheduler.py`) — a `DateTrigger` job posted at agency creation
would not survive a restart, and Fly restarts on every deploy. A short cron
that sweeps the agencies past their delay survives by construction: the
state lives in the DATABASE (`agency.onboarding_email_sent_at`), never in
the scheduler's memory. Same shape as every lifecycle job here, no queue to
install for one mail per signup.

THE THREE GUARDS, all in config (never hardcoded):
- `onboarding_email_enabled` (None = production only): a local seed of 20
  agencies sends ZERO mail without anyone remembering a flag.
- `onboarding_email_excluded_patterns`: internal addresses, matched on the
  creator's email.
- `agency.is_internal`: the platform's own agencies.

THE FLAG IS POSED AFTER THE SEND, never before: a send that raises leaves
it NULL and the next tick retries. And the sweep only looks INSIDE its
catch-up window (24 h), so the park that predates the feature is out of
scope by construction — no backfill, and no "welcome" mail months late.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from src.core.config import Settings, get_settings
from src.core.email import send_email
from src.core.email_templates import agency_onboarding_email
from src.core.job_wrapper import LogFn

logger = logging.getLogger(__name__)


def first_internal_member(db: Session, agency_id: uuid.UUID) -> Agent | None:
    """The agency's first admin: its EARLIEST internal member — the account
    the creation gesture made (wizard or self-signup), never an agent invited
    afterwards, never a client, never a provider (`is_external` excluded).
    Shared with the nurture pipeline so both mails write to the same person."""
    return db.execute(
        select(Agent)
        .where(Agent.agency_id == agency_id, Agent.is_external.is_(False))
        .order_by(Agent.created_at)
        .limit(1)
    ).scalar_one_or_none()


def mail_enabled(settings: Settings) -> bool:
    """`bool | None` like `mock_email`: None = derive from the environment
    (production only), True/False force it."""
    if settings.onboarding_email_enabled is not None:
        return settings.onboarding_email_enabled
    return settings.environment.strip().lower() == "production"


def is_excluded_address(email: str, settings: Settings) -> bool:
    """Substring match on the lowercased address — the patterns are config
    (ONBOARDING_EMAIL_EXCLUDED_PATTERNS, comma-separated), so adding an
    internal domain never needs a deploy."""
    lowered = email.strip().lower()
    return any(
        pattern.strip().lower() in lowered
        for pattern in settings.onboarding_email_excluded_patterns
        if pattern.strip()
    )


def _has_example_case(db: Session, agency_id: uuid.UUID) -> bool:
    """Does the agency REALLY have its example dossier, live, right now?

    The mail says « un dossier d'exemple est déjà prêt dans votre espace ».
    It is true on the normal path (the seed runs at creation) but not
    always: an agency created without a sector gets no example, the seed
    can fail, and the agency may have deleted the case since. The sentence
    is dropped rather than promising a dossier that is not there."""
    return (
        db.execute(
            select(ClientCase.id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.is_demo.is_(True),
                ClientCase.deleted_at.is_(None),
            )
            .limit(1)
        ).first()
        is not None
    )


def send_onboarding_emails(db: Session, *, log: LogFn, dry_run: bool = False) -> dict[str, Any]:
    settings = get_settings()
    stats: dict[str, Any] = {"due": 0, "sent": 0, "excluded": 0, "no_recipient": 0, "failed": 0}
    if not mail_enabled(settings):
        log("onboarding mail disabled here (ONBOARDING_EMAIL_ENABLED / non-production)")
        stats["disabled"] = True
        return stats

    now = datetime.now(UTC)
    due_before = now - timedelta(minutes=settings.onboarding_email_delay_minutes)
    window_start = now - timedelta(hours=settings.onboarding_email_catchup_hours)
    agencies = (
        db.execute(
            select(Agency)
            .where(
                Agency.onboarding_email_sent_at.is_(None),
                Agency.is_internal.is_(False),
                Agency.created_at <= due_before,
                Agency.created_at >= window_start,
            )
            .order_by(Agency.created_at)
        )
        .scalars()
        .all()
    )
    for agency in agencies:
        stats["due"] += 1
        admin = first_internal_member(db, agency.id)
        if admin is None:
            # No recipient: leave the flag NULL, the window may still bring
            # one (and closes on its own).
            log(f"{agency.slug}: no internal member to write to, left open")
            stats["no_recipient"] += 1
            continue
        if is_excluded_address(admin.email, settings):
            # NOT stamped: the flag means « mail sent », and stamping here
            # would record a send that never happened. The window closes it.
            log(f"{agency.slug}: {admin.email} matches an excluded pattern, skipped")
            stats["excluded"] += 1
            continue

        has_example = _has_example_case(db, agency.id)
        if dry_run:
            log(f"{agency.slug}: would send to {admin.email} (lang={agency.default_language})")
            stats["sent"] += 1
            continue

        content = agency_onboarding_email(
            admin.first_name,
            app_url=settings.frontend_url,
            video_url=settings.onboarding_video_url,
            lang=agency.default_language,
            has_example_case=has_example,
        )
        try:
            send_email(admin.email, content.subject, content.text, content.html)
        except Exception:
            # A failed send leaves the flag NULL → retried at the next tick,
            # inside the catch-up window. One bad address never stops the sweep.
            logger.exception("onboarding mail failed for agency %s", agency.slug)
            log(f"{agency.slug}: send FAILED, will be retried")
            stats["failed"] += 1
            continue
        agency.onboarding_email_sent_at = datetime.now(UTC)
        # Commit per agency: a crash mid-run never re-sends the ones already out.
        db.commit()
        stats["sent"] += 1
        log(
            f"{agency.slug}: onboarding mail sent to {admin.email} "
            f"(lang={agency.default_language}, example={has_example})"
        )

    if dry_run:
        stats["dry_run"] = True
    log(
        f"onboarding sweep: {stats['due']} due, {stats['sent']} sent, "
        f"{stats['excluded']} excluded, {stats['failed']} failed"
    )
    return stats
