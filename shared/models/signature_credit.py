"""Ledger de crédits signature par AGENCE (méga-lot 28/07, lot 2).

`signature_credit_entry` = LA vérité comptable (append-only) : purchase
(webhook Paddle) / reserve (envoi d'une demande) / consume (complétion) /
release (annulation, expiration). `signature_credit_balance` = la
matérialisation du solde, UNE ligne par agence — c'est elle qui porte le
verrou de ligne (SELECT FOR UPDATE) sérialisant les réservations
concurrentes, et le CHECK >= 0 qui rend le solde négatif IMPOSSIBLE au
niveau DB (l'invariant du lot). Un test épingle balance == dérivation
des écritures."""

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SignatureCreditBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signature_credit_balance"
    __table_args__ = (
        UniqueConstraint("agency_id", name="uq_signature_credit_balance_agency"),
        CheckConstraint("available >= 0", name="available_never_negative"),
        CheckConstraint("reserved >= 0", name="reserved_never_negative"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Disponible = achats - réservations vivantes - consommés (un consume ne
    # touche pas available : le crédit était déjà sorti à la réservation).
    available: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # Réservations vivantes (reserve - consume - release).
    reserved: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )


class SignatureCreditEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signature_credit_entry"
    __table_args__ = (
        # Ceinture d'idempotence Paddle EN PLUS de la ligne événement unique
        # de paddle_webhook_event (doctrine billing) : un même event ne peut
        # pas créditer deux fois, même si un handler était rejoué.
        UniqueConstraint("paddle_event_id", name="uq_signature_credit_entry_paddle_event"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # SignatureCreditKind
    # Toujours POSITIF ; le sens est porté par kind (lisible, jamais de
    # somme signée piégeuse dans un rapport).
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    signature_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signature_request.id", ondelete="SET NULL"), index=True
    )
    paddle_event_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
