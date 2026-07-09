"""Daily trial-nurture pipeline (nurture bloc 3) — SYNC, run through
src/core/job_wrapper.run_job like the reminder jobs.

For every agency IN TRIAL it computes days elapsed since activation
(the `agence_activee` milestone, i.e. the moment the wizard posed the
trial) plus the LIVE usage state (S0/S1/S2, demo excluded), and sends
the calendar mail: J+7, J+21, J+28. Rules:

- STRICT dedup on (agency, day_key): a slot fires once, whatever the
  state was. S0→S1 between mails ⇒ the agency gets s0_j7 THEN s1_j21
  (Eric's texts are written for it: a different angle every time).
- Catch-up: a mail due at J+7 still leaves at J+8 (missed tick), but
  never twice, and never two mails the same day — when several slots
  are due, only the MOST RECENT one is sent, the older ones are marked
  skipped. A slot more than CATCHUP_WINDOW_DAYS past due is skipped
  too (a J+7 mail landing at J+40 would be worse than silence).
- J+28 carries Eric's booking link: while NURTURE_BOOKING_URL is empty
  the slot is held as pending_config (a mail never leaves with a hole)
  and retried by later runs.
- Never: agency without a trial, platform/test agencies
  (NURTURE_EXCLUDED_SLUGS), anything beyond the J+28 calendar.
- dry_run lists what WOULD leave, writes nothing, sends nothing (the
  required first prod run: POST /jobs/trial_nurture/trigger dry_run).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.nurture import NurtureSend
from shared.models.usage import AgencyUsageMilestone
from src.core.config import get_settings
from src.core.email import send_email
from src.core.enums import NurtureSendStatus
from src.nurture.nurture_texts import needs_booking_url, render_mail
from src.usage.usage_manager import classify_usage_state

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]

SCHEDULE: tuple[tuple[str, int], ...] = (("j7", 7), ("j21", 21), ("j28", 28))
# How long past its threshold a slot may still fire (missed ticks,
# pending_config unblocking). Beyond that it is burned as skipped.
CATCHUP_WINDOW_DAYS = 7


def _usage_state(db: Session, agency_id: Any) -> str:
    """SYNC read of the agency's milestone keys → the SHARED classifier (one
    source of truth with UsageManager.compute_usage_state and the dashboard)."""
    keys = set(
        db.execute(
            select(AgencyUsageMilestone.key).where(AgencyUsageMilestone.agency_id == agency_id)
        ).scalars()
    )
    return classify_usage_state(keys)


def _activation_anchor(db: Session, agency: Agency) -> datetime:
    """J+N is relative to agency.activated — the `agence_activee`
    milestone posed by the wizard with the trial. Fallback: created_at
    (same instant for wizard agencies). STABLE under a manual trial
    extension, so an extension never re-arms the calendar."""
    first_at = db.execute(
        select(AgencyUsageMilestone.first_at).where(
            AgencyUsageMilestone.agency_id == agency.id,
            AgencyUsageMilestone.key == "agence_activee",
        )
    ).scalar_one_or_none()
    return first_at if first_at is not None else agency.created_at


def _first_admin(db: Session, agency_id: Any) -> Agent | None:
    """The recipient: the agency's first admin — its earliest internal
    member (the wizard creates the admin first; backfilled agencies
    follow the same shape)."""
    return db.execute(
        select(Agent)
        .where(Agent.agency_id == agency_id, Agent.is_external.is_(False))
        .order_by(Agent.created_at)
        .limit(1)
    ).scalar_one_or_none()


def _burn(db: Session, row: NurtureSend | None, agency_id: Any, day_key: str, state: str) -> None:
    """Mark a slot skipped (insert, or downgrade a pending_config)."""
    if row is None:
        db.add(
            NurtureSend(
                agency_id=agency_id,
                day_key=day_key,
                mail_key=f"{state.lower()}_{day_key}",
                status=NurtureSendStatus.SKIPPED.value,
            )
        )
    else:
        row.status = NurtureSendStatus.SKIPPED.value


def send_trial_nurture(db: Session, *, log: LogFn, dry_run: bool = False) -> dict[str, Any]:
    settings = get_settings()
    booking_url = settings.nurture_booking_url.strip()
    excluded = set(settings.nurture_excluded_slugs)
    now = datetime.now(UTC)
    stats = {"in_scope": 0, "sent": 0, "skipped": 0, "pending_config": 0}

    # trial_ends_at NOT NULL = the trial calendar; converted agencies
    # (converted_at posed by Eric) leave the nurture scope immediately -
    # trial_ends_at itself is never touched at conversion (pricing
    # 2026-07-07), so the extra guard is required.
    agencies = (
        db.execute(
            select(Agency)
            .where(Agency.trial_ends_at.is_not(None), Agency.converted_at.is_(None))
            .order_by(Agency.slug)
        )
        .scalars()
        .all()
    )
    for agency in agencies:
        if agency.slug in excluded:
            continue
        days = (now - _activation_anchor(db, agency)).days
        if days < SCHEDULE[0][1] or days > SCHEDULE[-1][1] + CATCHUP_WINDOW_DAYS:
            continue  # before the calendar, or past it entirely
        stats["in_scope"] += 1

        rows = {
            row.day_key: row
            for row in db.execute(
                select(NurtureSend).where(NurtureSend.agency_id == agency.id)
            ).scalars()
        }
        # Open slots: due, and not already terminally decided.
        open_slots = [
            (day_key, threshold, rows.get(day_key))
            for day_key, threshold in SCHEDULE
            if days >= threshold
            and (
                rows.get(day_key) is None
                or rows[day_key].status == NurtureSendStatus.PENDING_CONFIG.value
            )
        ]
        if not open_slots:
            continue

        state = _usage_state(db, agency.id)
        # Never two mails the same day: only the MOST RECENT due slot
        # may send, the older ones are burned.
        day_key, threshold, row = open_slots[-1]
        for older_key, _t, older_row in open_slots[:-1]:
            if dry_run:
                log(f"{agency.slug}: would skip {older_key} (overtaken by {day_key})")
            else:
                _burn(db, older_row, agency.id, older_key, state)
            stats["skipped"] += 1

        mail_key = f"{state.lower()}_{day_key}"
        admin = _first_admin(db, agency.id)
        if days > threshold + CATCHUP_WINDOW_DAYS:
            if dry_run:
                log(f"{agency.slug}: would skip {mail_key} (stale, J+{days})")
            else:
                _burn(db, row, agency.id, day_key, state)
            stats["skipped"] += 1
        elif admin is None:
            # No recipient: leave the slot open (an admin may appear
            # within the catch-up window), just report.
            log(f"{agency.slug}: no internal member to write to, slot {day_key} left open")
        elif needs_booking_url(state, day_key) and not booking_url:
            if dry_run:
                log(f"{agency.slug}: would hold {mail_key} (NURTURE_BOOKING_URL unset)")
            elif row is None:
                db.add(
                    NurtureSend(
                        agency_id=agency.id,
                        day_key=day_key,
                        mail_key=mail_key,
                        status=NurtureSendStatus.PENDING_CONFIG.value,
                        recipient=admin.email,
                    )
                )
                log(f"{agency.slug}: {mail_key} held, booking URL unset (pending_config)")
            stats["pending_config"] += 1
        elif dry_run:
            log(f"{agency.slug}: would send {mail_key} to {admin.email} (J+{days})")
            stats["sent"] += 1
        else:
            mail = render_mail(state, day_key, first_name=admin.first_name, booking_url=booking_url)
            send_email(
                admin.email,
                mail.subject,
                mail.body,
                sender=settings.nurture_from,
                reply_to=settings.nurture_from,
            )
            if row is None:
                row = NurtureSend(agency_id=agency.id, day_key=day_key, mail_key=mail_key)
                db.add(row)
            row.mail_key = mail_key  # state re-evaluated at the actual send
            row.status = NurtureSendStatus.SENT.value
            row.sent_at = now
            row.recipient = admin.email
            row.lang = "fr"
            stats["sent"] += 1
            log(f"{agency.slug}: sent {mail_key} to {admin.email} (J+{days})")
        if not dry_run:
            # Commit per agency: a crash mid-run never resends the ones
            # already decided.
            db.commit()

    if dry_run:
        stats["dry_run"] = True
        log(
            f"dry-run: {stats['sent']} would send, {stats['skipped']} would skip, "
            f"{stats['pending_config']} held"
        )
    return stats
