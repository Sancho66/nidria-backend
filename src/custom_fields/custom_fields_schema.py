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
    # REQUISE, SAUF POUR UN CHAMP SOCIÉTÉ où elle est DÉRIVÉE du libellé
    # côté serveur (slugify, la moulinette de la grille d'import — cf.
    # `_create_company`) : l'écran société ne montre aucune clé, il n'a
    # donc rien à en faire saisir, et deux fabriques de clés pour la même
    # table auraient divergé au premier caractère accentué. L'envoyer
    # quand même sur `scope='company'` est refusé, pas ignoré.
    key: str | None = Field(default=None, pattern=_KEY_PATTERN)
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
    #
    # `company` EST UNE PORTÉE COMME LES AUTRES (D9) : créer un champ de
    # fiche société passe par CETTE route, celle que le PATCH, l'archivage
    # et la masse servent déjà pour les deux faces. Une porte dédiée
    # aurait fait diverger deux fois le même geste — et le front aurait
    # appris une seconde route pour un contrat à 3 champs près.
    #
    # Ce que `company` change, et rien d'autre : la clé est DÉRIVÉE (voir
    # `key`), la section est REQUISE (elle n'a pas de berceau implicite de
    # ce côté), et `options`/`required` n'existent pas sur cette face —
    # `_create_company` refuse nommément plutôt que d'accepter sans rien
    # en faire.
    scope: Literal["person", "case", "company"] | None = None
    # La SECTION de naissance. Absente = « Divers », le berceau — c'était
    # déjà le comportement, il devient dicible : créer un champ
    # directement dans « Coordonnées » ne demande plus un second appel.
    # REQUISE sur `scope='company'` (arbitrage D9) : un repli silencieux
    # ferait naître le champ ailleurs que là où l'agence l'a déposé.
    profile_section: str | None = Field(default=None, min_length=1, max_length=50)

    @model_validator(mode="after")
    def _check_options(self) -> "CustomFieldDefinitionCreate":
        if self.scope == "company":
            if self.field_type in _SELECT_TYPES:
                # STRUCTUREL, pas une restriction d'humeur :
                # `company_field_definition` n'a pas de colonne `options`
                # (l'univers société la sert à `null`, le PATCH la refuse).
                # Une liste de choix y naîtrait sans choix.
                raise ValueError(
                    "A company field cannot be a choice list — the company sheet has no `options`."
                )
        elif self.key is None:
            raise ValueError("`key` is required (it is derived from the label only for `company`).")
        if self.field_type in _SELECT_TYPES:
            if not self.options:
                raise ValueError(f"{self.field_type} requires a non-empty `options` list.")
            if len(set(self.options)) != len(self.options):
                raise ValueError("`options` must be unique.")
        elif self.options is not None:
            raise ValueError(f"`options` is only valid for {[t.value for t in _SELECT_TYPES]}.")
        return self


class FieldUniverseEntry(BaseModel):
    """UN champ tel que l'ÉCRAN le montre, avec ce qu'on peut en faire.

    `state` est le cœur du contrat — il évite au front de déduire des
    droits d'une absence :
    - `declared` : une `custom_field_definition` existe → éditable,
      archivable, déplaçable (`definition_id` la désigne) ;
    - `native` : servi par le produit lui-même (les 10 colonnes civiles
      de la fiche personne, les 17 presets société) → s'affiche, ne
      s'archive JAMAIS ;
    - `catalog_undeclared` : le catalogue le connaît, l'agence ne l'a pas
      déclaré → l'écran peut proposer de l'ajouter ;
    - `sack_only` : découvert dans les valeurs d'une société, sans preset
      ni définition → renommable, rien d'autre.
    """

    reference: str
    label: str
    # Le type : celui de la définition quand elle existe, celui du
    # CATALOGUE pour un preset non déclaré (le back le résout déjà pour le
    # libellé — le taire obligerait l'écran à consulter sa propre copie du
    # catalogue). Nul pour une colonne civile, un preset société et une
    # clé de sack : elles n'exposent pas de type.
    field_type: str | None = None
    # D'où vient la clé : `catalog` = le PRODUIT la connaît (preset,
    # colonne civile, preset société) ; `agency` = l'agence l'a écrite.
    # Servie plutôt que déduite d'une table de clés recopiée côté écran.
    # Sur la surface `case`, elle distingue une référence ORPHELINE
    # (`catalog_undeclared` + `agency` : une définition disparue qu'un
    # parcours cite encore) d'un preset réellement proposable.
    origin: Literal["catalog", "agency"] = "agency"
    section: str
    state: Literal["declared", "native", "catalog_undeclared", "sack_only"]
    definition_id: uuid.UUID | None = None
    required: bool | None = None
    # LA POSITION SERT AU GESTE, PAS AU TRI : l'ordre servi dans chaque
    # section est déjà celui de l'écran, le front ne recompose rien. Elle
    # n'existe que pour les entrées `declared` — une native, un preset non
    # déclaré ou une clé de sack n'ont aucune position à déplacer, d'où
    # `null` (et non 0, qui se confondrait avec la première place).
    #
    # C'est une suite GLOBALE à l'agence, pas un rang dans la section :
    # deux entrées voisines à l'écran peuvent porter 3 et 47.
    #
    # DÉPOSER ENTRE DEUX LIGNES SANS POSITION (deux natives, par exemple) :
    # la définition déplacée prend la position de la PREMIÈRE ENTRÉE
    # `declared` QUI SUIT dans l'ordre servi ; s'il n'y en a aucune, elle
    # passe après la dernière (max + 1). Les voisines sans position sont
    # ignorées — elles n'occupent aucun rang. Règle écrite ICI, à
    # l'endroit que les deux côtés lisent ; elle s'applique côté écran
    # (voir le rapport : le point de dépôt n'existe que là), puis se
    # persiste par le PATCH de position déjà en place.
    position: int | None = Field(
        default=None,
        description=(
            "Position de la définition — sert au GESTE de déplacement, jamais au tri "
            "(l'ordre servi dans chaque section est déjà celui de l'écran). Nulle pour "
            "`native`, `catalog_undeclared` et `sack_only` : rien à déplacer. Suite "
            "GLOBALE à l'agence, pas un rang dans la section — deux entrées voisines "
            "peuvent porter 3 et 47. DÉPÔT ENTRE DEUX ENTRÉES SANS POSITION : la "
            "définition déplacée prend la position de la première entrée `declared` qui "
            "SUIT dans l'ordre servi ; s'il n'y en a aucune, max + 1. Les voisines sans "
            "position sont ignorées, elles n'occupent aucun rang. Persistance par "
            "PATCH /agencies/me/custom-fields/{field_id} {position}."
        ),
    )
    # Surface `case` uniquement : le même champ sert couramment 90
    # parcours — une entrée, un compte, jamais 90 lignes.
    used_in_journeys: int | None = None
    # Surface `company` : le libellé se personnalise (company_field_label)
    # alors que rien d'autre ne se touche.
    renamable: bool | None = None


class FieldUniverseSection(BaseModel):
    key: str
    name: str
    fields: list[FieldUniverseEntry]


class FieldUniverseResponse(BaseModel):
    """Les sections DANS L'ORDRE DE L'ÉCRAN — le front ne recompose rien,
    il rend ce qu'il reçoit."""

    surface: Literal["person", "company", "case"]
    sections: list[FieldUniverseSection]


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
    # Même bascule que le PATCH à l'unité : la clé se valide contre les
    # sections de l'agence, pas contre une liste gravée au contrat.
    profile_section: str | None = Field(default=None, min_length=1, max_length=50)
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
    # élargi — même PATCH, même gate).
    #
    # PLUS DE `Literal` ICI (lot sections configurables du 07/08) : les 4
    # clés y étaient gravées, donc une section créée par une agence était
    # refusée en 422 PAR SON PROPRE BACK. La validation devient une
    # VÉRIFICATION PAR TENANT — la clé doit exister dans les sections de
    # CETTE agence, sur la surface de CE champ (`profile_section.unknown`,
    # qui NOMME les sections disponibles). Une clé inventée reste refusée ;
    # une clé légitime passe enfin.
    profile_section: str | None = Field(default=None, min_length=1, max_length=50)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    label_i18n: dict[str, str] | None = None
    options: list[str] | None = None
    required: bool | None = None
    position: int | None = None
