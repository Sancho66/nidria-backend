import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StepRequirement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A requirement DECLARED on a template step (NEW WAVE): the step
    expects an info or a document, per person.

    `kind` ∈ base_field|custom_field|document.
    `reference`: base_field → a collectable case_person field name
    (whitelist in src/progress/requirements_eval); custom_field → a
    custom_field_definition key of the agency; document → a free label.
    `scope` ∈ principal|each_person. All requirements are mandatory
    (no optional flag — product decision)."""

    __tablename__ = "step_requirement"
    __table_args__ = (
        UniqueConstraint("step_id", "kind", "reference", "scope", name="uq_step_requirement"),
    )

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)
