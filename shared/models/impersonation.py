import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class ImpersonationLog(UUIDPrimaryKeyMixin, Base):
    """Immutable audit trail of impersonation token issuance — one row
    per emission, `created_at` only. Deliberately NOT in activity_log
    (case-scoped); `target_id` is FK-less polymorphic like
    activity_log.actor_id, resolved via `target_type`."""

    __tablename__ = "impersonation_log"

    impersonator_agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(10), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
