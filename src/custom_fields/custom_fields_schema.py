import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.enums import CustomFieldType

# A slug: lowercase letters/digits/underscores. Stable JSONB key.
_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"
_SELECT_TYPES = {CustomFieldType.SELECT, CustomFieldType.MULTI_SELECT}


class CustomFieldDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    # BLOC 2bis — RAW i18n blob for the editor (the resolved `label` is set by
    # the listing endpoint; `key` stays the untranslated identifier).
    label_i18n: dict[str, str]
    field_type: str
    options: list[str] | None
    # Portée (chantier fiches) : 'person' = trait stable du client (fiche,
    # promouvable) ; 'case' = propre à la mission. L'AGENCE est le juge
    # final — reclassable par le PATCH (toggle scope).
    scope: str
    profile_section: str
    required: bool
    position: int
    archived_at: datetime | None


class CustomFieldDefinitionCreate(BaseModel):
    key: str = Field(pattern=_KEY_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    label_i18n: dict[str, str] | None = None
    field_type: CustomFieldType
    options: list[str] | None = None
    required: bool = False
    position: int = 0
    # LA PORTÉE, ENFIN CHOISIE (constat champs invisibles). Elle manquait
    # à ce contrat : l'API ne l'acceptait pas, le défaut `case` de
    # `build_definition` s'appliquait donc TOUJOURS, et un champ créé
    # depuis les réglages ne pouvait structurellement jamais apparaître
    # sur une fiche client. Le front l'envoie désormais explicitement.
    #
    # Absent = `case`, le comportement d'avant : ce lot ouvre le choix, il
    # ne déplace pas le défaut (un appelant existant qui se tait garde ce
    # qu'il avait).
    scope: Literal["person", "case"] | None = None

    @model_validator(mode="after")
    def _check_options(self) -> "CustomFieldDefinitionCreate":
        if self.field_type in _SELECT_TYPES:
            if not self.options:
                raise ValueError(f"{self.field_type} requires a non-empty `options` list.")
            if len(set(self.options)) != len(self.options):
                raise ValueError("`options` must be unique.")
        elif self.options is not None:
            raise ValueError(f"`options` is only valid for {[t.value for t in _SELECT_TYPES]}.")
        return self


class CustomFieldBulkRequest(BaseModel):
    """LES GESTES DE MASSE sur les définitions — par LISTE D'IDS, jamais
    par filtre : l'agence traite ce qu'elle a sélectionné à l'écran, donc
    ce qu'elle voit (même principe que la suppression de masse des fiches,
    où le filtre est le geste ; ici la sélection l'est).

    `dry_run` rend le MÊME rapport sans rien écrire : c'est ce qui permet
    à l'écran d'annoncer AVANT plutôt que d'expliquer après. Une seule
    évaluation sert les deux (cf. `_evaluate`) — le compte annoncé et le
    geste appliqué ne peuvent pas diverger."""

    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: Literal["archive", "scope", "section"]
    # Cible du geste, selon l'action (validé ci-dessous).
    scope: Literal["person", "case"] | None = None
    profile_section: Literal["identity", "contact", "situation", "misc"] | None = None
    dry_run: bool = False
    # LE FRANCHISSEMENT EXPLICITE de la protection « utilisé dans un
    # parcours » (archivage seul). Faux par défaut : le geste ne peut donc
    # jamais retirer EN SILENCE un champ qu'un parcours collecte. Vrai =
    # l'agence a vu les champs NOMMÉS et redemande. Même mot, même sens
    # que le `force` de l'archivage à l'unité.
    force: bool = False

    @model_validator(mode="after")
    def _check_target(self) -> "CustomFieldBulkRequest":
        if self.action == "scope" and self.scope is None:
            raise ValueError("`scope` is required for the 'scope' action.")
        if self.action == "section" and self.profile_section is None:
            raise ValueError("`profile_section` is required for the 'section' action.")
        if self.action == "archive" and (self.scope or self.profile_section):
            raise ValueError("The 'archive' action takes no target.")
        return self


class CustomFieldBulkRefusal(BaseModel):
    """Un refus NOMMÉ : l'écran doit pouvoir dire « Adresse fiscale »,
    pas « 3 champs refusés »."""

    id: uuid.UUID
    # Nuls pour un id inconnu — on ne fabrique pas un nom qu'on n'a pas.
    key: str | None = None
    label: str | None = None
    reason: Literal["not_found", "used_in_journey"]
    # Les parcours concernés, par leur nom (vide si `not_found`).
    templates: list[str] = Field(default_factory=list)


class CustomFieldBulkReport(BaseModel):
    """Le rapport agrégé, identique en dry-run et en réel (seul `applied`
    reste à 0 en dry-run)."""

    dry_run: bool
    requested: int  # ids reçus, dédupliqués
    eligible: int  # ce que le geste touche (ou toucherait)
    applied: int  # effectivement modifiées — 0 en dry-run
    unchanged: int  # déjà dans l'état demandé : ni traitées, ni refusées
    refused: int
    # Les conséquences à annoncer AVANT, pas à expliquer après.
    with_values: int  # champs éligibles portant au moins une valeur saisie
    values_count: int  # valeurs saisies concernées (fiches + sociétés + dossiers)
    in_journey: int  # champs éligibles collectés/exigés par un parcours
    refusals: list[CustomFieldBulkRefusal] = Field(default_factory=list)


class CustomFieldDefinitionUpdate(BaseModel):
    """`key` and `field_type` are IMMUTABLE — deliberately absent.
    `scope` est le TOGGLE de reclassification (annuaire, sections) : les
    valeurs ne bougent JAMAIS (elles vivent dans les sacs JSONB) — seule
    la surface change (fiche/complétude/prefill pour 'person')."""

    scope: Literal["person", "case"] | None = None
    # Taxonomie fiche : reclasser la section de la définition (toggle
    # élargi — même PATCH, même gate). 4 valeurs depuis la fusion
    # id_documents → identity (parité personne/société).
    profile_section: Literal["identity", "contact", "situation", "misc"] | None = None
    label: str | None = Field(default=None, min_length=1, max_length=200)
    label_i18n: dict[str, str] | None = None
    options: list[str] | None = None
    required: bool | None = None
    position: int | None = None
