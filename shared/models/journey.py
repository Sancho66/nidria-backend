import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JourneyTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Reusable journey MODEL configured by the agency.
    Its instantiation on a case is `case_step_progress`."""

    __tablename__ = "journey_template"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class JourneyTemplateStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "journey_template_step"
    __table_args__ = (UniqueConstraint("template_id", "position"),)

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False)
    estimated_days: Mapped[int | None] = mapped_column()
    default_responsible_type: Mapped[str | None] = mapped_column(String(20))
    # Optional NAMED default responsible (wave C): a precise INTERNAL agent
    # (durable — externals exist only at the case level, never on the
    # generic template; the Manager enforces is_external=False). Copied to
    # the progress row at journey assignment.
    default_responsible_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # How the step closes (NEW WAVE). Default reproduces the current
    # flow (agency closes the step) — additive, breaks nothing. `auto`
    # is a capability flag here; the active auto-complete trigger lands
    # in a later wave.
    completion_mode: Mapped[str] = mapped_column(
        String(20),
        default="agency_validation",
        server_default=text("'agency_validation'"),
        nullable=False,
    )


class StepPrerequisite(Base):
    """Self-referencing M2M between steps of the SAME template
    (locked-steps feature). Same-template + no-cycle validation is
    applicative (step 8); the DB only rules out self-reference."""

    __tablename__ = "step_prerequisite"
    __table_args__ = (
        CheckConstraint("step_id != prerequisite_step_id", name="no_self_prerequisite"),
    )

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), primary_key=True
    )
    prerequisite_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), primary_key=True
    )


class JourneyTemplateField(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A field a template COLLECTS at case creation (NEW WAVE) — the
    explicit list driving the dynamic creation form. Twin of
    `step_requirement` but attached to the TEMPLATE (not a step) and
    SEPARATE in purpose: collected once at creation, vs requirements that
    ask the client in-flight. The same field may appear in both, freely.

    `kind` ∈ base_field | custom_field (no `document` — documents are
    requirements, not creation fields). `reference`: base → a collectable
    case_person field; custom → a custom_field_definition key (resolved /
    flagged is_archived at read time, never copied). `required_at_creation`
    drives form validation in the case-creation wave."""

    __tablename__ = "journey_template_field"
    __table_args__ = (
        UniqueConstraint("template_id", "kind", "reference", name="uq_journey_template_field"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)
    required_at_creation: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
