from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SignupVerification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Self-serve signup, stage 1+2. The 6-digit code is stored HASHED
    (sha256 — the real lock is the attempts counter, not hash strength),
    expires in 15 minutes, dies after 5 wrong attempts. One LIVE
    verification per email (a re-request kills the previous one). Stage 2:
    a long completion_token (30 min) issued when the code matches — the
    only key the completion endpoint accepts. Nothing else exists until
    completion: no ghost agency, ever."""

    __tablename__ = "signup_verification"

    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    lang: Mapped[str] = mapped_column(String(2), nullable=False, default="fr")
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Stage 2 (posed when the code matches; the code is then consumed).
    completion_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    completion_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
