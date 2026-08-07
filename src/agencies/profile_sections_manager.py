"""LES SECTIONS DE FICHE D'UNE AGENCE — lecture, gestes, invariants.

Elles étaient 4, en dur, identiques pour tout le monde
(`PROFILE_SECTIONS`). Elles sont désormais une donnée d'agence, par
surface. Ce module est le SEUL endroit qui sait :

1. **Le repli catalogue.** `label_i18n` vide = « l'agence n'a pas
   renommé » → le libellé vient de `PROFILE_SECTIONS`, qui porte les 7
   langues et SUIT les corrections de traduction du produit. Graver les
   libellés à la migration les aurait figés pour toujours.
2. **Les deux indéracinables.** « Divers » (`misc`) ne se supprime ni ne
   se renomme : c'est le berceau des champs sans rangement, et la cible
   de repli de tout ce qui tombe. Une section qui PORTE des champs
   (définitions ou colonnes civiles) ne se supprime pas non plus — sinon
   ses champs deviendraient invisibles sans que personne ne l'ait
   demandé.
3. **La clé est immuable.** `custom_field_definition.profile_section` et
   `company_field_definition.profile_section` la portent : la renommer
   ferait tomber tous ses champs en « Divers » d'un coup. Renommer touche
   le LIBELLÉ, jamais la clé.
"""

import re
import uuid
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import AgencyProfileSection
from shared.models.company_profile import CompanyFieldDefinition
from shared.models.custom_field import CustomFieldDefinition
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.core.i18n import resolve_i18n

# La section indéracinable, des deux côtés.
MISC: Final[str] = "misc"
SURFACES: Final[tuple[str, ...]] = ("person", "company")
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


def catalog_label_i18n(key: str) -> dict[str, str]:
    """Le libellé ×7 du catalogue pour une clé d'origine, `{}` sinon."""
    from src.client_profiles.profile_sections import PROFILE_SECTIONS

    return dict(PROFILE_SECTIONS.get(key, {}))


def section_name(section: AgencyProfileSection, lang: str, agency_default: str) -> str:
    """LE LIBELLÉ SERVI : celui de l'agence si elle a renommé, celui du
    catalogue sinon, la clé en dernier recours (une section créée par
    l'agence a toujours un libellé — le dernier recours ne sert que si
    une donnée arrive vide, et il vaut mieux une clé qu'une chaîne vide)."""
    blob = section.label_i18n or catalog_label_i18n(section.key)
    return resolve_i18n(blob, lang, agency_default, section.key) or section.key


async def list_sections(
    db: AsyncSession, agency_id: uuid.UUID, surface: str
) -> list[AgencyProfileSection]:
    """Les sections d'une surface, DANS L'ORDRE DE L'ÉCRAN.

    MATÉRIALISATION PARESSEUSE, le pattern déjà en place pour les champs :
    une agence née après la migration (ou dont la surface n'a jamais été
    ouverte) reçoit ses 4 sections d'origine ici. Sans ça, la création
    d'agence devrait connaître la taxonomie — une deuxième vérité.
    """
    stmt = (
        select(AgencyProfileSection)
        .where(
            AgencyProfileSection.agency_id == agency_id,
            AgencyProfileSection.surface == surface,
        )
        .order_by(AgencyProfileSection.position, AgencyProfileSection.key)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if rows:
        return rows
    from src.client_profiles.profile_sections import PROFILE_SECTIONS

    born = [
        AgencyProfileSection(
            agency_id=agency_id,
            surface=surface,
            key=key,
            label_i18n={},  # repli catalogue — voir le module
            position=position,
        )
        for position, key in enumerate(PROFILE_SECTIONS)
    ]
    db.add_all(born)
    await db.flush()
    return born


async def section_keys(db: AsyncSession, agency_id: uuid.UUID, surface: str) -> list[str]:
    return [s.key for s in await list_sections(db, agency_id, surface)]


async def assert_section_exists(
    db: AsyncSession, agency_id: uuid.UUID, surface: str, key: str
) -> None:
    """LA VALIDATION PAR TENANT qui remplace les deux `Literal` figés :
    une section créée par l'agence ne peut plus être refusée en 422 par
    son propre back, et une clé inventée reste refusée — nommément."""
    keys = await section_keys(db, agency_id, surface)
    if key not in keys:
        raise ValidationError(
            f"Unknown profile section {key!r} for this agency.",
            code="profile_section.unknown",
            params={"section": key, "surface": surface, "available": keys},
        )


async def _field_counts(db: AsyncSession, agency_id: uuid.UUID, surface: str, key: str) -> int:
    """Combien de CHAMPS DÉCLARÉS cette section porte — archivés compris :
    un champ archivé qu'on ressuscite doit retrouver sa section."""
    model: type[CustomFieldDefinition] | type[CompanyFieldDefinition] = (
        CustomFieldDefinition if surface == "person" else CompanyFieldDefinition
    )
    stmt = (
        select(func.count())
        .select_from(model)
        .where(model.agency_id == agency_id, model.profile_section == key)
    )
    if model is CustomFieldDefinition:
        # La face personne partage sa table avec les champs de dossier :
        # seuls les champs de FICHE occupent une section de fiche.
        stmt = stmt.where(CustomFieldDefinition.scope == "person")
    return int((await db.execute(stmt)).scalar_one())


def _civil_count(surface: str, key: str) -> int:
    """Les COLONNES CIVILES que la section porte (face personne seule).

    Elles ne sont pas déplaçables (leur section vit dans
    `CIVIL_PROFILE_SECTION`, en dur — backlog D5) : les oublier
    autoriserait à supprimer « Identité » alors que 6 colonnes natives y
    vivent, et elles disparaîtraient de la fiche sans recours."""
    if surface != "person":
        return 0
    from src.client_profiles.profile_sections import CIVIL_PROFILE_SECTION

    return sum(1 for section in CIVIL_PROFILE_SECTION.values() if section == key)


async def create_section(
    db: AsyncSession,
    agency_id: uuid.UUID,
    *,
    surface: str,
    key: str,
    label_i18n: dict[str, str],
    position: int | None,
) -> AgencyProfileSection:
    if surface not in SURFACES:
        raise ValidationError(
            f"Unknown surface {surface!r}.",
            code="profile_section.unknown_surface",
            params={"surface": surface, "available": list(SURFACES)},
        )
    if not _KEY_PATTERN.match(key):
        raise ValidationError(
            "A section key is a slug: lowercase letters, digits and underscores.",
            code="profile_section.invalid_key",
            params={"key": key},
        )
    existing = await list_sections(db, agency_id, surface)
    if any(s.key == key for s in existing):
        raise ConflictError(
            f"A section with key {key!r} already exists on this surface.",
            code="profile_section.key_taken",
            params={"key": key, "surface": surface},
        )
    if not label_i18n:
        raise ValidationError(
            "A new section needs a label.",
            code="profile_section.label_required",
            params={"key": key},
        )
    section = AgencyProfileSection(
        agency_id=agency_id,
        surface=surface,
        key=key,
        label_i18n=dict(label_i18n),
        position=position if position is not None else len(existing),
    )
    db.add(section)
    await db.flush()
    return section


async def _get(
    db: AsyncSession, agency_id: uuid.UUID, section_id: uuid.UUID
) -> AgencyProfileSection:
    stmt = select(AgencyProfileSection).where(
        AgencyProfileSection.agency_id == agency_id,
        AgencyProfileSection.id == section_id,
    )
    section = (await db.execute(stmt)).scalar_one_or_none()
    if section is None:
        raise NotFoundError("Profile section not found.", code="profile_section.not_found")
    return section


async def rename_section(
    db: AsyncSession,
    agency_id: uuid.UUID,
    section_id: uuid.UUID,
    *,
    label_i18n: dict[str, str] | None,
    position: int | None,
) -> AgencyProfileSection:
    """Renommer et/ou déplacer. La CLÉ ne bouge pas — voir le module.

    `label_i18n = {}` REND le libellé du catalogue (le repli reprend la
    main) : c'est la façon d'annuler un renommage sans supprimer la
    section, donc sans toucher aux champs qu'elle porte."""
    section = await _get(db, agency_id, section_id)
    if label_i18n is not None:
        if section.key == MISC and label_i18n:
            raise ValidationError(
                "The catch-all section cannot be renamed.",
                code="profile_section.misc_immutable",
                params={"key": MISC},
            )
        section.label_i18n = dict(label_i18n)
    if position is not None:
        section.position = position
    return section


async def delete_section(db: AsyncSession, agency_id: uuid.UUID, section_id: uuid.UUID) -> None:
    """LA SUPPRESSION EST UN REFUS PAR DÉFAUT : une section qui porte
    quelque chose ne part pas, et le message dit quoi déplacer d'abord.

    Conséquence assumée, nommée au constat : « Identité », « Coordonnées »
    et « Situation » portent des colonnes civiles NON déplaçables (D5) —
    elles se renomment donc, mais ne se suppriment pas. Une agence ne
    repart pas d'une page blanche ; elle crée les siennes à côté."""
    section = await _get(db, agency_id, section_id)
    if section.key == MISC:
        raise ValidationError(
            "The catch-all section cannot be deleted — it is where unsorted fields land.",
            code="profile_section.misc_immutable",
            params={"key": MISC},
        )
    fields = await _field_counts(db, agency_id, section.surface, section.key)
    civils = _civil_count(section.surface, section.key)
    if fields or civils:
        raise ValidationError(
            "This section still holds fields — move them first.",
            code="profile_section.not_empty",
            params={
                "key": section.key,
                "fields": fields,
                "native_columns": civils,
                "surface": section.surface,
            },
        )
    await db.delete(section)
