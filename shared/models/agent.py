import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, PersonNameMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from shared.models.rbac import Role


class Agent(UUIDPrimaryKeyMixin, PersonNameMixin, TimestampMixin, Base):
    """Agency-side user. ONE role per agent (Prism model) — a dynamic
    FK to the editable `role` table, deliberately NO hardcoded enum.
    RESTRICT: a role somebody wears cannot be deleted (the Manager
    rebinds clone wearers before deleting)."""

    __tablename__ = "agent"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("role.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped["Role"] = relationship("Role")
