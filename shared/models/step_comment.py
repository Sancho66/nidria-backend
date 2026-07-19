import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
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
    # Piece jointe (2026-07-19) : la reference vers UN document du dossier
    # (regles GAP-B — une verite, deux affichages : le fil ET le panneau).
    # SET NULL : le document supprime rend la reference muette ; le
    # soft-delete du message ne tue jamais le document.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL")
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
