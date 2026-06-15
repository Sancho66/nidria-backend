import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CaseExternalAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Links an EXTERNAL agent (provider) to a case (wave B). An external
    accesses a case ⟺ a row exists here — this is the single fact the
    per-case scoping (`get_case_for_external`) filters on. Case-level
    (the whole journey is visible once assigned); nominal per-step
    assignment is a later wave.

    `agent_id` has no is_external CHECK at the DB layer — the Manager
    validates the target is an external of the agency before inserting."""

    __tablename__ = "case_external_assignment"
    __table_args__ = (UniqueConstraint("case_id", "agent_id", name="uq_case_external_assignment"),)

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), index=True, nullable=False
    )
    assigned_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
