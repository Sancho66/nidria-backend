"""L'UNIVERS DES CIBLES de l'import FICHES — une seule source, et la
VUE SERVIE de cet univers (`GET /imports/targets`).

Le constat qui a fait ce module : `POST /imports/mappings` refusait
`residence_address.street` (« Unknown person-referential targets ») alors
que l'import, lui, l'accepte — une agence pouvait composer une adresse
dans le wizard sans pouvoir ENREGISTRER la correspondance. La cause
n'était pas la règle mais sa DUPLICATION : trois copies personne
(l'import, le suggéreur, la config) et deux copies société, qui ont
dérivé l'une de l'autre lot après lot.

Ici vit LA définition, et les cinq appelants la consomment. Un test
structurel prouve l'égalité des univers import/config : toute cible que
l'import accepte, la config l'enregistre.

Le sixième appelant est l'ENDPOINT : `person_target_catalog` /
`company_target_catalog` habillent le MÊME `valid` de ce dont un
combobox a besoin (libellé ×7, type, section, flags) et le servent au
front. Le front n'a donc plus de miroir : ce qu'il offre EST ce que
l'import accepte, par construction et non par recopie.

Note de grammaire : l'import PARCOURS (`case_import`) parle une autre
langue (`custom_field:<clé>` contre les fiches déclarées d'un parcours)
et a déjà SA validation partagée (`mapping_validation`) — deux univers
distincts, assumés, qui ne se mélangent pas.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.company_profile import CompanyFieldDefinition
from shared.models.custom_field import CustomFieldDefinition

# L'identité de la fiche personne : les 3 colonnes du contrat person.
IDENTITY_TARGETS: tuple[str, ...] = ("first_name", "last_name", "email")
# Cibles STRUCTURELLES personne : `tags` (split ,/; dédupliqué) et la
# COLONNE langue (le preset preferred_language est mort au dédoublonnage).
# `status_override` (lot statut) : STRUCTURELLE comme les deux autres —
# c'est une colonne de la fiche, pas un champ du référentiel. NULL = la
# dérivation joue (prospect sans dossier vivant, client dès qu'il y en a
# un) ; posée, elle prime.
PERSON_STRUCTURAL_TARGETS: frozenset[str] = frozenset({"tags", "preferred_lang", "status_override"})
# Côté société : la dénomination (clé de dédup) et les étiquettes.
COMPANY_STRUCTURAL_TARGETS: frozenset[str] = frozenset({"name", "tags"})
# Les deux bases adresse de la fiche société (pas de référentiel société
# au MVP : ces bases sont en dur, à l'image du plan de valeurs).
COMPANY_ADDRESS_BASES: frozenset[str] = frozenset({"address", "headquarters_address"})


@dataclass(frozen=True)
class PersonTargets:
    """L'univers personne + les pièces dont l'analyse a besoin (les défs
    pour coercer, les bases adresse pour composer)."""

    defs_by_key: dict[str, CustomFieldDefinition] = field(default_factory=dict)
    preset_keys: set[str] = field(default_factory=set)
    address_bases: set[str] = field(default_factory=set)
    dotted: set[str] = field(default_factory=set)
    valid: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class CompanyTargets:
    labels_by_key: dict[str, CompanyFieldDefinition] = field(default_factory=dict)
    keys_by_label: dict[str, str] = field(default_factory=dict)
    address_bases: set[str] = field(default_factory=set)
    dotted: set[str] = field(default_factory=set)
    valid: set[str] = field(default_factory=set)


async def person_targets(db: AsyncSession, agency_id: uuid.UUID) -> PersonTargets:
    """Les cibles d'import PERSONNE d'une agence.

    Le CATALOGUE ENTIER est cible (lot plafond : un preset non déclaré se
    déclare à l'import) + les customs scope='person' déclarés, moins
    l'univers société (demande design A — même déclarées, ces clés ne
    sont plus des cibles personne). Les bases typées `address` ouvrent
    leurs SOUS-CHAMPS (`<base>.street|city|postal_code|country`) en plus
    du texte intégral.
    """
    from src.client_profiles.backfill import CIVIL_COLUMNS
    from src.client_profiles.client_profiles_manager import person_scope_definitions
    from src.client_profiles.profile_sections import (
        PERSON_SHEET_EXCLUDED_KEYS,
        PRESET_PROFILE_SECTION,
    )
    from src.imports.value_normalizers import ADDRESS_SUBFIELDS
    from src.journeys.field_catalog import FIELD_PRESETS

    person_defs = await person_scope_definitions(db, agency_id)
    defs_by_key = {d.key: d for d in person_defs if d.key not in PERSON_SHEET_EXCLUDED_KEYS}
    preset_keys = set(PRESET_PROFILE_SECTION) - PERSON_SHEET_EXCLUDED_KEYS
    address_bases = {k for k, d in defs_by_key.items() if d.field_type == "address"} | {
        k for k in preset_keys if k not in defs_by_key and FIELD_PRESETS[k].field_type == "address"
    }
    dotted = {f"{base}.{sub}" for base in address_bases for sub in ADDRESS_SUBFIELDS}
    valid = (
        set(IDENTITY_TARGETS)
        | set(CIVIL_COLUMNS)
        | set(defs_by_key)
        | preset_keys
        | dotted
        | set(PERSON_STRUCTURAL_TARGETS)
    )
    return PersonTargets(
        defs_by_key=defs_by_key,
        preset_keys=preset_keys,
        address_bases=address_bases,
        dotted=dotted,
        valid=valid,
    )


async def company_targets(db: AsyncSession, agency_id: uuid.UUID) -> CompanyTargets:
    """Les cibles d'import SOCIÉTÉ d'une agence : le plan de valeurs
    company, ses alias de compat, les deux bases adresse en sous-champs,
    et les clés BAPTISÉES de l'agence (nées de la grille — leur label et
    leur kind de naissance vivent en table)."""
    from src.client_profiles.profile_sections import (
        COMPANY_PRESET_PROFILE_SECTION,
        COMPANY_TARGET_ALIASES,
    )
    from src.company_profiles.company_profiles_repository import CompanyProfilesRepository
    from src.imports.value_normalizers import ADDRESS_SUBFIELDS

    label_rows = await CompanyProfilesRepository(db).field_definitions(agency_id)
    labels_by_key = {row.key: row for row in label_rows}
    keys_by_label = {row.label.strip().lower(): row.key for row in label_rows}
    dotted = {f"{base}.{sub}" for base in COMPANY_ADDRESS_BASES for sub in ADDRESS_SUBFIELDS}
    valid = (
        set(COMPANY_STRUCTURAL_TARGETS)
        | set(COMPANY_PRESET_PROFILE_SECTION)
        | set(COMPANY_TARGET_ALIASES)
        | dotted
        | set(labels_by_key)
    )
    return CompanyTargets(
        labels_by_key=labels_by_key,
        keys_by_label=keys_by_label,
        address_bases=set(COMPANY_ADDRESS_BASES),
        dotted=dotted,
        valid=valid,
    )


# ─── LA VUE SERVIE de l'univers (GET /imports/targets) ────────────────────
# Le `valid` ci-dessus est un ensemble de jetons — de quoi ACCEPTER. Un
# combobox a besoin de plus : de quoi AFFICHER. Les deux se calculent
# ici, côte à côte, sur la même lecture de base : impossible d'offrir ce
# que l'import refuse, impossible de refuser ce que la liste offre.


@dataclass(frozen=True)
class ImportSectionSpec:
    """Une section de la taxonomie FICHE, avec son libellé ×7."""

    key: str
    label_i18n: dict[str, str]


@dataclass(frozen=True)
class ImportTargetSpec:
    """UNE cible offerte au combobox.

    `label` est le repli SCALAIRE (le libellé d'agence pour une clé
    baptisée, sinon le français) ; `label_i18n` porte les langues
    connues. Le routeur résout l'un contre l'autre pour la langue du
    visiteur — même chaîne que partout ailleurs (`resolve_i18n`).
    """

    key: str
    label: str
    label_i18n: dict[str, str]
    field_type: str
    section: str
    required: bool = False
    # Sous-champ d'adresse : la base qu'il compose, et lequel des 4.
    address_base: str | None = None
    address_subfield: str | None = None
    # Preset du catalogue que l'agence n'a PAS déclaré : l'import le
    # crée en chemin — la liste le dit AVANT d'agir (« sera ajouté »).
    will_create: bool = False
    # Clé BAPTISÉE : son libellé vient de l'agence, pas du produit — une
    # seule langue, celle où il a été écrit. On ne traduit pas ce qu'on
    # ne connaît pas, et on ne le déguise pas non plus.
    agency_named: bool = False


def _subfield_specs(base: ImportTargetSpec) -> list[ImportTargetSpec]:
    """Les 4 morceaux d'une base adresse, juste après elle."""
    from src.imports.target_labels import ADDRESS_SUBFIELD_LABELS
    from src.imports.value_normalizers import ADDRESS_SUBFIELDS

    return [
        ImportTargetSpec(
            key=f"{base.key}.{sub}",
            label=ADDRESS_SUBFIELD_LABELS[sub]["fr"],
            label_i18n=dict(ADDRESS_SUBFIELD_LABELS[sub]),
            field_type="text",
            section=base.section,
            address_base=base.key,
            address_subfield=sub,
            will_create=base.will_create,
            agency_named=base.agency_named,
        )
        for sub in ADDRESS_SUBFIELDS
    ]


def _flatten(
    buckets: dict[str, list[ImportTargetSpec]], sections: dict[str, dict[str, str]]
) -> list[ImportTargetSpec]:
    """Les sections dans l'ordre de la taxonomie, et dans chacune les
    cibles dans l'ordre où elles ont été posées.

    Les MORCEAUX d'adresse ferment leur section, groupés par base. Le
    combobox ouvre un sous-groupe par base (« Adresse de résidence › en
    morceaux ») : les intercaler juste après leur base couperait la
    section en deux et son titre s'afficherait deux fois. Ils restent
    donc DANS leur section — bien plus près qu'au fond d'une liste de 74
    options, ce qu'ils étaient quand le front les dérivait."""
    out: list[ImportTargetSpec] = []
    for section in sections:
        specs = buckets[section]
        out.extend(specs)
        for spec in specs:
            if spec.field_type == "address":
                out.extend(_subfield_specs(spec))
    return out


async def person_target_catalog(
    db: AsyncSession, agency_id: uuid.UUID
) -> tuple[list[ImportSectionSpec], list[ImportTargetSpec]]:
    """L'univers PERSONNE, habillé pour le combobox.

    Ordre : la taxonomie fiche (identité → contact → situation →
    divers) ; dans l'identité, le trio obligatoire d'abord. Dans chaque
    section : les colonnes civiles (ordre du catalogue), les presets
    (ordre du catalogue), puis les clés d'agence. Une base adresse est
    immédiatement suivie de ses 4 morceaux.

    La section d'une clé DÉCLARÉE vient de sa définition
    (`profile_section`, la vérité vivante que le toggle édite) ; celle
    d'un preset non déclaré, de la table de migration. Même arbitre que
    la fiche — les deux écrans rangent pareil.
    """
    from src.client_profiles.backfill import CIVIL_COLUMNS
    from src.client_profiles.profile_sections import (
        CIVIL_PROFILE_SECTION,
        IDENTITY_SECTION_ORDER,
        PRESET_PROFILE_SECTION,
        PROFILE_SECTIONS,
    )
    from src.imports.target_labels import (
        CIVIL_FIELD_TYPES,
        CIVIL_LABELS,
        IDENTITY_LABELS,
        STATUS_LABEL,
        TAGS_LABEL,
    )
    from src.journeys.field_catalog import FIELD_PRESETS

    targets = await person_targets(db, agency_id)
    buckets: dict[str, list[ImportTargetSpec]] = {key: [] for key in PROFILE_SECTIONS}
    seen: set[str] = set()

    def emit(spec: ImportTargetSpec) -> None:
        if spec.key in seen:
            return
        seen.add(spec.key)
        buckets[spec.section if spec.section in buckets else "misc"].append(spec)

    # 1. Le trio obligatoire — sans lui l'import ne part pas.
    for key in IDENTITY_TARGETS:
        emit(
            ImportTargetSpec(
                key=key,
                label=IDENTITY_LABELS[key]["fr"],
                label_i18n=dict(IDENTITY_LABELS[key]),
                field_type="text",
                section="identity",
                required=True,
            )
        )

    # 2. Les colonnes civiles natives, l'état civil avant les documents.
    rank = {key: index for index, key in enumerate(IDENTITY_SECTION_ORDER)}
    for key in sorted(CIVIL_COLUMNS, key=lambda k: (rank.get(k, len(rank)), k)):
        emit(
            ImportTargetSpec(
                key=key,
                label=CIVIL_LABELS[key]["fr"],
                label_i18n=dict(CIVIL_LABELS[key]),
                field_type=CIVIL_FIELD_TYPES[key],
                section=CIVIL_PROFILE_SECTION[key],
            )
        )

    def field_spec(key: str, fallback_section: str) -> ImportTargetSpec:
        """Un champ de fiche : preset du catalogue, définition d'agence,
        ou les deux (une agence DÉCLARE un preset). Le libellé se lit
        alors dans l'ordre du fix traductions — les traductions de
        l'agence priment LANGUE PAR LANGUE, le catalogue comble le reste,
        et une clé hors catalogue n'a que sa langue d'origine."""
        preset = FIELD_PRESETS.get(key)
        definition = targets.defs_by_key.get(key)
        label_i18n = dict(preset.labels) if preset else {}
        if definition is not None:
            label_i18n.update(definition.label_i18n or {})
        return ImportTargetSpec(
            key=key,
            label=definition.label if definition else label_i18n.get("fr", key),
            label_i18n=label_i18n,
            field_type=(
                definition.field_type if definition else (preset.field_type if preset else "text")
            ),
            section=((definition.profile_section or "misc") if definition else fallback_section),
            will_create=definition is None,
            agency_named=preset is None,
        )

    # 3. Les presets du catalogue, dans l'ordre du catalogue. Déclarés,
    #    ils portent la section de LEUR définition ; non déclarés, celle
    #    de la table de migration — et la mention « sera ajouté ».
    for key, preset_section in PRESET_PROFILE_SECTION.items():
        if key not in targets.preset_keys and key not in targets.defs_by_key:
            continue
        emit(field_spec(key, preset_section))

    # 4. Les clés DÉCLARÉES restantes — celles que l'agence a inventées,
    #    et les presets hors plan de fiche : même arbitre, autre porte.
    for key, definition in sorted(
        targets.defs_by_key.items(), key=lambda kv: (kv[1].position, kv[0])
    ):
        emit(field_spec(key, definition.profile_section or "misc"))

    # 5. Les cibles STRUCTURELLES : la langue de contact (colonne native)
    #    et les étiquettes (ni champ ni preset — un déversoir).
    emit(
        ImportTargetSpec(
            key="preferred_lang",
            label=CIVIL_LABELS["preferred_lang"]["fr"],
            label_i18n=dict(CIVIL_LABELS["preferred_lang"]),
            field_type=CIVIL_FIELD_TYPES["preferred_lang"],
            section="contact",
        )
    )
    emit(
        ImportTargetSpec(
            key="tags",
            label=TAGS_LABEL["fr"],
            label_i18n=dict(TAGS_LABEL),
            field_type="tags",
            section="misc",
        )
    )
    emit(
        ImportTargetSpec(
            key="status_override",
            label=STATUS_LABEL["fr"],
            label_i18n=dict(STATUS_LABEL),
            # `select` : la colonne n'accepte que deux valeurs, et la
            # grille doit le montrer comme tel — pas comme du texte libre.
            field_type="select",
            section="misc",
        )
    )

    sections = [
        ImportSectionSpec(key=key, label_i18n=dict(labels))
        for key, labels in PROFILE_SECTIONS.items()
    ]
    return sections, _flatten(buckets, PROFILE_SECTIONS)


async def company_target_catalog(
    db: AsyncSession, agency_id: uuid.UUID
) -> tuple[list[ImportSectionSpec], list[ImportTargetSpec]]:
    """L'univers SOCIÉTÉ, habillé pour le combobox — même taxonomie que
    la fiche personne (la parité est revenue), le plan de valeurs
    company dans son ordre, puis les clés BAPTISÉES de l'agence.

    Deux différences assumées avec la face personne :
    - `will_create` est toujours faux : une valeur société tombe dans le
      sac JSONB, il n'y a pas de définition à créer (une clé baptisée
      naît d'un geste explicite dans la grille, pas d'un import) ;
    - les ALIAS de compat (`COMPANY_TARGET_ALIASES`) restent ACCEPTÉS
      sans être OFFERTS : offrir `registration_number` à côté de
      `company_registration_number`, ce serait deux options pour un seul
      champ. Le test grave l'écart pour qu'il ne grandisse pas en douce.
    """
    from src.client_profiles.profile_sections import (
        COMPANY_PRESET_PROFILE_SECTION,
        COMPANY_PROFILE_SECTIONS,
    )
    from src.company_profiles.company_catalog import company_preset_spec
    from src.imports.target_labels import COMPANY_NAME_LABEL, TAGS_LABEL

    targets = await company_targets(db, agency_id)
    buckets: dict[str, list[ImportTargetSpec]] = {key: [] for key in COMPANY_PROFILE_SECTIONS}
    seen: set[str] = set()

    def emit(spec: ImportTargetSpec) -> None:
        if spec.key in seen:
            return
        seen.add(spec.key)
        buckets[spec.section if spec.section in buckets else "misc"].append(spec)

    # 1. La dénomination : la clé de dédup, sans elle rien ne part.
    emit(
        ImportTargetSpec(
            key="name",
            label=COMPANY_NAME_LABEL["fr"],
            label_i18n=dict(COMPANY_NAME_LABEL),
            field_type="text",
            section="identity",
            required=True,
        )
    )

    # 2. Le plan de valeurs company, dans son ordre de déclaration.
    for key in COMPANY_PRESET_PROFILE_SECTION:
        # La résolution (type, section, libellé ×7) vit dans le catalogue
        # société — elle était recopiée ici, elle y est désormais LUE.
        field_type, section, label_i18n = company_preset_spec(key)
        emit(
            ImportTargetSpec(
                key=key,
                label=label_i18n.get("fr", key),
                label_i18n=dict(label_i18n),
                field_type=field_type,
                section=section,
            )
        )

    # 3. Les étiquettes, puis les clés BAPTISÉES par l'agence dans la
    #    grille : leur libellé et leur kind de naissance vivent en table.
    emit(
        ImportTargetSpec(
            key="tags",
            label=TAGS_LABEL["fr"],
            label_i18n=dict(TAGS_LABEL),
            field_type="tags",
            section="misc",
        )
    )
    for key, row in sorted(targets.labels_by_key.items()):
        emit(
            ImportTargetSpec(
                key=key,
                label=row.label,
                label_i18n={},
                field_type=row.field_type,
                section="misc",
                agency_named=True,
            )
        )

    sections = [
        ImportSectionSpec(key=key, label_i18n=dict(labels))
        for key, labels in COMPANY_PROFILE_SECTIONS.items()
    ]
    return sections, _flatten(buckets, COMPANY_PROFILE_SECTIONS)
