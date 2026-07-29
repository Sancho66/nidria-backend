import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint, text
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
    # E-signature (méga-lot 28/07): a DOCUMENT requirement may demand a
    # signature instead of a deposit. `signature_level` ∈ ses|aes|qes
    # (SignatureLevel) — the model accepts the three, the journeys API only
    # lets `ses` in until the others have an implementation.
    signature_required: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    signature_level: Mapped[str] = mapped_column(
        String(3), default="ses", server_default=text("'ses'"), nullable=False
    )
    # LOT 6 : le PDF que l'agence fait signer (uploadé sur l'exigence du
    # template, stocké chez nous). Chaque upload écrit un NOUVEAU chemin —
    # les snapshots des dossiers en vol gardent l'ancien fichier. On ne
    # signe JAMAIS un document vide : l'assignation ET l'envoi refusent une
    # exigence signable sans PDF (422 nommé).
    # Méga-lot modèles (29/07) : le PDF-direct est MORT (0 ligne en prod au
    # verdict) — une exigence signable référence un MODÈLE de la bibliothèque
    # (zones posées par l'agence dans le builder). RESTRICT en défense : la
    # suppression d'un modèle référencé est refusée applicativement (409).
    document_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_template.id", ondelete="RESTRICT"), nullable=True
    )
