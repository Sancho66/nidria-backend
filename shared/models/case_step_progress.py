import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.core.enums import StepStatus


class CaseStepProgress(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Instantiation of a template step on a case.

    Polymorphic responsible: depending on `responsible_type`, read
    `responsible_agent_id`, `responsible_external_id`, or (EXPAT) the
    case's principal. NULL type = not assigned yet. The CHECK keeps
    type and FK coherent at all times.

    The two responsible FKs are NO ACTION on purpose: SET NULL would
    violate the CHECK, and RESTRICT (checked immediately) would break
    the case CASCADE where this row and the external_contact fall in
    the same DELETE. NO ACTION is checked at end of statement: case
    deletion purges everything together, but deleting a contact still
    responsible for a step alone is refused."""

    __tablename__ = "case_step_progress"
    __table_args__ = (
        UniqueConstraint("case_id", "template_step_id"),
        CheckConstraint(
            "(responsible_type IS NULL"
            " AND responsible_agent_id IS NULL AND responsible_external_id IS NULL)"
            " OR (responsible_type = 'agent'"
            " AND responsible_agent_id IS NOT NULL AND responsible_external_id IS NULL)"
            " OR (responsible_type = 'expat'"
            " AND responsible_agent_id IS NULL AND responsible_external_id IS NULL)"
            " OR (responsible_type = 'external'"
            " AND responsible_external_id IS NOT NULL AND responsible_agent_id IS NULL)",
            name="responsible_type_matches_fk",
        ),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default=StepStatus.TODO, nullable=False)
    responsible_type: Mapped[str | None] = mapped_column(String(20))
    responsible_agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent.id"))
    responsible_external_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_contact.id")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # Optional FIRM deadline set by the agency. When present it takes
    # priority over the estimated_days-derived target for the counter.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
