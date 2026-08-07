"""Schémas fiches SOCIÉTÉ (V2b, solde CRM — F5 back)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Labels d'agence des clés LIBRES du sack (demande design A) : le
    # label choisi à la création depuis la grille d'import — les presets
    # gardent leurs labels de catalogue côté front.
    field_labels: dict[str, str] = Field(default_factory=dict)
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
    # Même dérivation que les personnes : max activité des dossiers liés,
    # plancher updated_at de la fiche société.
    last_activity_at: datetime | None = None
    created_at: datetime


class CompanyProfileListResponse(BaseModel):
    items: list[CompanyProfileListItemResponse]
    total: int
    page: int
    page_size: int


# --- suppression de masse (lot suppression par filtre) -----------------------


class CompanyListFilter(BaseModel):
    """Les critères de l'annuaire SOCIÉTÉ — même déclaration que les
    paramètres de `GET /company-profiles`, appliqués par les mêmes
    prédicats (`CompanyProfilesRepository.filter_predicates`). Sans tri ni
    pagination : un filtre vise l'ensemble, jamais la page courante."""

    model_config = ConfigDict(extra="forbid")

    search: str | None = None
    tags: list[str] | None = None
    has_active_case: bool | None = None
    has_people: bool | None = None


class CompanyBulkDeleteRequest(BaseModel):
    """Le miroir strict de la face personne : `ids` (plafonné) OU
    `filter` (sans plafond), jamais les deux, jamais aucun."""

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    filter: CompanyListFilter | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def one_selector_exactly(self) -> "CompanyBulkDeleteRequest":
        if (self.ids is None) == (self.filter is None):
            raise ValueError("Provide exactly one of `ids` or `filter`.")
        return self


class CompanyFieldLabelUpdate(BaseModel):
    """RENOMMER une clé de l'univers société — le SEUL geste que cet
    univers autorise (rien n'y est archivable ni typable : ses champs
    n'ont pas de définition, cf. `field_universe`).

    `label = null` RETIRE la personnalisation : la clé retrouve son
    libellé d'origine (catalogue ou clé nue). Sans ça, une agence qui
    s'est trompée n'aurait aucun moyen de revenir en arrière."""

    label: str | None = Field(default=None, max_length=200)

    @field_validator("label")
    @classmethod
    def _no_blank(cls, v: str | None) -> str | None:
        # Une chaîne d'espaces n'est pas un libellé ; `null` est le geste
        # explicite de retour au défaut, « » ne l'est pas.
        if v is not None and not v.strip():
            raise ValueError("`label` must not be blank (use null to reset).")
        return v.strip() if v else None


class CompanyFieldLabelResponse(BaseModel):
    key: str
    label: str
    # Vrai = l'agence a posé ce libellé ; faux = c'est le défaut servi.
    customized: bool
