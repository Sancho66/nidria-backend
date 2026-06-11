import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
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
    # Step 15 (Eric): free-label list of the pieces the agency expects
    # at this step. INFORMATIVE at MVP — the lock stays prerequisites
    # only; piece↔requirement matching is V1.5. server_default so the
    # additive migration backfills existing rows.
    required_documents: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
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
