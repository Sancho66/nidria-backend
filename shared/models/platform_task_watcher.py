import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class PlatformTaskWatcher(UUIDPrimaryKeyMixin, Base):
    """An operator following a platform task: joins the creator on every
    status-change email (same triggers, same dedup, never the actor).
    Nidria-pure adaptation — Prism has NO watcher/follower concept."""

    __tablename__ = "platform_task_watcher"
    __table_args__ = (UniqueConstraint("task_id", "agent_id"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_task.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
