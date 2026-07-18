import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotificationWindow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Anti-burst tracker, ONE per (case, recipient, category) — the
    generalization of the old per-step comment tracker (demi-lot
    anti-burst, decision 2026-07-18): the window is per DOSSIER and per
    RECIPIENT (email), so two steps touched minutes apart cost ONE email,
    while two recipients never share a window. `last_sent_at` records the
    EFFECTIVE send, posted only after send_email succeeds — a failed mail
    never suppresses the next one (the reason this is a table, not a
    derivation from event timestamps).

    Categories today: "comments" (thread notifications, both directions)
    and "steps" (requirement-request mails; seeded at case creation and
    journey kickoff so the setup burst stays ONE email)."""

    __tablename__ = "notification_window"
    __table_args__ = (
        UniqueConstraint("case_id", "recipient_email", "category", name="uq_notification_window"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
