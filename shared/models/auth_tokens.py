import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RefreshToken(Base):
    """One row per issued refresh token — the JWT `jti` claim.

    Rotation: every /refresh revokes the consumed jti and issues a new
    one. A revoked/unknown jti presented with a valid signature means
    the chain of trust is broken (reuse/theft) → ALL active tokens of
    the actor get revoked. Actor is polymorphic with no FK (same
    pattern as activity_log); `created_at` only — the single mutation
    is `revoked_at`."""

    __tablename__ = "refresh_token"
    __table_args__ = (
        # The reuse-detection revoke-all scans by actor.
        Index("ix_refresh_token_actor", "actor_type", "actor_id"),
    )

    jti: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PasswordResetToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Short-lived (~1h), single-use reset token — same pattern as the
    invitations. A successful reset consumes the token AND revokes all
    active refresh tokens of the actor."""

    __tablename__ = "password_reset_token"

    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
