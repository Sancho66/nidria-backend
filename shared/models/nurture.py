"""Trial-nurture send ledger (nurture bloc 3).

One row per (agency, calendar slot) — THE dedup: `day_key` ("j7" /
"j21" / "j28") is unique per agency whatever the usage state was, so a
slot fires at most once. `mail_key` records WHICH text went out
("s1_j21": the state evaluated at send time — trace, not dedup).

`status`: SENT (mail out, `sent_at` stamped), SKIPPED (slot burned
without a send — overtaken by a more recent due slot, or too stale),
PENDING_CONFIG (J+28 held back while the booking URL is unset; retried
by later runs, the one non-terminal status)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NurtureSend(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "nurture_send"
    __table_args__ = (UniqueConstraint("agency_id", "day_key", name="uq_nurture_send_slot"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    day_key: Mapped[str] = mapped_column(String(10), nullable=False)
    mail_key: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The first admin's address at decision time (audit: who received what).
    recipient: Mapped[str | None] = mapped_column(String(255))
    # FR only for now; recorded so the translated era can tell who got what.
    lang: Mapped[str] = mapped_column(String(5), nullable=False, server_default=text("'fr'"))
