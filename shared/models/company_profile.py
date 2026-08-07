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
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, text
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


class CompanyFieldDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """LA DÉFINITION D'UN CHAMP DE FICHE SOCIÉTÉ (lot du 07/08).

    Née `company_field_label` (demande design A du 03/08) : elle ne
    portait qu'un libellé d'agence et le kind de naissance d'une clé
    baptisée à la grille d'import. Elle porte désormais tout ce qu'une
    définition doit porter — type, section, position, archivage, libellé
    ×7 — et devient la source unique de ce que la fiche société montre.

    POURQUOI UNE TABLE DÉDIÉE, ET PAS `custom_field_definition` : la
    contrainte `(agency_id, key)` y est UNIQUE et 9 des 17 clés société
    (`company_name`, `legal_form`, `share_capital`…) y existent déjà en
    `scope='person'` — une même clé ne peut pas porter deux définitions.
    S'y ajouter aurait demandé un 3ᵉ scope, donc un audit des ~20
    appelants d'`active_definitions()` qui ne filtrent pas le scope. Deux
    tables, deux espaces de clés : la fiche société ne peut plus fuir
    dans le dossier ni dans l'espace expat.

    Le libellé d'agence reste un ÉCART VOLONTAIRE au preset : `label`
    vide-de-personnalisation n'existe pas — la ligne matérialisée naît
    avec le libellé du catalogue, que le PATCH remplace."""

    __tablename__ = "company_field_definition"
    __table_args__ = (UniqueConstraint("agency_id", "key", name="uq_company_field_definition"),)

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # Le libellé ×7 : celui du catalogue à la matérialisation, celui de
    # l'agence dès qu'elle renomme (même mécanique que la face personne).
    label_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # text | number | date | boolean | address | country | select…
    # (`kind` avant le lot — renommé pour parler la même langue que la
    # définition personne, que le même contrat de sortie sert).
    field_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="text", server_default="text"
    )
    # La section de la taxonomie fiche (identity/contact/situation/misc).
    # LUE par `resolve_company_sections` — c'est ce qui rend le
    # reclassement vrai au lieu d'un plan figé dans le code.
    profile_section: Mapped[str] = mapped_column(
        String(20), nullable=False, default="misc", server_default="misc"
    )
    position: Mapped[int] = mapped_column(default=0, nullable=False, server_default=text("0"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
