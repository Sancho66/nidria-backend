import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.core.enums import ExternalContactType


class ExternalContact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """External professional on a case (notary, lawyer, bank…).
    NO login at MVP — reminder target only. V2 hook: an `external_user`
    auth table will relate 1 login ↔ N external_contact rows."""

    __tablename__ = "external_contact"

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    type: Mapped[str] = mapped_column(String(30), default=ExternalContactType.OTHER, nullable=False)
