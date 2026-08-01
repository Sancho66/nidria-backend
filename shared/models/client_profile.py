"""La FICHE CLIENT d'agence (chantier fiches, F1 — Phase 0 fait foi).

Le pendant AGENCE du compte client global : `expat_user` reste l'identité
de login (cross-agences, jamais de métier — doctrine RGPD du modèle) ; la
fiche porte les données que L'AGENCE connaît de ce client, transversales à
ses dossiers. Patron de promotion `external_contact` (ancre agence).

Stockage MIROIR de `case_person` (verdict Phase 0 A1) : les 10 colonnes
civiles nullables + le sac `custom_fields` JSONB — le leaf du résolveur
est identique des deux côtés (FieldPlane.PROFILE), l'import/export/prefill
parlent le même langage. La fiche COPIE (gestes explicites F2), elle ne
déporte JAMAIS les valeurs des dossiers (invariant n°3 de la Phase 0).

Blocs CRM propres (Phase 0 D8) : `source` (l'acquisition DU CLIENT) et
`tags` (un tag client n'est pas un tag dossier — jamais fusionnés)."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ClientProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "client_profile"
    __table_args__ = (
        # UNE fiche par client par agence — la clé du chantier.
        UniqueConstraint("agency_id", "expat_user_id", name="uq_client_profile_agency_expat"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # NULLABLE (complément 2, F4) : une fiche peut naître SANS compte
    # (prospect à froid, création directe) — la liaison se fait au PREMIER
    # dossier (adoption par email dans link_and_prefill_person). L'UNIQUE
    # (agency, expat) ne contraint pas les NULL (sémantique Postgres).
    expat_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("expat_user.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    # Identité PROPRE de la fiche (posée à la création directe) : sert tant
    # que la fiche n'est pas liée ; après liaison, l'identité du COMPTE
    # prime à la lecture, ces colonnes restent la trace d'origine.
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255), index=True)

    # --- miroir civil de case_person (mêmes types, mêmes longueurs) -------
    passport_number: Mapped[str | None] = mapped_column(String(50))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    nationality: Mapped[str | None] = mapped_column(String(100))
    place_of_birth: Mapped[str | None] = mapped_column(String(200))
    sex: Mapped[str | None] = mapped_column(String(1))
    marital_status: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(50))
    birth_name: Mapped[str | None] = mapped_column(String(200))
    profession: Mapped[str | None] = mapped_column(String(200))
    employer: Mapped[str | None] = mapped_column(String(200))
    # Affichage seulement — jamais un routeur d'envoi (invariant n°6).
    preferred_channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- blocs CRM propres à la fiche -------------------------------------
    source: Mapped[str | None] = mapped_column(String(100))
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    expat_user = relationship("ExpatUser", lazy="select")
