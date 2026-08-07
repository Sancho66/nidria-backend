"""Schémas du domaine fiches client (F1 lecture + F2 gestes)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.cases.cases_schema import _CivilStatusFields
from src.core.email import NormalizedEmailStr
from src.core.i18n import Language


class ProfileCaseSummaryResponse(BaseModel):
    """Un dossier de la fiche, en résumé. `current_step_name` est résolu
    dans la langue par défaut de l'AGENCE (annuaire F3.3) — même règle de
    bande de progression que la liste dossiers, batché sans N+1."""

    id: uuid.UUID
    status: str
    journey_name: str | None
    current_step_name: str | None = None
    reference: str | None
    created_at: datetime


class ProfileCompletenessResponse(BaseModel):
    """La complétude transversale (F2.4) : quels champs de portée personne
    sont déjà valorisés sur la fiche — le futur allègement de collecte."""

    filled: list[str]
    missing: list[str]


class ProfileFieldSectionResponse(BaseModel):
    """Une catégorie du RAIL DU PICKER sur la fiche (correctif final rail) :
    TOUTES les catégories du catalogue sont servies — même source, même
    ordre, même clé stable (`key`) — y compris à zéro champ person (le
    front décide de leur rendu). Le panier « Sans catégorie » (key/name
    null) ferme la liste s'il reste des clés hors catalogue."""

    key: str | None
    # Le nom RÉSOLU en langue d'agence — le repli, et ce qu'attendent les
    # appelants d'avant. Il reste servi : ce lot ajoute, il ne déplace pas.
    name: str | None
    # Le nom BRUT ×7 (demande D7), même forme que `label_i18n` sur les
    # champs et pour la même raison : le nom d'une section se résout dans
    # la langue du VISITEUR, pas dans celle de l'agence. Servir le seul
    # résolu obligeait l'écran à retraduire les clés qu'il connaît et à
    # retomber sur le servi pour les autres — un repli qui MENTIRA dès
    # qu'une agence créera sa propre section (lot 2), puisqu'il n'y aura
    # plus de table côté écran où la retrouver.
    #
    # PAS DE DÉFAUT : un producteur qui se tairait servirait `{}` en
    # silence, et l'écran retomberait sur le nom d'agence sans que rien ne
    # le signale — exactement le motif de l'arbitrage `scope` du 07/08.
    # Vide est une réponse LÉGITIME (le panier « Sans catégorie », qui n'a
    # pas de nom du tout), donc elle doit être ÉCRITE, pas déduite.
    name_i18n: dict[str, str]
    references: list[str]


class ProfileCompanyLinkResponse(BaseModel):
    """« Ses sociétés » (lecture INVERSE du lien personne↔société) : tout
    ce qu'il faut pour afficher ET dissocier — le DELETE existant côté
    société est appelable avec (company_id, role_id) tels quels."""

    company_id: uuid.UUID
    name: str
    role: str
    role_label: str | None
    role_id: uuid.UUID


class ClientProfileListItemResponse(BaseModel):
    # `id` EST le client_profile_id (une seule clé, pas de champ dupliqué).
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    cases_count: int
    active_cases_count: int
    # Statut client DÉRIVÉ (Phase 0 D9 : jamais deux vérités) — 'prospect'
    # si aucun dossier au-delà de prospect, sinon le plus avancé.
    derived_status: str
    status_override: str | None = None
    tags: list[str] = []
    # Dernière activité : max(activity_log) des dossiers vivants de la
    # fiche ; à défaut, updated_at de la fiche (verdict annuaire F3.2).
    last_activity_at: datetime | None = None
    client_space_activated: bool = False
    created_at: datetime


class ClientProfileListResponse(BaseModel):
    items: list[ClientProfileListItemResponse]
    total: int
    page: int
    page_size: int


class ClientProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # NULL = fiche pas encore liée à un compte (création directe F4) —
    # l'identité vient alors des colonnes propres de la fiche.
    expat_user_id: uuid.UUID | None
    first_name: str
    last_name: str
    email: str
    preferred_lang: str | None
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
    status_override: str | None = None
    # Ses sociétés — la lecture inverse du lien (une jointure, pas de N+1).
    companies: list[ProfileCompanyLinkResponse] = []
    completeness: ProfileCompletenessResponse
    # Lot sections : les références person groupées par leurs sections
    # réelles — même univers que la complétude, fini le fourre-tout.
    sections: list[ProfileFieldSectionResponse] = []
    created_at: datetime
    updated_at: datetime


class ClientProfileCreateRequest(_CivilStatusFields):
    """Création DIRECTE de fiche (complément 2, F4) — le prospect à froid,
    AVANT tout dossier. `email` requis : il porte la dédup (409 par agence)
    et la liaison différée au premier dossier (adoption par email). Même
    mixin civil que le PATCH ; `custom_fields` scope='person' seules."""

    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    # OPTIONNEL (dernier complément) : un prospect à froid n'a pas
    # toujours d'email. Sans lui : pas de dédup (rien à comparer), pas de
    # liaison différée par email, et « Nouvelle démarche » répond 422
    # profile.no_email tant qu'un email n'est pas posé au PATCH.
    email: NormalizedEmailStr | None = None
    custom_fields: dict[str, Any] | None = None


class ClientProfileUpdateRequest(_CivilStatusFields):
    """PATCH de la fiche — MIROIR de `PersonUpdateRequest` : même mixin
    civil (mêmes types, mêmes longueurs, mêmes enums), mêmes sémantiques
    exclude_unset (champ absent = intouché, null explicite = effacé),
    `custom_fields` en merge partiel — clés scope='person' SEULES (une clé
    de portée dossier → 422 nommé).

    Écarts avec PersonUpdateRequest, NOMMÉS : pas de `full_name` /
    `relationship` (concepts de personne-au-dossier ; l'identité de la
    fiche vit sur le compte expat_user), pas d'`email` (liaison de compte,
    pas une donnée de fiche). `source`/`tags` ne sont pas non plus dans ce
    PATCH (absents aussi du contrat person — à ouvrir si le front le
    demande)."""

    model_config = ConfigDict(extra="forbid")

    custom_fields: dict[str, Any] | None = None
    # V1b : forcer le statut (null explicite = retour à la dérivation).
    status_override: Literal["prospect", "client"] | None = None
    # Complément PATCH : la langue (registre d'agence, prime à la lecture,
    # null = retour à la préférence du compte) et l'email — éditable
    # SEULEMENT sans compte lié (422 nommé sinon), dédup 409 à jour.
    preferred_lang: Language | None = None
    email: NormalizedEmailStr | None = None
    # Actions groupées (2b) : tags éditables au PATCH — l'écart « à ouvrir
    # si le front le demande » est levé (liste REMPLACÉE, pas fusionnée :
    # le front envoie l'état voulu ; l'ajout groupé lit puis re-poste).
    tags: list[str] | None = None


class ProfileActivityEntryResponse(BaseModel):
    """Une ligne du fil d'activité de la fiche (complément sections) —
    la forme dossier (`ActivityLogResponse`) + le dossier d'ORIGINE.
    Lecture croisée : aucun journal nouveau."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    action_type: str
    details: dict[str, Any]
    created_at: datetime
    case_id: uuid.UUID
    case_reference: str | None


class ProfileActivityListResponse(BaseModel):
    items: list[ProfileActivityEntryResponse]
    total: int
    page: int
    page_size: int


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


# --- suppression de masse (lot suppression par filtre) -----------------------


class ProfileListFilter(BaseModel):
    """LES CRITÈRES DE L'ANNUAIRE — la MÊME déclaration que les paramètres
    de `GET /client-profiles`.

    C'est ce qui rend « tout ce que je vois » == « tout ce que je
    supprime » : le front renvoie les filtres qu'il a à l'écran, tels
    quels, et le back les applique par les mêmes prédicats SQL
    (`ClientProfilesRepository.filter_predicates`, une seule copie).

    Volontairement SANS `sort_by` / `page` : trier ou paginer ne change
    pas QUI est visé, seulement l'ordre et la tranche montrée. Une
    suppression par filtre vise l'ensemble, jamais la page courante.
    """

    model_config = ConfigDict(extra="forbid")

    search: str | None = None
    status: Literal["prospect", "client"] | None = None
    tags: list[str] | None = None
    client_space_activated: bool | None = None
    has_active_case: bool | None = None


class ProfileBulkDeleteRequest(BaseModel):
    """DEUX FORMES, exclusives : une sélection à la main (`ids`, plafonnée
    — au-delà on filtre) ou un critère (`filter`, sans plafond : c'est
    tout son intérêt).

    `filter: {}` est LÉGITIME et signifie « toutes les fiches de
    l'agence » — c'est ce que montre une liste sans filtre. Il faut
    l'écrire explicitement : l'absence des deux champs est refusée, on ne
    supprime pas tout par omission.
    """

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    filter: ProfileListFilter | None = None
    #: Le compte AVANT le geste. Même chemin, même chiffres — seule la
    #: dernière instruction change.
    dry_run: bool = False

    @model_validator(mode="after")
    def one_selector_exactly(self) -> "ProfileBulkDeleteRequest":
        if (self.ids is None) == (self.filter is None):
            raise ValueError("Provide exactly one of `ids` or `filter`.")
        return self


class ProfileBulkResetStatusRequest(BaseModel):
    """« Réinitialiser le statut » — LE RATTRAPAGE d'un import mal réglé.

    Même grammaire que la suppression de masse (une sélection `ids` OU un
    critère `filter`, jamais les deux ; `filter: {}` = toute l'agence, à
    écrire explicitement ; `dry_run` pour annoncer avant d'agir). La
    raison d'être est simple : un `default_status` posé de travers sur un
    fichier de 1600 lignes ne se rattrape pas fiche par fiche.

    Le geste EFFACE l'override — il ne pose pas l'autre statut. La fiche
    repasse en dérivation (prospect sans dossier vivant, client dès qu'il
    y en a un), c'est-à-dire à l'état où elle serait sans intervention.
    """

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    filter: ProfileListFilter | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def one_selector_exactly(self) -> "ProfileBulkResetStatusRequest":
        if (self.ids is None) == (self.filter is None):
            raise ValueError("Provide exactly one of `ids` or `filter`.")
        return self


class ProfileBulkResetStatusReport(BaseModel):
    """`matching` — ce que le critère désigne. `with_override` — celles qui
    portent VRAIMENT un statut forcé, donc le nombre que le geste changera :
    c'est LE chiffre à annoncer. `reset` — ce qui a réellement repris la
    dérivation (0 en dry-run)."""

    dry_run: bool
    matching: int
    with_override: int
    reset: int
