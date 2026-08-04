"""L'UNIVERS DES CIBLES de l'import FICHES — une seule source.

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

Note de grammaire : l'import PARCOURS (`case_import`) parle une autre
langue (`custom_field:<clé>` contre les fiches déclarées d'un parcours)
et a déjà SA validation partagée (`mapping_validation`) — deux univers
distincts, assumés, qui ne se mélangent pas.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.company_profile import CompanyFieldLabel
from shared.models.custom_field import CustomFieldDefinition

# L'identité de la fiche personne : les 3 colonnes du contrat person.
IDENTITY_TARGETS: tuple[str, ...] = ("first_name", "last_name", "email")
# Cibles STRUCTURELLES personne : `tags` (split ,/; dédupliqué) et la
# COLONNE langue (le preset preferred_language est mort au dédoublonnage).
PERSON_STRUCTURAL_TARGETS: frozenset[str] = frozenset({"tags", "preferred_lang"})
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
    labels_by_key: dict[str, CompanyFieldLabel] = field(default_factory=dict)
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

    label_rows = await CompanyProfilesRepository(db).field_labels(agency_id)
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
