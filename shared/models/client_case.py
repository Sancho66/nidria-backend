import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.core.enums import CaseStatus

if TYPE_CHECKING:
    from shared.models.expat_user import ExpatUser


class ClientCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The expatriation file — hub of the product.

    ondelete semantics: the case dies with its agency (CASCADE); an
    expat user with live cases cannot be deleted (RESTRICT); an
    assigned journey template cannot be deleted (RESTRICT); the owner
    agent can leave (SET NULL). Countries are ISO 3166-1 alpha-2,
    format validated at the Pydantic layer."""

    __tablename__ = "client_case"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    principal_expat_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("expat_user.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL"), index=True
    )
    journey_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journey_template.id", ondelete="RESTRICT")
    )
    origin_country: Mapped[str | None] = mapped_column(String(2))
    dest_country: Mapped[str | None] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(
        String(30), default=CaseStatus.PROSPECT, index=True, nullable=False
    )
    source: Mapped[str | None] = mapped_column(String(100))
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    # Soft delete: NULL = live. Every read path filters `deleted_at IS
    # NULL` (listing, detail, expat space, reminders, the scheduler,
    # dashboard) — a deleted case must surface NOWHERE. Bulk-delete
    # stamps it; re-deleting is a no-op.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    principal: Mapped["ExpatUser"] = relationship("ExpatUser")
