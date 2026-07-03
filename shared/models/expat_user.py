from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, PersonNameMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ExpatUser(UUIDPrimaryKeyMixin, PersonNameMixin, TimestampMixin, Base):
    """Client-side user. Deliberately NO `agency_id`: an expat can have
    cases with several agencies — the link goes through
    `client_case.principal_expat_user_id`.

    `password_hash` is nullable: the row is created when a case invites
    the expat by email; the password is set at activation
    (`activated_at` flips from NULL)."""

    __tablename__ = "expat_user"

    preferred_lang: Mapped[str] = mapped_column(String(5), default="fr", nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Profile picture (bloc 1) — same serving rule as the agent's. Like
    # first/last_name, the avatar belongs to the GLOBAL expat identity:
    # visible to every agency holding a live case (deliberate, the client
    # manages their own identity across agencies).
    avatar_path: Mapped[str | None] = mapped_column(String(500))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
