import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StepCaseRequirement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A CASE-LEVEL requirement DECLARED on a template step (sections
    chantier, vague C — option B + feuille de C).

    Twin of `step_requirement` but for a `client_case` column (country /
    address) instead of a `case_person` field. The DECISIVE difference:
    NO `person_id`, NO `scope`, and NO concrete materialized table. A
    field requirement's value is never stored on the concrete row — it is
    derived live from the backing store. A case field has no person and a
    single case-wide value, so there is nothing to materialize and no
    composition to freeze: a declaration + a live evaluation of
    `client_case` is the whole mechanism. The person-keyed invariant of
    `case_step_requirement` stays intact (no nullable person_id, no target
    discriminator).

    `case_field` ∈ COLLECTABLE_CASE_FIELDS (validated in the manager)."""

    __tablename__ = "step_case_requirement"
    __table_args__ = (UniqueConstraint("step_id", "case_field", name="uq_step_case_requirement"),)

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_field: Mapped[str] = mapped_column(String(30), nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)
