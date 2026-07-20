import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class PlatformTaskAttachment(UUIDPrimaryKeyMixin, Base):
    """A file attached to a superadmin platform task. DEDICATED table
    (not Prism's polymorphic `attachments`): one parent, a real FK, a
    real DB cascade. The blob lives in the documents bucket under
    platform-tasks/{task_id}/{attachment_id} — uuid-only path, the
    display name never leaks into the storage key. The DB CASCADE only
    cleans rows: managers delete the blobs BEFORE deleting the task."""

    __tablename__ = "platform_task_attachment"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_task.id", ondelete="CASCADE"), index=True, nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    uploaded_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
