"""Per-agency monthly AI usage (translation feature).

One row per (agency, month "YYYY-MM"), points accumulated on SUCCESSFUL
AI calls only (1 point = a tenth of a cent of model cost, floor 1 per
call). The month key gives the free monthly reset: a new month simply
starts a new row."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgencyAiUsage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agency_ai_usage"
    __table_args__ = (UniqueConstraint("agency_id", "month", name="uq_agency_ai_usage_month"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # "YYYY-MM"
    points_used: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
