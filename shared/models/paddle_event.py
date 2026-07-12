import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PaddleWebhookEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Every VERIFIED Paddle webhook, exactly once — the idempotence gate
    (event_id UNIQUE: a re-delivery is a no-op 200) AND the audit trail.
    `agency_id` is a plain UUID (no FK): an event for an unknown agency is
    STORED (audited, alerted) but never creates anything; `occurred_at` is
    Paddle's clock, used by the handlers to converge on out-of-order
    deliveries (a stale status event never overwrites a newer one)."""

    __tablename__ = "paddle_webhook_event"

    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agency_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
