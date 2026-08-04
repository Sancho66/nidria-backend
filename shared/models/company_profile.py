"""La FICHE SOCIÉTÉ d'agence (V2b, solde CRM — F5 back).

Le pendant société de `client_profile` : l'agence connaît des SOCIÉTÉS
(la structure qu'un client crée, détient ou représente), transversales
aux dossiers. Dénomination + sac de valeurs JSONB sur la taxonomie
COMPANY_PROFILE_SECTIONS (les 8 presets company mappés, les clés libres
en 'misc'). La dédup par (agence, dénomination) est une SUGGESTION
(409 souple, contournable) — deux sociétés homonymes existent dans la
vraie vie.

`company_profile_role` : la table de rôles personne↔société — le rôle
CANONIQUE (manager/partner/contact/beneficiary/other, même vocabulaire
que case_person.relationship_kind) + libellé libre."""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CompanyProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "company_profile"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str | None] = mapped_column(String(100))
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )


class CompanyFieldLabel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Demande design A (03/08) : le LABEL d'une clé de sack société —
    choisi à la création de champ depuis la grille d'import, porté au
    niveau AGENCE (une vérité par clé, jamais une copie par société — le
    sack garde des valeurs nues). Le kind de la naissance voyage avec :
    les imports suivants coercent la clé comme à sa création."""

    __tablename__ = "company_field_label"
    __table_args__ = (UniqueConstraint("agency_id", "key", name="uq_company_field_label"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # text | number | date | boolean — le kind choisi à la grille.
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default="text", server_default="text"
    )


class CompanyProfileRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "company_profile_role"
    __table_args__ = (
        UniqueConstraint(
            "company_profile_id",
            "client_profile_id",
            "role",
            name="uq_company_profile_role",
        ),
    )

    company_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company_profile.id", ondelete="CASCADE"), index=True, nullable=False
    )
    client_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_profile.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    role_label: Mapped[str | None] = mapped_column(String(50))
