"""Immutable trace of a HARD agency deletion (Groupe C).

A superadmin platform tool: one row per successful DELETE
/agencies/{id}. NO FK to `agency` (it is gone by the time this is
read) - the id/name/slug are FROZEN in place, exactly like the consent
trace survives the agency it binds. `performed_by_agent_id` is a bare
UUID too (no FK), the email is captured verbatim. Insert-only (no
TimestampMixin): the audit record of an irreversible act."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class AgencyDeletionLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agency_deletion_log"

    deleted_agency_id: Mapped[uuid.UUID] = mapped_column(nullable=False)  # bare UUID, no FK
    agency_name: Mapped[str] = mapped_column(String(200), nullable=False)
    agency_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    deleted_cases_count: Mapped[int] = mapped_column(Integer, nullable=False)
    performed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column()  # bare UUID, no FK
    performed_by_email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
