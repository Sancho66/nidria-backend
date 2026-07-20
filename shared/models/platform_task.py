import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# The 3 lanes, fixed in code (GO 2026-07-20): Prism's per-project status
# catalog is machinery without an object for an internal backlog. The
# column stays a String so a future extension is data-shaped, not a
# migration of every row.
PLATFORM_TASK_STATUSES: tuple[str, ...] = ("todo", "in_progress", "done")
PLATFORM_TASK_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")
PLATFORM_TASK_TYPES: tuple[str, ...] = ("task", "call", "meeting", "follow_up")


class PlatformTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Superadmin internal ops backlog (the Prism tasks port). PLATFORM-
    scoped like job_config: no agency tenant — the nullable agency_id is
    the SUBJECT of the work ("relancer X", "vérifier le KYB de Y"), never
    a scope, exactly like Prism's peripheral company_id."""

    __tablename__ = "platform_task"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="todo", server_default="todo", index=True
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium", server_default="medium"
    )
    task_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="task", server_default="task"
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # The Prism appointment block (call/meeting): a precise UTC instant
    # paired with the IANA zone it was picked in (required together).
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_timezone: Mapped[str | None] = mapped_column(String(50))
    duration_minutes: Mapped[int | None] = mapped_column()
    location: Mapped[str | None] = mapped_column(String(500))
    agency_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agency.id", ondelete="SET NULL"), index=True
    )
    assigned_to_agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    completed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional operator note carried by the done email (copy-paste for the
    # client). Provided content lives forever: reopen never clears it.
    completion_message: Mapped[str | None] = mapped_column(Text)
