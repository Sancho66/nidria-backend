import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, text
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
        # "Action validée par" (refonte). LOOSER than the responsible CHECK
        # (decision D2): type 'agent' allows a NULL agent_id (= "the agency
        # in general", any member — the migrated state of the former
        # 'agency_validation'); a designated member sets it. type 'external'
        # REQUIRES the provider agent (an is_external Agent — a validator
        # logs in and clicks, so it is in validated_by_agent_id, never a
        # no-login external_contact). 'none' (auto) / 'expat' (the principal):
        # no FK.
        CheckConstraint(
            "(validated_by_type IN ('none', 'expat') AND validated_by_agent_id IS NULL)"
            " OR (validated_by_type = 'agent')"
            " OR (validated_by_type = 'external' AND validated_by_agent_id IS NOT NULL)",
            name="validated_by_type_matches_fk",
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
    # "Action validée par" — copied from the template's
    # default_validated_by_type at assignment (frozen per dossier, D1: a
    # later template edit never retro-changes a live case's closing rule).
    # `validated_by_agent_id` holds the INTERNAL member (type 'agent') OR the
    # is_external provider (type 'external'); NULL for 'agent' = any member.
    validated_by_type: Mapped[str] = mapped_column(
        String(20), default="agent", server_default=text("'agent'"), nullable=False
    )
    validated_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # Optional FIRM deadline set by the agency. When present it takes
    # priority over the estimated_days-derived target for the counter.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
