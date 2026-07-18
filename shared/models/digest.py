import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DigestCursor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The progress-digest cursor, ONE per agency: everything after
    `last_sent_at` is 'new since the last digest'. A TABLE, not an
    agency.settings key — the front's settings PATCH replaces the whole
    JSONB dict and would silently wipe a cursor key (a real race, the
    decisive argument). Advanced on every in-scope run, events included
    or not: an event never appears twice, a quiet period stays quiet."""

    __tablename__ = "digest_cursor"
    __table_args__ = (UniqueConstraint("agency_id", name="uq_digest_cursor_agency"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
