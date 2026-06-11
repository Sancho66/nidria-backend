import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class ActivityLog(UUIDPrimaryKeyMixin, Base):
    """Immutable audit trail (who did what, when) — `created_at` only,
    no TimestampMixin. `actor_id` is a bare UUID, polymorphic via
    `actor_type` (NULL for SYSTEM). `details` carries old/new values
    of the mutation — manual case notes live in `case_note`, NOT here."""

    __tablename__ = "activity_log"
    __table_args__ = (
        # The case timeline reads (case_id, created_at DESC).
        Index("ix_activity_log_case_id_created_at", "case_id", "created_at"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), nullable=False
    )
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
