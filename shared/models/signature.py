"""E-signature domain (méga-lot 2026-07-28) — PROVIDER-AGNOSTIC.

`signature_request` = ONE document sent for signature on one case step
(1 request = 1 credit, whatever the signer count). `signature_signer` =
one targeted person on it, mapped to THEIR concrete requirement row so
completion flows through the EXISTING machinery (step_all_met, auto
reminders, member filtering) exactly like a deposited piece.

The domain stores NOTHING provider-specific beyond OPAQUE refs
(`provider_ref` per request, per signer) — every DocuSeal detail lives
behind the SignatureProvider port (src/signatures/provider.py)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SignatureRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signature_request"

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_case.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_step_progress_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The SOURCE definition (traceability; survives a definition delete —
    # same SET NULL pattern as case_step_requirement).
    step_requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("step_requirement.id", ondelete="SET NULL"), index=True
    )
    # Snapshot of the document label (the definition may change/vanish).
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # SignatureProviderKind
    level: Mapped[str] = mapped_column(String(3), nullable=False)  # SignatureLevel
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default=text("'draft'"), nullable=False
    )
    # Opaque provider handle (DocuSeal: the submission id). NEVER parsed.
    provider_ref: Mapped[str | None] = mapped_column(String(255), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignatureSigner(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signature_signer"
    __table_args__ = (
        # One seat per person on a request (idempotent materialization).
        UniqueConstraint("signature_request_id", "case_person_id", name="uq_signature_signer"),
        # Contreseing agence (lot 30/07) : un siège est EXACTEMENT l'un des
        # deux — une personne du dossier OU un agent (le contreseing).
        CheckConstraint(
            "num_nonnulls(case_person_id, agent_id) = 1",
            name="signer_person_xor_agent",
        ),
    )

    signature_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signature_request.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("case_person.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # Le siège de CONTRESEING : l'agent résolu à l'envoi (assigné au
    # dossier, sinon premier porteur d'agency.manage).
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # The person's concrete requirement row — flipped to provided when THEY
    # sign, so completeness rides the existing rails (never a copy).
    case_step_requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("case_step_requirement.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default=text("'pending'"), nullable=False
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Opaque per-signer provider handle (DocuSeal: submitter id) and the
    # per-signer embed slug served to the client space. Opaque both.
    provider_ref: Mapped[str | None] = mapped_column(String(255), index=True)
    provider_slug: Mapped[str | None] = mapped_column(String(255))
    # Room for provider-agnostic extras without schema churn.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
