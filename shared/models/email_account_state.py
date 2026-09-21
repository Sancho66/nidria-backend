"""Shared provider-account telemetry, never scoped to an agency."""

from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin


class EmailAccountState(TimestampMixin, Base):
    __tablename__ = "email_account_state"

    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
