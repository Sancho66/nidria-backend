import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JourneyStepCost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A PLANNED cost line on a journey TEMPLATE step (the agency's expected
    débours: timbre fiscal 120, droit d'enregistrement 180). Sibling of
    `journey_step_attachment` / `journey_step_participant` — a template-level
    child, CASCADE-deleted with its step.

    Same shape as `case_step_cost` (amount DECIMAL(18,4) + label), but this is a
    STARTING POINT, never a truth: at case instantiation each planned line is
    COPIED into a real `case_step_cost` row (planned_amount frozen + a trace
    back to this id). Editing or deleting a planned cost NEVER touches an
    already-instantiated case (no propagation) — the copy is by value.

    Lives ONLY on an agency-owned template: a library sample (agency_id NULL)
    is unreachable for writes (get_template_in_agency), so a shared model never
    carries an agency's costs, and cloning a sample creates none."""

    __tablename__ = "journey_step_cost"

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), index=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
