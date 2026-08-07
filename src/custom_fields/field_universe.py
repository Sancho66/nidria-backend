"""L'UNIVERS AFFICHÉ d'un écran, pas le stockage.

Le constat du 07/08 : l'écran « Champs personnalisés » listait des
DÉFINITIONS alors que l'agence veut voir ce que ses écrans MONTRENT. Les
trois surfaces ne sont pas symétriques, et c'est là tout le problème :

- FICHE PERSONNE : 10 colonnes civiles natives + les définitions
  `scope='person'`. Tout ce qui s'affiche est déclaré, mais les natives
  n'ont aucune définition et ne s'archivent jamais.
- FICHE SOCIÉTÉ : les définitions `company_field_definition`, dans leur
  propre espace de clés. Les 17 presets et les clés trouvées dans les
  sacks s'y MATÉRIALISENT à la première ouverture de l'écran (lot du
  07/08) : tout ce qui s'y affiche est déclaré, donc archivable,
  typable, déplaçable — comme la face personne.
- FICHE DOSSIER : les champs des sections de parcours, agrégés TOUS
  PARCOURS CONFONDUS — un champ sert couramment 90 parcours, il ne doit
  apparaître qu'une fois, avec son compte.

Ce module rend les trois dans UN contrat, chaque champ portant son ÉTAT :
l'écran n'a plus à deviner ce qu'il peut faire d'une ligne.
"""

from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from src.agencies.profile_sections_manager import catalog_label_i18n, list_sections
from src.core.i18n import resolve_i18n
from src.custom_fields.custom_fields_schema import (
    FieldUniverseEntry,
    FieldUniverseResponse,
    FieldUniverseSection,
)

Surface = Literal["person", "company", "case"]

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


def _preset_type(key: str) -> str | None:
    """Le TYPE d'une clé du catalogue — servi même quand l'agence ne l'a
    pas déclarée. Sans lui, l'écran devrait consulter sa propre copie du
    catalogue pour savoir s'il peut proposer l'ajout : une déduction de
    plus, et c'est ce motif-là qui a produit trois désalignements cette
    semaine. Nul pour une clé hors catalogue (elle n'a pas encore de
    type — c'est la création qui le pose)."""
    from src.journeys.field_catalog import FIELD_PRESETS

    preset = FIELD_PRESETS.get(key)
    return preset.field_type if preset is not None else None


def _origin(key: str) -> str:
    """`catalog` = le PRODUIT connaît cette clé (preset du catalogue,
    colonne civile, preset société) ; `agency` = elle a été écrite par
    l'agence. Le front le déduisait de sa propre table de clés — même
    motif, même risque de dérive : la vérité vient d'ici.

    Sur la surface `case`, c'est ce qui distingue une référence
    ORPHELINE (`catalog_undeclared` + `agency` : une définition archivée
    ou supprimée qu'un parcours cite encore) d'un preset réellement
    proposable (`catalog_undeclared` + `catalog`)."""
    from src.client_profiles.profile_sections import COMPANY_PRESET_PROFILE_SECTION
    from src.imports.target_labels import CIVIL_LABELS, IDENTITY_LABELS
    from src.journeys.field_catalog import FIELD_PRESETS

    known = key in FIELD_PRESETS or key in COMPANY_PRESET_PROFILE_SECTION
    return "catalog" if known or key in CIVIL_LABELS or key in IDENTITY_LABELS else "agency"


async def field_universe(
    db: AsyncSession, agent: Agent, surface: Surface, lang: str
) -> FieldUniverseResponse:
    from src.client_profiles.profile_sections import (
        CIVIL_PROFILE_SECTION,
        COMPANY_PRESET_PROFILE_SECTION,
        IDENTITY_SECTION_ORDER,
        PERSON_SHEET_EXCLUDED_KEYS,
        PRESET_PROFILE_SECTION,
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
            position=definition.position,
            origin=_origin(key),
        )

    buckets: dict[str, list[FieldUniverseEntry]] = {}
    sections: dict[str, dict[str, str]]

    if surface == "person":
        # LES SECTIONS DE L'AGENCE (lot du 07/08), plus la table figée :
        # l'écran des Réglages est le miroir de la FICHE, il doit donc
        # montrer les mêmes sections qu'elle — y compris celles que
        # l'agence a créées, et dans son ordre.
        agency_sections = await list_sections(db, agency_id, "person")
        await db.commit()
        sections = {s.key: (s.label_i18n or catalog_label_i18n(s.key)) for s in agency_sections}
        buckets = {key: [] for key in sections}
        buckets.setdefault("misc", [])
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
                    origin=_origin(key),
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
                    field_type=_preset_type(key),
                    section=section,
                    state="catalog_undeclared",
                    origin=_origin(key),
                )
            )
        rank = {key: index for index, key in enumerate(IDENTITY_SECTION_ORDER)}
        buckets["identity"].sort(key=lambda e: rank.get(e.reference, len(rank)))

    elif surface == "company":
        # LA SURFACE SOCIÉTÉ EST DÉSORMAIS DÉCLARÉE (lot du 07/08).
        #
        # Elle servait 17 presets `native` et des clés `sack_only` : rien
        # à archiver, rien à typer, rien à ranger — l'écran coupait donc
        # ses gestes en dur, et il avait raison. Ouvrir cet écran
        # MATÉRIALISE l'univers dans `company_field_definition` : chaque
        # entrée porte son `definition_id`, et les capacités deviennent
        # vraies au lieu d'être annoncées.
        from src.company_profiles.company_catalog import (
            company_sack_keys,
            materialize_company_definitions,
        )

        agency_sections = await list_sections(db, agency_id, "company")
        sections = {s.key: (s.label_i18n or catalog_label_i18n(s.key)) for s in agency_sections}
        buckets = {key: [] for key in sections}
        buckets.setdefault("misc", [])
        # Le balayage des sacks : c'est l'écran qui le paie, et il vaut
        # pour toute l'agence (la fiche, elle, ne déclare que ses propres
        # clés). La passe vit à côté de la matérialisation qui la
        # consomme — la création de champ société la réutilise pour sa
        # détection de collision.
        company_definitions = await materialize_company_definitions(
            db, agency_id, sack_keys=await company_sack_keys(db, agency_id)
        )
        await db.commit()
        presets = set(COMPANY_PRESET_PROFILE_SECTION)
        for company_definition in sorted(company_definitions, key=lambda d: (d.position, d.key)):
            section = company_definition.profile_section
            buckets.setdefault(section if section in buckets else "misc", []).append(
                FieldUniverseEntry(
                    reference=company_definition.key,
                    label=resolve_i18n(
                        company_definition.label_i18n,
                        lang,
                        agency_default,
                        company_definition.label,
                    )
                    or company_definition.label,
                    field_type=company_definition.field_type,
                    section=section if section in buckets else "misc",
                    state="declared",
                    definition_id=company_definition.id,
                    position=company_definition.position,
                    required=False,
                    # Le renommage reste offert des DEUX voies : par la
                    # clé (le geste que le front tient déjà) comme par le
                    # PATCH de définition. Le retirer casserait l'écran
                    # pour ne rien gagner.
                    renamable=True,
                    origin="catalog" if company_definition.key in presets else "agency",
                )
            )

    else:  # case
        # La surface DOSSIER garde la taxonomie commune servie par
        # l'agence côté personne : les champs de mission n'ont pas leur
        # propre plan de sections (deux univers assumés depuis le lot
        # taxonomie), mais ils ne doivent pas afficher d'autres noms que
        # la fiche pour les mêmes clés.
        agency_sections = await list_sections(db, agency_id, "person")
        await db.commit()
        sections = {s.key: (s.label_i18n or catalog_label_i18n(s.key)) for s in agency_sections}
        buckets = {key: [] for key in sections}
        buckets.setdefault("misc", [])
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
                    origin=_origin(reference),
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
                    field_type=_preset_type(reference),
                    section=section,
                    state="catalog_undeclared",
                    used_in_journeys=int(n),
                    origin=_origin(reference),
                )
            buckets[entry.section].append(entry)
        for key in buckets:
            buckets[key].sort(key=lambda e: (-(e.used_in_journeys or 0), e.label.lower()))

    return FieldUniverseResponse(
        surface=surface,
        sections=[
            FieldUniverseSection(
                key=section_key,
                name=resolve_i18n(labels, lang, agency_default, section_key) or section_key,
                fields=buckets.get(section_key, []),
            )
            for section_key, labels in sections.items()
        ],
    )


__all__ = ["Surface", "field_universe"]
