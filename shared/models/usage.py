import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UsageEvent(UUIDPrimaryKeyMixin, Base):
    """Usage tracker layer 1 (spec Eric 2026-07-03): one typed, dated
    event per significant action, AGENCY-scoped (activity_log stays the
    case-scoped, CASE_VIEW-gated journal — deliberately untouched).
    Insert-only, `created_at` only. `case_id` is a bare UUID (nullable:
    agency-level events carry none; no FK so the trail survives hard
    deletes); actor is the polymorphic no-FK pair. Demo cases
    (client_case.is_demo) never emit."""

    __tablename__ = "usage_event"
    __table_args__ = (
        Index("ix_usage_event_agency_type_created", "agency_id", "event_type", "created_at"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)  # agent|expat|system
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgencyUsageMilestone(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Usage tracker layer 2: the per-agency adoption aggregate the
    nurture mails and the superadmin dashboard read. GOLDEN RULE:
    `first_at` is set at the FIRST occurrence and NEVER rewritten;
    `count` increments. Fed in-transaction by the emitters, rebuildable
    by the replay script (state + events)."""

    __tablename__ = "agency_usage_milestone"
    __table_args__ = (UniqueConstraint("agency_id", "key", name="uq_agency_usage_milestone"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(default=0, nullable=False)
