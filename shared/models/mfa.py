import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MfaTotp(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One TOTP enrollment per actor (RFC 6238) — polymorphic actor with
    no FK, same pattern as refresh_token. `enabled_at` NULL = PENDING:
    the secret was generated at setup but stays inert until a first
    valid code proves possession (enable). The base32 `secret` is NEVER
    exposed by any response after setup; at-rest encryption is a
    documented follow-up (needs a key-management story)."""

    __tablename__ = "mfa_totp"
    __table_args__ = (UniqueConstraint("actor_type", "actor_id", name="uq_mfa_totp_actor"),)

    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)  # agent | expat
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    secret: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MfaBackupCode(UUIDPrimaryKeyMixin, Base):
    """One-time recovery codes: bcrypt-hashed like passwords, shown in
    clear exactly once (at enable). `used_at` marks consumption — the
    row survives for audit, a used code never authenticates again."""

    __tablename__ = "mfa_backup_code"

    mfa_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mfa_totp.id", ondelete="CASCADE"), index=True, nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MfaChallenge(Base):
    """A pending login step-2, keyed by the mfa_token's jti: the
    server-side attempts counter a stateless JWT cannot carry. Dies at
    the attempts cap or natural expiry; deleted on success. Expired
    rows are swept opportunistically at the next challenge creation."""

    __tablename__ = "mfa_challenge"
    __table_args__ = (Index("ix_mfa_challenge_actor", "actor_type", "actor_id"),)

    jti: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
