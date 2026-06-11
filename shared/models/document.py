import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Uploaded piece (Supabase Storage). `uploaded_by_*` is polymorphic
    (AGENT/EXPAT) with no FK, like activity_log actors.
    `validation_status` NULL = not reviewed yet (distinct from a
    reviewed-INCOMPLETE)."""

    __tablename__ = "document"

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    step_progress_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    uploaded_by_type: Mapped[str] = mapped_column(String(20), nullable=False)
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    validation_status: Mapped[str | None] = mapped_column(String(20))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
