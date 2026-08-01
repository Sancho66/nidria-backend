"""Schémas du domaine fiches client (F1 lecture + F2 gestes)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProfileCaseSummaryResponse(BaseModel):
    """Un dossier de la fiche, en résumé."""

    id: uuid.UUID
    status: str
    journey_name: str | None
    reference: str | None
    created_at: datetime


class ProfileCompletenessResponse(BaseModel):
    """La complétude transversale (F2.4) : quels champs de portée personne
    sont déjà valorisés sur la fiche — le futur allègement de collecte."""

    filled: list[str]
    missing: list[str]


class ClientProfileListItemResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    cases_count: int
    active_cases_count: int
    # Statut client DÉRIVÉ (Phase 0 D9 : jamais deux vérités) — 'prospect'
    # si aucun dossier au-delà de prospect, sinon le plus avancé.
    derived_status: str
    created_at: datetime


class ClientProfileListResponse(BaseModel):
    items: list[ClientProfileListItemResponse]
    total: int
    page: int
    page_size: int


class ClientProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    expat_user_id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    preferred_lang: str
    activated_at: datetime | None
    # Le miroir civil (les 10 colonnes) + le sac custom (valeurs visibles).
    passport_number: str | None
    date_of_birth: Any
    nationality: str | None
    place_of_birth: str | None
    sex: str | None
    marital_status: str | None
    phone: str | None
    birth_name: str | None
    profession: str | None
    employer: str | None
    preferred_channels: list[str]
    custom_fields: dict[str, Any]
    source: str | None
    tags: list[str]
    cases: list[ProfileCaseSummaryResponse]
    derived_status: str
    completeness: ProfileCompletenessResponse
    created_at: datetime
    updated_at: datetime


class ProfileMergeRequest(BaseModel):
    """FUSIONNER (F1.6) : la fiche SOURCE se vide dans la fiche cible —
    re-liaison des case_person, valeurs de la CIBLE prioritaires (la source
    comble les trous), source supprimée. Les comptes de login restent
    distincts (nommé au rapport)."""

    model_config = ConfigDict(extra="forbid")

    source_profile_id: uuid.UUID


class FieldGestureRequest(BaseModel):
    """Les gestes-péages du croisement (F2.3) : une référence de champ de
    portée personne (colonne civile ou clé custom scope='person')."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=100)


class NewCaseForProfileRequest(BaseModel):
    """« Nouvelle démarche pour ce client » (F2.5) — la création DEPUIS la
    fiche : l'identité vient de la fiche, le pré-remplissage person suit la
    même mécanique que toute création (F2.1)."""

    model_config = ConfigDict(extra="forbid")

    journey_template_id: uuid.UUID | None = None
    origin_country: str | None = Field(default=None, min_length=2, max_length=2)
    dest_country: str | None = Field(default=None, min_length=2, max_length=2)
    reference: str | None = Field(default=None, max_length=100)
    source: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list)
