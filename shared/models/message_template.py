import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MessageTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Agency-scoped reminder message template. `body` carries
    variables ({client_name}, {step_name}, {days_left}) interpolated
    SERVER-side when a reminder is created."""

    __tablename__ = "message_template"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
