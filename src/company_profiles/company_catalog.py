"""LE CATALOGUE SOCIÉTÉ et sa MATÉRIALISATION PARESSEUSE.

Deux choses, indissociables, écrites ici une seule fois :

1. LE CATALOGUE — les 17 presets société avec leur type, leur section et
   leur libellé ×7. Ces trois vérités existaient déjà, mais éparpillées :
   la section dans `COMPANY_PRESET_PROFILE_SECTION`, le type entre
   `COMPANY_EXTRA_FIELD_TYPES` et `FIELD_PRESETS`, le libellé entre
   `COMPANY_EXTRA_LABELS` et `FIELD_PRESETS` — et la résolution des trois
   était recopiée dans `import_targets.company_target_universe`. Une
   seule fonction les résout désormais, et l'import l'appelle.

2. LA MATÉRIALISATION — le pattern de la face personne
   (`materialize_preset_definitions`) : à la première ouverture d'un
   écran société, les presets manquants DEVIENNENT des définitions de
   l'agence. À partir de là, la fiche lit des définitions et plus un plan
   figé : archiver, renommer, reclasser, ranger deviennent vrais.

LE TYPE D'UNE CLÉ DE SACK NE SE DEVINE JAMAIS DE SA VALEUR. Il vient de
la définition quand elle existe (l'intention déclarée à la grille
d'import), `text` sinon. Deviner ferait de « 1234 » un `number`, et la
coercition du prochain import échouerait sur « 01234 » — la règle
« suggérable = coerçable » y passerait.
"""

import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.company_profile import CompanyFieldDefinition

if TYPE_CHECKING:
    from src.custom_fields.custom_fields_schema import CustomFieldDefinitionResponse


def company_preset_spec(key: str) -> tuple[str, str, dict[str, str]]:
    """(field_type, profile_section, label_i18n) d'un preset société.

    L'ordre de résolution est celui qui existait dans
    `company_target_universe` : les tables société d'abord (elles
    surchargent volontairement le catalogue — `country` y est de type
    `country`, pas `text`), le catalogue ensuite, `text` en dernier."""
    from src.client_profiles.profile_sections import COMPANY_PRESET_PROFILE_SECTION
    from src.imports.target_labels import COMPANY_EXTRA_FIELD_TYPES, COMPANY_EXTRA_LABELS
    from src.journeys.field_catalog import FIELD_PRESETS

    preset = FIELD_PRESETS.get(key)
    labels = COMPANY_EXTRA_LABELS.get(key) or (dict(preset.labels) if preset else {})
    field_type = COMPANY_EXTRA_FIELD_TYPES.get(key) or (preset.field_type if preset else "text")
    section = COMPANY_PRESET_PROFILE_SECTION.get(key, "misc")
    return field_type, section, dict(labels)


def company_preset_keys() -> tuple[str, ...]:
    """Les 17, DANS L'ORDRE DE DÉCLARATION — c'est lui qui devient la
    position de naissance, donc l'ordre de la fiche tant que l'agence n'a
    rien rangé."""
    from src.client_profiles.profile_sections import COMPANY_PRESET_PROFILE_SECTION

    return tuple(COMPANY_PRESET_PROFILE_SECTION)


def humanize(key: str) -> str:
    """Le libellé de naissance d'une clé de sack : la clé, lisible. Ce
    n'est pas une invention — c'est exactement ce que l'écran affichait
    déjà pour ces clés (`ref.replace(/_/g, " ")`), désormais posé une
    fois en base où l'agence peut le corriger."""
    return key.replace("_", " ").strip().capitalize() or key


# Le libellé ×7 des clés hors catalogue reste vide : servir la même
# chaîne dans 7 langues serait mentir sur une traduction qui n'existe pas
# (l'agence a nommé son champ dans SA langue).
_NO_I18N: Final[dict[str, str]] = {}

# Les clés du sack société, découvertes en UNE passe sur les sociétés de
# l'agence : une clé n'existe que si une valeur a été saisie quelque part.
_SACK_KEYS: Final[str] = (
    "SELECT DISTINCT k FROM company_profile c,"
    " LATERAL jsonb_object_keys(c.custom_fields) AS k"
    " WHERE c.agency_id = :agency_id"
)


async def company_sack_keys(db: AsyncSession, agency_id: uuid.UUID) -> frozenset[str]:
    """Le balayage des sacs de TOUTE l'agence — posé ici, à côté de la
    matérialisation qui le consomme, plutôt que recopié chez chaque
    appelant (l'univers des Réglages le payait déjà ; la création de
    champ le paie pour que sa détection de collision soit exhaustive :
    une clé de sack non encore matérialisée est une clé prise)."""
    rows = await db.execute(text(_SACK_KEYS), {"agency_id": agency_id})
    return frozenset(key for (key,) in rows)


async def materialize_company_definitions(
    db: AsyncSession,
    agency_id: uuid.UUID,
    *,
    sack_keys: frozenset[str] = frozenset(),
) -> list[CompanyFieldDefinition]:
    """Rend TOUTES les définitions société de l'agence, en créant les
    manquantes au passage. Idempotent, sans commit (l'appelant décide) —
    la relecture d'un écran déjà ouvert n'écrit rien.

    Les archivées sont rendues elles aussi : c'est l'appelant qui filtre
    (l'univers des Réglages les montre, la fiche non). Une clé archivée
    n'est JAMAIS recréée — la ressusciter en douce annulerait le geste de
    l'agence, exactement comme côté personne.
    """
    rows = list(
        (
            await db.execute(
                select(CompanyFieldDefinition).where(CompanyFieldDefinition.agency_id == agency_id)
            )
        ).scalars()
    )
    existing = {row.key for row in rows}
    presets = company_preset_keys()
    born: list[CompanyFieldDefinition] = []

    for position, key in enumerate(presets):
        if key in existing:
            continue
        field_type, section, labels = company_preset_spec(key)
        born.append(
            CompanyFieldDefinition(
                agency_id=agency_id,
                key=key,
                label=labels.get("fr") or humanize(key),
                label_i18n=labels,
                field_type=field_type,
                profile_section=section,
                position=position,
            )
        )

    # Les clés de SACK : elles n'existent que parce qu'une valeur a été
    # saisie quelque part. Elles naissent en `misc`, en `text`, APRÈS les
    # presets — jamais avec un type lu dans la valeur.
    next_position = len(presets)
    for key in sorted(sack_keys - existing - set(presets)):
        born.append(
            CompanyFieldDefinition(
                agency_id=agency_id,
                key=key,
                label=humanize(key),
                label_i18n=dict(_NO_I18N),
                field_type="text",
                profile_section="misc",
                position=next_position,
            )
        )
        next_position += 1

    if born:
        db.add_all(born)
        await db.flush()
        rows.extend(born)
    return rows


# ─── Les gestes, adressés par l'ID de définition ──────────────────────────
# Le front tient déjà `definition_id` et tape /agencies/me/custom-fields/
# {id} : c'est ce qui permet aux capacités société de devenir vraies sans
# qu'il apprenne une seconde route. Les ids sont des UUID — deux tables ne
# peuvent pas se disputer un id, la résolution est sans ambiguïté.


def company_definition_response(row: CompanyFieldDefinition) -> "CustomFieldDefinitionResponse":
    """Une définition société DANS LE CONTRAT DES DÉFINITIONS — même
    forme que la face personne, pour que l'écran n'apprenne pas un second
    vocabulaire.

    `scope='company'` est servi EN SORTIE seulement : c'est ce qui dit au
    front de quelle face vient la ligne. Aucune colonne `scope` n'existe
    dans cette table, et aucune n'a été ajoutée à l'autre — c'est
    précisément ce que la table dédiée permet d'éviter."""
    from src.custom_fields.custom_fields_schema import CustomFieldDefinitionResponse

    return CustomFieldDefinitionResponse(
        id=row.id,
        key=row.key,
        label=row.label,
        label_i18n=dict(row.label_i18n or {}),
        field_type=row.field_type,
        options=None,
        scope="company",
        profile_section=row.profile_section,
        required=False,
        position=row.position,
        archived_at=row.archived_at,
    )


async def company_definition_by_id(
    db: AsyncSession, agency_id: uuid.UUID, field_id: uuid.UUID
) -> CompanyFieldDefinition | None:
    stmt = select(CompanyFieldDefinition).where(
        CompanyFieldDefinition.agency_id == agency_id,
        CompanyFieldDefinition.id == field_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def company_definitions_by_ids(
    db: AsyncSession, agency_id: uuid.UUID, ids: set[uuid.UUID]
) -> list[CompanyFieldDefinition]:
    if not ids:
        return []
    stmt = select(CompanyFieldDefinition).where(
        CompanyFieldDefinition.agency_id == agency_id,
        CompanyFieldDefinition.id.in_(ids),
    )
    return list((await db.execute(stmt)).scalars().all())


async def company_value_counts(
    db: AsyncSession, agency_id: uuid.UUID, keys: set[str]
) -> dict[str, int]:
    """Combien de sociétés portent une valeur pour chaque clé — ce que
    l'archivage doit ANNONCER avant de retirer le champ de la fiche (les
    valeurs, elles, restent dans le sac).

    PAS DE PROTECTION PARCOURS ICI, et ce n'est pas un oubli : un parcours
    collecte des références PERSONNE/DOSSIER. Une clé comme `company_name`
    porte désormais deux définitions — celle de la face personne (que le
    parcours lit) et celle de la face société (celle-ci). Archiver la
    seconde ne retire rien à la première : les protéger l'une par l'autre
    inventerait un lien qui n'existe pas."""

    if not keys:
        return {}
    rows = await db.execute(
        text(
            "SELECT k, count(*) FROM company_profile c,"
            " LATERAL jsonb_object_keys(c.custom_fields) AS k"
            " WHERE c.agency_id = :agency_id AND k = ANY(:keys) GROUP BY k"
        ),
        {"agency_id": agency_id, "keys": list(keys)},
    )
    return {key: int(count) for key, count in rows}
