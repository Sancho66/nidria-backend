import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, text
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
    # Origin / destination addresses, flat columns. `origin_country` and
    # `dest_country` are the `country` of each address — KEPT as-is so
    # the country filters / sorts / saved views never change; street /
    # city / postal_code are the new fields added around them.
    origin_country: Mapped[str | None] = mapped_column(String(2))
    origin_street: Mapped[str | None] = mapped_column(String(255))
    origin_city: Mapped[str | None] = mapped_column(String(100))
    origin_postal_code: Mapped[str | None] = mapped_column(String(20))
    dest_country: Mapped[str | None] = mapped_column(String(2))
    dest_street: Mapped[str | None] = mapped_column(String(255))
    dest_city: Mapped[str | None] = mapped_column(String(100))
    dest_postal_code: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(
        String(30), default=CaseStatus.PROSPECT, index=True, nullable=False
    )
    source: Mapped[str | None] = mapped_column(String(100))
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    # Demo flag (usage trackers bloc 1, sample-case bloc 2): a seeded
    # example dossier. Excluded from EVERY usage signal (events,
    # milestones, backfill, counters) so the nurture never mistakes the
    # demo for real adoption.
    is_demo: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    # Soft delete: NULL = live. Every read path filters `deleted_at IS
    # NULL` (listing, detail, expat space, reminders, the scheduler,
    # dashboard) — a deleted case must surface NOWHERE. Bulk-delete
    # stamps it; re-deleting is a no-op.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    principal: Mapped["ExpatUser"] = relationship("ExpatUser")
