"""L'UNIVERS AFFICHÉ d'un écran, pas le stockage.

Le constat du 07/08 : l'écran « Champs personnalisés » listait des
DÉFINITIONS alors que l'agence veut voir ce que ses écrans MONTRENT. Les
trois surfaces ne sont pas symétriques, et c'est là tout le problème :

- FICHE PERSONNE : 10 colonnes civiles natives + les définitions
  `scope='person'`. Tout ce qui s'affiche est déclaré, mais les natives
  n'ont aucune définition et ne s'archivent jamais.
- FICHE SOCIÉTÉ : ZÉRO définition. 17 presets en dur dans le code, servis
  à toutes les agences, plus les clés découvertes dans les sacks. Rien à
  archiver ; seul le LIBELLÉ se personnalise (`company_field_label`).
- FICHE DOSSIER : les champs des sections de parcours, agrégés TOUS
  PARCOURS CONFONDUS — un champ sert couramment 90 parcours, il ne doit
  apparaître qu'une fois, avec son compte.

Ce module rend les trois dans UN contrat, chaque champ portant son ÉTAT :
l'écran n'a plus à deviner ce qu'il peut faire d'une ligne.
"""

import uuid
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from src.custom_fields.custom_fields_schema import (
    FieldUniverseEntry,
    FieldUniverseResponse,
    FieldUniverseSection,
)

Surface = Literal["person", "company", "case"]

# Les clés du sack société, découvertes en UNE passe sur les sociétés de
# l'agence (même motif que le compte de valeurs des gestes de masse) :
# une clé n'existe que si une valeur a été saisie quelque part.
_SACK_KEYS = (
    "SELECT DISTINCT k FROM company_profile c,"
    " LATERAL jsonb_object_keys(c.custom_fields) AS k"
    " WHERE c.agency_id = :agency_id"
)

# Les champs de parcours, AGRÉGÉS : un GROUP BY, jamais une requête par
# parcours (témoin de coût — l'agence qui a 94 parcours paie le même prix
# que celle qui en a deux).
_JOURNEY_FIELDS = (
    "SELECT f.reference AS reference, f.kind AS kind,"
    " count(DISTINCT f.template_id) AS n"
    " FROM journey_template_field f"
    " JOIN journey_template t ON t.id = f.template_id"
    " WHERE t.agency_id = :agency_id"
    " GROUP BY f.reference, f.kind"
)


def _label(key: str, lang: str, agency_default: str) -> str:
    """Le libellé d'une clé NON déclarée : le catalogue d'abord (il porte
    les 7 langues), puis les libellés d'import société, puis la clé —
    jamais une chaîne inventée."""
    from src.imports.target_labels import CIVIL_LABELS, COMPANY_EXTRA_LABELS, IDENTITY_LABELS
    from src.journeys.field_catalog import FIELD_PRESETS

    preset = FIELD_PRESETS.get(key)
    if preset is not None:
        return preset.labels.get(lang) or preset.labels.get(agency_default) or preset.labels["fr"]
    for table in (CIVIL_LABELS, IDENTITY_LABELS, COMPANY_EXTRA_LABELS):
        blob = table.get(key)
        if blob:
            return blob.get(lang) or blob.get(agency_default) or blob["fr"]
    return key


async def field_universe(
    db: AsyncSession, agent: Agent, surface: Surface, lang: str
) -> FieldUniverseResponse:
    from src.client_profiles.profile_sections import (
        CIVIL_PROFILE_SECTION,
        COMPANY_PRESET_PROFILE_SECTION,
        COMPANY_PROFILE_SECTIONS,
        IDENTITY_SECTION_ORDER,
        PERSON_SHEET_EXCLUDED_KEYS,
        PRESET_PROFILE_SECTION,
        PROFILE_SECTIONS,
    )
    from src.custom_fields.custom_fields_manager import CustomFieldsManager
    from src.custom_fields.custom_fields_repository import CustomFieldsRepository
    from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS

    agency_id = agent.agency_id
    agency_default = await CustomFieldsManager(db).agency_default(agency_id)
    definitions = await CustomFieldsRepository(db).list_for_agency(agency_id)
    active = {d.key: d for d in definitions if d.archived_at is None}

    def declared(key: str, section: str, definition: CustomFieldDefinition) -> FieldUniverseEntry:
        return FieldUniverseEntry(
            reference=key,
            label=definition.label,
            field_type=definition.field_type,
            section=section,
            state="declared",
            definition_id=definition.id,
            required=definition.required,
        )

    buckets: dict[str, list[FieldUniverseEntry]] = {}
    sections: dict[str, dict[str, str]]

    if surface == "person":
        sections = dict(PROFILE_SECTIONS)
        buckets = {key: [] for key in sections}
        # 1. Les colonnes civiles : elles s'affichent, elles ne s'archivent
        #    jamais — l'écran doit le savoir, pas le supposer.
        for key in sorted(COLLECTABLE_BASE_FIELDS):
            buckets[CIVIL_PROFILE_SECTION[key]].append(
                FieldUniverseEntry(
                    reference=key,
                    label=_label(key, lang, agency_default),
                    field_type=None,
                    section=CIVIL_PROFILE_SECTION[key],
                    state="native",
                )
            )
        # 2. Les définitions de l'agence. L'UNIVERS SOCIÉTÉ EST ÉCARTÉ :
        #    cet écran est le miroir de la FICHE, pas du stockage — les 8
        #    clés société n'y figurent pas (elles vivent sur la surface
        #    `company`, où elles sont toutes des presets ; rien ne devient
        #    invisible, c'est vérifié par test).
        for key, definition in active.items():
            if definition.scope != "person" or key in PERSON_SHEET_EXCLUDED_KEYS:
                continue
            section = definition.profile_section or "misc"
            buckets.setdefault(section, buckets["misc"]).append(declared(key, section, definition))
        # 3. Le catalogue que l'agence n'a PAS déclaré : l'écran peut le
        #    proposer (« ajouter à mon univers ») au lieu de le taire.
        for key, section in PRESET_PROFILE_SECTION.items():
            if key in active or key in PERSON_SHEET_EXCLUDED_KEYS:
                continue
            buckets[section].append(
                FieldUniverseEntry(
                    reference=key,
                    label=_label(key, lang, agency_default),
                    field_type=None,
                    section=section,
                    state="catalog_undeclared",
                )
            )
        rank = {key: index for index, key in enumerate(IDENTITY_SECTION_ORDER)}
        buckets["identity"].sort(key=lambda e: rank.get(e.reference, len(rank)))

    elif surface == "company":
        sections = dict(COMPANY_PROFILE_SECTIONS)
        buckets = {key: [] for key in sections}
        overrides = {row.key: row.label for row in await _company_labels(db, agency_id)}
        # 1. Les 17 presets, servis à TOUTE agence par le code : aucun
        #    n'est archivable ni typable, seul son libellé se personnalise.
        for key, section in COMPANY_PRESET_PROFILE_SECTION.items():
            buckets[section].append(
                FieldUniverseEntry(
                    reference=key,
                    label=overrides.get(key) or _label(key, lang, agency_default),
                    field_type=None,
                    section=section,
                    state="native",
                    renamable=True,
                )
            )
        # 2. Les clés trouvées dans les sacks : elles n'existent que parce
        #    qu'une valeur a été saisie. Renommables, jamais archivables.
        rows = await db.execute(text(_SACK_KEYS), {"agency_id": agency_id})
        for (key,) in rows:
            if key in COMPANY_PRESET_PROFILE_SECTION:
                continue
            buckets["misc"].append(
                FieldUniverseEntry(
                    reference=key,
                    label=overrides.get(key) or _label(key, lang, agency_default),
                    field_type=None,
                    section="misc",
                    state="sack_only",
                    renamable=True,
                )
            )
        buckets["misc"].sort(key=lambda e: e.label.lower())

    else:  # case
        sections = dict(PROFILE_SECTIONS)
        buckets = {key: [] for key in sections}
        rows = await db.execute(text(_JOURNEY_FIELDS), {"agency_id": agency_id})
        for reference, kind, n in rows:
            # La section : celle de la définition quand elle existe, celle
            # du plan civil pour un champ de base, « divers » sinon. Les
            # sections de PARCOURS ne peuvent pas servir ici — le même
            # champ vit dans 94 parcours, donc dans 94 sections
            # différentes : il n'y a pas de vérité à agréger. On garde la
            # taxonomie commune aux trois surfaces.
            declared_here = active.get(reference)
            if kind == "base_field":
                section = CIVIL_PROFILE_SECTION.get(reference, "misc")
                entry = FieldUniverseEntry(
                    reference=reference,
                    label=_label(reference, lang, agency_default),
                    field_type=None,
                    section=section,
                    state="native",
                    used_in_journeys=int(n),
                )
            elif declared_here is not None:
                section = declared_here.profile_section or "misc"
                entry = declared(reference, section, declared_here)
                entry.used_in_journeys = int(n)
            else:
                # Référencé par un parcours SANS définition : l'éditeur de
                # parcours le montre déjà drapeau `is_archived`. Ici il est
                # « du catalogue, pas déclaré » s'il en vient, sinon il est
                # orphelin — dans les deux cas, non éditable.
                section = PRESET_PROFILE_SECTION.get(reference, "misc")
                entry = FieldUniverseEntry(
                    reference=reference,
                    label=_label(reference, lang, agency_default),
                    field_type=None,
                    section=section,
                    state="catalog_undeclared",
                    used_in_journeys=int(n),
                )
            buckets[entry.section].append(entry)
        for key in buckets:
            buckets[key].sort(key=lambda e: (-(e.used_in_journeys or 0), e.label.lower()))

    return FieldUniverseResponse(
        surface=surface,
        sections=[
            FieldUniverseSection(
                key=section_key,
                name=labels.get(lang) or labels["fr"],
                fields=buckets.get(section_key, []),
            )
            for section_key, labels in sections.items()
        ],
    )


async def _company_labels(db: AsyncSession, agency_id: uuid.UUID) -> Any:
    from shared.models.company_profile import CompanyFieldLabel

    stmt = select(CompanyFieldLabel).where(CompanyFieldLabel.agency_id == agency_id)
    return (await db.execute(stmt)).scalars().all()


__all__ = ["Surface", "field_universe"]
