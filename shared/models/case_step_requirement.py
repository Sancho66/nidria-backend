import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CaseStepRequirement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A CONCRETE requirement, materialized when a step becomes active
    on a case (NEW WAVE). Reads the case composition AT THAT INSTANT:
    one row per (requirement, targeted person). FROZEN — later changes
    to the case composition never add/remove concrete requirements on an
    already-activated step.

    `kind`/`reference` are SNAPSHOTTED from the definition (stable even
    if the definition changes or is deleted). `step_requirement_id` is
    SET NULL on definition delete (traceability without breaking).

    `status`/`provided_at`/`document_id` are AUTHORITATIVE for
    kind=document only. For base_field/custom_field the provided state
    is DERIVED at read time from case_person (the value is never copied
    here — single source of truth)."""

    __tablename__ = "case_step_requirement"
    __table_args__ = (
        # Idempotent materialization, robust even if step_requirement_id
        # becomes NULL after a definition delete.
        UniqueConstraint(
            "case_step_progress_id",
            "person_id",
            "kind",
            "reference",
            name="uq_case_step_requirement",
        ),
    )

    case_step_progress_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="CASCADE"), index=True, nullable=False
    )
    step_requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("step_requirement.id", ondelete="SET NULL"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_person.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    # Authoritative for kind=document; derived at read time for fields.
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    provided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL")
    )
