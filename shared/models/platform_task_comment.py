import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class PlatformTaskComment(UUIDPrimaryKeyMixin, Base):
    """A message in the free-form discussion of a superadmin platform task
    ("Commentaires" tab of the task editor).

    DEDICATED table, not StepComment: that one is bound to a
    case_step_progress and its attachments reference a dossier `document`
    (case-scoped) — nothing of that transfers to an agency-less platform
    task. Single author kind here (the surface is superadmin-only), so a
    plain `author_agent_id` FK, not the AGENT|EXPAT polymorphism.

    No edit (product rule); HARD delete by the author, matching the
    platform-task ethos (an internal ops backlog, not a legal record —
    PlatformTask itself hard-deletes). `author_agent_id` is SET NULL so a
    departed operator never orphans the row; deleting the task CASCADEs the
    whole thread. Attachments live on platform_task_attachment via its
    nullable `comment_id` — the existing upload path, reused."""

    __tablename__ = "platform_task_comment"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_task.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
