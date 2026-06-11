import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CaseNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Internal agency note on a case. `is_confidential` notes are
    visible only to agents holding the dedicated permission — access
    control on a real column, not a flag buried in a log."""

    __tablename__ = "case_note"

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_confidential: Mapped[bool] = mapped_column(default=False, nullable=False)
