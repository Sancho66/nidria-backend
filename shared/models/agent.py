import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, PersonNameMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from shared.models.rbac import Role


class Agent(UUIDPrimaryKeyMixin, PersonNameMixin, TimestampMixin, Base):
    """Agency-side user. Roles live in `agent_role` (dynamic RBAC) —
    deliberately NO hardcoded `role` enum column."""

    __tablename__ = "agent"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    roles: Mapped[list["Role"]] = relationship("Role", secondary="agent_role")
