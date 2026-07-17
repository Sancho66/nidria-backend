import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReferralCredit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One granted referral credit: -20% recurring on the REFERRER's
    subscription for 12 months, granted when the REFERRED agency converts
    (apply_conversion, the single gesture — once per life by construction,
    belt: referred_agency_id is UNIQUE, a godchild credits exactly once).

    This table is THE truth; the Paddle discount posed on the referrer's
    subscription is the EXECUTION (sum of active credits, capped at 60,
    maximum_recurring_intervals up to the FIRST credit boundary — Paddle
    stops by itself there, the lazy recompute re-poses the next tier).
    A churning godchild does NOT revoke the credit (decided): nothing here
    looks at the referred agency's later state."""

    __tablename__ = "referral_credit"
    __table_args__ = (UniqueConstraint("referred_agency_id", name="uq_referral_credit_referred"),)

    referrer_agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    referred_agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # granted_at + 12 months (decided: the simple rule — a dormant credit
    # sleeps at the referrer's expense if they are slow to convert).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rate: Mapped[int] = mapped_column(default=20, nullable=False)
