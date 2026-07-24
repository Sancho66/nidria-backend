import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin

# The two notifiable event kinds. Attachments alone are deliberately NOT
# events (too low-signal — a file without a word says little).
PLATFORM_TASK_EVENT_TYPES: tuple[str, ...] = ("status_change", "comment")


class PlatformTaskEvent(UUIDPrimaryKeyMixin, Base):
    """A notifiable change on a platform task, queued for the watcher
    digest (a 20-minute sliding window per task). One row per status change
    or new comment; `notified_at` is the idempotence marker — a processed
    event never re-enters a digest, even if the job runs twice.

    A SNAPSHOT, not a live reference: `old_status`/`new_status` and the
    comment `excerpt`/author are stored AT EVENT TIME, so a comment deleted
    before the window closes still reads correctly in the digest. The actor
    (`actor_agent_id`) is who did it — excluded from their OWN digest, never
    notified of their own action."""

    __tablename__ = "platform_task_event"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_task.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # The actor's display name, snapshotted: the digest reads human even if
    # the agent later leaves (author_agent_id → NULL).
    actor_name: Mapped[str | None] = mapped_column(String(255))
    # status_change only.
    old_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str | None] = mapped_column(String(20))
    # comment only: a short excerpt of the body (the full comment lives in
    # platform_task_comment; the excerpt survives its deletion).
    excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # NULL = queued (un-notified). Set once the window has been processed
    # and the digests sent — THE idempotence marker (requirement 8).
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
