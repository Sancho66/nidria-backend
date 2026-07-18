"""The anti-burst window (demi-lot 2026-07-18): per (case, recipient
email, category), 30 minutes. The FIRST notification of a category goes
out and opens the window; the follow-ups inside it are suppressed (the
first email already says "check your space", which shows everything
live). Only an EFFECTIVE send posts the window — a failed mail never
suppresses the next one."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.notification_window import NotificationWindow

WINDOW = timedelta(minutes=30)


async def window_allows(
    db: AsyncSession,
    case_id: uuid.UUID,
    recipient_email: str,
    category: str,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    row = (
        await db.execute(
            select(NotificationWindow).where(
                NotificationWindow.case_id == case_id,
                NotificationWindow.recipient_email == recipient_email,
                NotificationWindow.category == category,
            )
        )
    ).scalar_one_or_none()
    return row is None or (now - row.last_sent_at) >= WINDOW


async def record_send(
    db: AsyncSession,
    case_id: uuid.UUID,
    recipient_email: str,
    category: str,
    now: datetime | None = None,
) -> None:
    """Upsert the window row. The CALLER commits (or it rides the caller's
    transaction) — same pattern as every manager write."""
    now = now or datetime.now(UTC)
    row = (
        await db.execute(
            select(NotificationWindow).where(
                NotificationWindow.case_id == case_id,
                NotificationWindow.recipient_email == recipient_email,
                NotificationWindow.category == category,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            NotificationWindow(
                case_id=case_id,
                recipient_email=recipient_email,
                category=category,
                last_sent_at=now,
            )
        )
    else:
        row.last_sent_at = now
