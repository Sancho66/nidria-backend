"""Schémas fiches SOCIÉTÉ (V2b, solde CRM — F5 back)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.client_profiles.client_profiles_schema import ProfileFieldSectionResponse

CompanyRole = Literal["manager", "partner", "contact", "beneficiary", "other"]


class CompanyProfileCreateRequest(BaseModel):
    """Création d'une fiche société. La dédup (agence, dénomination) est
    une SUGGESTION : 409 souple avec la référence de l'homonyme —
    `allow_duplicate=true` passe outre (deux sociétés homonymes existent
    dans la vraie vie)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    custom_fields: dict[str, Any] | None = None
    source: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list)
    allow_duplicate: bool = False


class CompanyProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    # Merge partiel (clé à null = retirée) — le sac société est LIBRE au
    # MVP (pas de référentiel société : les presets company mappés par la
    # taxonomie, le reste en misc).
    custom_fields: dict[str, Any] | None = None
    source: str | None = Field(default=None, max_length=100)
    tags: list[str] | None = None


class CompanyRoleCreateRequest(BaseModel):
    """Lier une PERSONNE (fiche client) à la société avec un rôle
    canonique — même vocabulaire que case_person.relationship_kind."""

    model_config = ConfigDict(extra="forbid")

    client_profile_id: uuid.UUID
    role: CompanyRole
    role_label: str | None = Field(default=None, max_length=50)


class CompanyRoleResponse(BaseModel):
    id: uuid.UUID
    client_profile_id: uuid.UUID
    role: str
    role_label: str | None
    first_name: str
    last_name: str
    email: str


class CompanyCaseSummaryResponse(BaseModel):
    id: uuid.UUID
    status: str
    reference: str | None
    created_at: datetime


class CompanyProfileResponse(BaseModel):
    id: uuid.UUID
    name: str
    custom_fields: dict[str, Any]
    source: str | None
    tags: list[str]
    sections: list[ProfileFieldSectionResponse]
    roles: list[CompanyRoleResponse]
    cases: list[CompanyCaseSummaryResponse]
    created_at: datetime
    updated_at: datetime


class CompanyProfileListItemResponse(BaseModel):
    id: uuid.UUID
    name: str
    tags: list[str]
    roles_count: int
    cases_count: int
    created_at: datetime


class CompanyProfileListResponse(BaseModel):
    items: list[CompanyProfileListItemResponse]
    total: int
    page: int
    page_size: int
