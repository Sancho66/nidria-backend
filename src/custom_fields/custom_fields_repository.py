import uuid
from collections import defaultdict

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.custom_field import CustomFieldDefinition

# LES TROIS SACS où une valeur de champ perso peut vivre. Une clé est une
# clé : on compte partout, quelle que soit la portée courante de la
# définition — c'est ce qui rend le compte annoncé VRAI même après une
# reclassification (une valeur ne suit pas la portée, elle reste dans son
# sac). Les dossiers supprimés ne comptent pas : ils sont invisibles
# partout ailleurs, ils ne vont pas resurgir dans un avertissement.
_VALUE_SACKS = (
    "SELECT k, count(*) AS n FROM client_profile p,"
    " LATERAL jsonb_object_keys(p.custom_fields) AS k"
    " WHERE p.agency_id = :agency_id AND k IN :keys GROUP BY k",
    "SELECT k, count(*) AS n FROM company_profile c,"
    " LATERAL jsonb_object_keys(c.custom_fields) AS k"
    " WHERE c.agency_id = :agency_id AND k IN :keys GROUP BY k",
    "SELECT k, count(*) AS n FROM case_person cp"
    " JOIN client_case cc ON cc.id = cp.case_id,"
    " LATERAL jsonb_object_keys(cp.custom_fields) AS k"
    " WHERE cc.agency_id = :agency_id AND cc.deleted_at IS NULL"
    " AND k IN :keys GROUP BY k",
)

# Un champ perso est référencé par un parcours à DEUX endroits : la
# collecte à la création (`journey_template_field`) et les exigences
# d'étape (`step_requirement`). Les deux pointent la définition par sa
# CLÉ, jamais par son id — d'où la jointure sur `reference`.
_JOURNEY_USAGE = (
    "SELECT f.reference AS key, t.name AS template FROM journey_template_field f"
    " JOIN journey_template t ON t.id = f.template_id"
    " WHERE t.agency_id = :agency_id AND f.kind = 'custom_field' AND f.reference IN :keys"
    " UNION"
    " SELECT r.reference AS key, t.name AS template FROM step_requirement r"
    " JOIN journey_template_step s ON s.id = r.step_id"
    " JOIN journey_template t ON t.id = s.template_id"
    " WHERE t.agency_id = :agency_id AND r.kind = 'custom_field' AND r.reference IN :keys"
)


class CustomFieldsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_agency(
        self, agency_id: uuid.UUID, *, include_archived: bool = False
    ) -> list[CustomFieldDefinition]:
        stmt = select(CustomFieldDefinition).where(CustomFieldDefinition.agency_id == agency_id)
        if not include_archived:
            stmt = stmt.where(CustomFieldDefinition.archived_at.is_(None))
        # TIE-BREAKER STABLE (D12) : 4 entrées sur 12 partageaient position
        # ET created_at à la microseconde en dev — l'ordre changeait tout
        # seul entre deux lectures. `id` en dernier ressort : deux lectures
        # ne peuvent plus rendre deux ordres.
        stmt = stmt.order_by(
            CustomFieldDefinition.position,
            CustomFieldDefinition.created_at,
            CustomFieldDefinition.id,
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_in_agency(
        self, agency_id: uuid.UUID, field_id: uuid.UUID
    ) -> CustomFieldDefinition | None:
        stmt = select(CustomFieldDefinition).where(
            CustomFieldDefinition.id == field_id,
            CustomFieldDefinition.agency_id == agency_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_by_key(self, agency_id: uuid.UUID, key: str) -> CustomFieldDefinition | None:
        stmt = select(CustomFieldDefinition).where(
            CustomFieldDefinition.agency_id == agency_id,
            CustomFieldDefinition.key == key,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def by_ids_in_agency(
        self, agency_id: uuid.UUID, ids: set[uuid.UUID]
    ) -> list[CustomFieldDefinition]:
        """UNE lecture pour toute la sélection (les gestes de masse ne
        relisent pas ligne par ligne). Le scope agence est dans le WHERE :
        un id d'une autre agence ne remonte pas — il finira `not_found`."""
        if not ids:
            return []
        stmt = select(CustomFieldDefinition).where(
            CustomFieldDefinition.agency_id == agency_id,
            CustomFieldDefinition.id.in_(ids),
        )
        return list((await self.db.execute(stmt)).scalars())

    async def journey_usage(self, agency_id: uuid.UUID, keys: set[str]) -> dict[str, list[str]]:
        """clé → noms des parcours qui la collectent ou l'exigent. Sert la
        protection d'archivage ET l'avertissement de l'écran : nommer, pas
        seulement compter."""
        if not keys:
            return {}
        stmt = text(_JOURNEY_USAGE).bindparams(bindparam("keys", expanding=True))
        rows = await self.db.execute(stmt, {"agency_id": agency_id, "keys": sorted(keys)})
        usage: dict[str, list[str]] = defaultdict(list)
        for key, template in rows:
            usage[key].append(template)
        return {key: sorted(set(names)) for key, names in usage.items()}

    async def value_counts(self, agency_id: uuid.UUID, keys: set[str]) -> dict[str, int]:
        """clé → nombre d'enregistrements portant une valeur pour elle, les
        trois sacs additionnés. Une requête par sac (jamais une par clé)."""
        if not keys:
            return {}
        counts: dict[str, int] = defaultdict(int)
        for sql in _VALUE_SACKS:
            stmt = text(sql).bindparams(bindparam("keys", expanding=True))
            rows = await self.db.execute(stmt, {"agency_id": agency_id, "keys": sorted(keys)})
            for key, n in rows:
                counts[key] += n
        return dict(counts)

    def add(self, **kwargs: object) -> CustomFieldDefinition:
        definition = CustomFieldDefinition(**kwargs)
        self.db.add(definition)
        return definition
