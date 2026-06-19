import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CaseStepParticipant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """ "Action à réaliser par" instantiated on a case step (responsible
    refonte: N participants with a role). Snapshot-copied from the template
    participants at assignment — frozen on the dossier, like the responsible.

    Polymorphic person CALQUÉ sur le responsable d'instance: agent /
    expat (the case principal, implicit) / external_contact. Same NO ACTION
    rationale on the two person FKs as case_step_progress (SET NULL would
    break the CHECK; the case CASCADE purges everything together).

    The `role` is a StepParticipantRole — never `validator` (validation stays
    on the untouched validated_by_* mechanism)."""

    __tablename__ = "case_step_participant"
    __table_args__ = (
        # `agent` with agent_id NULL = "the agency in general" (no named member),
        # symmetric to validated_by_type='agent' + agent_id NULL. A named member
        # sets agent_id; an external provider uses type='external' + external_id.
        CheckConstraint(
            "(type = 'agent' AND external_id IS NULL)"
            " OR (type = 'expat' AND agent_id IS NULL AND external_id IS NULL)"
            " OR (type = 'external' AND external_id IS NOT NULL AND agent_id IS NULL)",
            name="participant_instance_type_matches_fk",
        ),
    )

    case_step_progress_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # agent | expat | external
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent.id"))
    external_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("external_contact.id"))
    role: Mapped[str] = mapped_column(String(30), nullable=False)  # StepParticipantRole
