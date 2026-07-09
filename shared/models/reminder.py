import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.core.enums import ReminderStatus


class Reminder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Reminder with MANDATORY manual approval:
    TO_APPROVE → APPROVED (by an agent) → SENT, or CANCELLED.
    Nothing is ever dispatched before APPROVED.

    `message_body` is the server-interpolated text, frozen at creation.
    `recipient_external_id` is NO ACTION for the same CHECK/CASCADE
    interplay as case_step_progress."""

    __tablename__ = "reminder"
    __table_args__ = (
        CheckConstraint(
            "(recipient_type = 'expat' AND recipient_external_id IS NULL)"
            " OR (recipient_type = 'external' AND recipient_external_id IS NOT NULL)"
            # 'agent' = the case owner (escalation target), derived from
            # client_case.owner_agent_id → no recipient FK.
            " OR (recipient_type = 'agent' AND recipient_external_id IS NULL)",
            name="recipient_type_matches_fk",
        ),
        # The step-12 dispatcher polls approved reminders due for sending.
        Index("ix_reminder_status_scheduled_at", "status", "scheduled_at"),
        # PHYSICAL idempotence of the J+20/J+30 auto follow-ups: one
        # threshold can never be created twice for the same step. NULLs
        # (manual reminders) stay unconstrained (PG default NULLS DISTINCT).
        UniqueConstraint("step_progress_id", "auto_threshold_days"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    step_progress_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="SET NULL")
    )
    message_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message_template.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=ReminderStatus.TO_APPROVE, nullable=False
    )
    recipient_type: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_external_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_contact.id")
    )
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # Set ONLY by the auto-reminder job (20, 30, …) — NULL for manual
    # reminders. Carries the unique above.
    auto_threshold_days: Mapped[int | None] = mapped_column()
