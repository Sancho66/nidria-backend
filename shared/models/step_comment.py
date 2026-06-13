import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StepComment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A message in the per-step discussion thread (VAGUE 5). Attached to
    a case_step_progress (the dossier's real step, not the template), so
    the thread follows the client's actual progress.

    Dual-author: `author_type` AGENT|EXPAT + bare `author_id` UUID
    (polymorphic, no FK — same pattern as activity_log). Each author may
    edit/soft-delete ONLY their own message (enforced server-side from
    the JWT identity, never the payload).

    `edited_at`: set only when the body is edited (a dedicated column —
    NOT updated_at>created_at, which the soft-delete would falsify).
    `deleted_at`: SOFT delete — the row stays so the thread keeps its
    coherence; the body is hidden in the response ("message supprimé")."""

    __tablename__ = "step_comment"

    case_step_progress_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_type: Mapped[str] = mapped_column(String(20), nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StepCommentNotification(UUIDPrimaryKeyMixin, Base):
    """Anti-burst tracker for thread notifications (VAGUE 5). One row per
    (step thread, recipient side); `last_notified_at` records the EFFECTIVE
    send — posted only after send_email succeeds, never on a failed/skipped
    attempt. So a failed first mail does not suppress the next one (the
    deliberate reason this is a table, not a derivation from comment
    timestamps)."""

    __tablename__ = "step_comment_notification"
    __table_args__ = (
        UniqueConstraint(
            "case_step_progress_id", "recipient_type", name="uq_step_comment_notification"
        ),
    )

    case_step_progress_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recipient_type: Mapped[str] = mapped_column(String(20), nullable=False)
    last_notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
