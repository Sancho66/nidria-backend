import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, NotFoundError
from src.core.i18n import DEFAULT_LANG, apply_i18n_write
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.custom_fields.custom_fields_schema import (
    CustomFieldBulkRefusal,
    CustomFieldBulkReport,
    CustomFieldBulkRequest,
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionUpdate,
)
from src.usage.usage_manager import UsageManager


async def materialize_preset_definitions(
    db: "AsyncSession", agency_id: "uuid.UUID", keys: set[str], lang: str
) -> list[str]:
    """LA mécanique du picker (extraite de JourneysManager._materialize_
    catalog_definitions, réutilisée par l'import) : créer les défs
    manquantes pour des clés du CATALOGUE — label i18n complet, options
    en langue d'agence, les existantes (archivées comprises) jamais
    recréées (idempotent). Correctif au passage : scope et profile_section
    suivent la CLASSIFICATION du catalogue (les packs person naissaient
    'case'/'misc' depuis la migration f2d6 — bug latent des deux
    appelants). Retourne les clés créées."""
    from shared.models.custom_field import CustomFieldDefinition
    from src.client_profiles.profile_sections import catalog_classification
    from src.custom_fields.custom_fields_repository import CustomFieldsRepository
    from src.journeys.field_catalog import FIELD_PRESETS

    wanted = {k for k in keys if k in FIELD_PRESETS}
    if not wanted:
        return []
    existing = {
        d.key
        for d in await CustomFieldsRepository(db).list_for_agency(agency_id, include_archived=True)
    }
    created: list[str] = []
    for key in sorted(wanted - existing):
        preset = FIELD_PRESETS[key]
        options = None
        if preset.options is not None:
            options = preset.options.get(lang) or preset.options["fr"]
        scope, section = catalog_classification(key)
        db.add(
            CustomFieldDefinition(
                agency_id=agency_id,
                key=key,
                label=preset.labels.get(lang) or preset.labels["fr"],
                label_i18n=dict(preset.labels),
                field_type=preset.field_type,
                options=options,
                scope=scope,
                profile_section=section,
            )
        )
        created.append(key)
    return created


class CustomFieldsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CustomFieldsRepository(db)

    async def list_definitions(
        self, agent: Agent, *, include_archived: bool = False
    ) -> list[CustomFieldDefinition]:
        return await self.repo.list_for_agency(agent.agency_id, include_archived=include_archived)

    async def active_definitions(self, agency_id: uuid.UUID) -> list[CustomFieldDefinition]:
        return await self.repo.list_for_agency(agency_id, include_archived=False)

    async def agency_default(self, agency_id: uuid.UUID) -> str:
        """The agency's default content language (i18n label fallback)."""
        stmt = select(Agency.default_language).where(Agency.id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none() or DEFAULT_LANG

    async def build_definition(
        self,
        agent: Agent,
        payload: CustomFieldDefinitionCreate,
        *,
        scope: str,
        profile_section: str = "misc",
    ) -> CustomFieldDefinition:
        """Le cœur SANS COMMIT de `create` — réutilisé par la création
        depuis la grille d'import (le batch reste transactionnel, un seul
        commit en bout de course). Même validation, même i18n.

        `scope` N'A PLUS DE DÉFAUT (arbitrage du 07/08). Trois trous en
        trois semaines venaient tous du même motif : un appelant oubliait
        l'argument, le défaut « mission » s'appliquait en silence, et
        personne ne le voyait avant qu'une agence se plaigne de champs
        invisibles. Un paramètre sans défaut fait échouer la PASSE, pas le
        client. Une clé du catalogue se classe par
        `catalog_classification` ; un champ voulu par l'agence porte le
        choix qu'elle a fait à l'écran."""
        if await self.repo.get_by_key(agent.agency_id, payload.key) is not None:
            raise ConflictError(f"A custom field with key {payload.key!r} already exists.")
        agency_default = await self.agency_default(agent.agency_id)
        label_scalar, label_blob = apply_i18n_write(
            payload.label_i18n, payload.label, agency_default, None, {}
        )
        definition = self.repo.add(
            agency_id=agent.agency_id,
            key=payload.key,
            label=label_scalar or payload.label,
            field_type=payload.field_type.value,
            options=payload.options,
            required=payload.required,
            position=payload.position,
            scope=scope,
            profile_section=profile_section,
        )
        definition.label_i18n = label_blob
        return definition

    async def create(
        self, agent: Agent, payload: CustomFieldDefinitionCreate
    ) -> CustomFieldDefinition:
        # La portée voulue, ou le défaut historique si l'appelant se tait.
        definition = await self.build_definition(agent, payload, scope=payload.scope or "case")
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="agency.custom_fields_set",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"key": payload.key},
        )
        await self.db.commit()
        await self.db.refresh(definition)
        return definition

    async def update(
        self, agent: Agent, field_id: uuid.UUID, payload: CustomFieldDefinitionUpdate
    ) -> CustomFieldDefinition:
        definition = await self.repo.get_in_agency(agent.agency_id, field_id)
        if definition is None:
            raise NotFoundError("Custom field not found.")
        provided = payload.model_dump(exclude_unset=True)
        # key and field_type are immutable (not in the update schema).
        if "label" in provided or "label_i18n" in provided:
            agency_default = await self.agency_default(agent.agency_id)
            scalar, blob = apply_i18n_write(
                payload.label_i18n if "label_i18n" in provided else None,
                payload.label if "label" in provided else None,
                agency_default,
                definition.label,
                definition.label_i18n,
            )
            definition.label = scalar or definition.label
            definition.label_i18n = blob
        if "scope" in provided and provided["scope"] is not None:
            definition.scope = provided["scope"]
        if "profile_section" in provided and provided["profile_section"] is not None:
            definition.profile_section = provided["profile_section"]
        if "required" in provided and provided["required"] is not None:
            definition.required = provided["required"]
        if "position" in provided and provided["position"] is not None:
            definition.position = provided["position"]
        if "options" in provided:
            # Only meaningful for select types; the create-time validator
            # already pinned that. Editing options is allowed (adding or
            # removing) — removed options orphan existing values, kept.
            definition.options = provided["options"]
        await self.db.commit()
        await self.db.refresh(definition)
        return definition

    async def archive(
        self, agent: Agent, field_id: uuid.UUID, *, force: bool = False
    ) -> CustomFieldDefinition:
        """Soft archive — the only form of removal. Saved values are
        kept (the JSONB is independent); the field leaves the form.

        LA PROTECTION PARCOURS (posée ici, elle n'existait pas) : un champ
        qu'un parcours collecte ou exige ne part pas en silence — 409 qui
        NOMME les parcours. `force=True` la franchit, en connaissance de
        cause. Ce n'est pas un mur : le reste du système tolère un champ
        archivé attaché (il reste listé, drapeau `is_archived`), et 175 des
        254 définitions actives de la base de dev sont dans ce cas — un
        refus sans issue rendrait l'archivage impraticable."""
        definition = await self.repo.get_in_agency(agent.agency_id, field_id)
        if definition is None:
            raise NotFoundError("Custom field not found.")
        if definition.archived_at is None:
            if not force:
                usage = await self.repo.journey_usage(agent.agency_id, {definition.key})
                if definition.key in usage:
                    raise ConflictError(
                        f"Custom field {definition.key!r} is used by a journey template.",
                        code="custom_field.used_in_journey",
                        params={"label": definition.label, "templates": usage[definition.key]},
                    )
            definition.archived_at = datetime.now(UTC)
            await self.db.commit()
            await self.db.refresh(definition)
        return definition

    async def bulk(self, agent: Agent, payload: CustomFieldBulkRequest) -> CustomFieldBulkReport:
        """LES TROIS GESTES DE MASSE — archiver, reclasser, ranger — sur
        une SÉLECTION d'ids, en une transaction et un rapport.

        UNE SEULE évaluation sert le dry-run et le geste : le compte
        annoncé à l'écran et ce qui est appliqué ne peuvent pas diverger
        (même principe que l'aperçu d'import, où `_analyze` décide seul).
        `dry_run` s'arrête juste avant l'écriture, tout le reste est
        identique — refus compris."""
        ids = set(payload.ids)
        found = await self.repo.by_ids_in_agency(agent.agency_id, ids)
        by_id = {d.id: d for d in found}
        # Un id inconnu (ou d'une autre agence) est un refus, pas un silence.
        refusals = [
            CustomFieldBulkRefusal(id=missing, reason="not_found")
            for missing in sorted(ids - by_id.keys(), key=str)
        ]
        keys = {d.key for d in found}
        usage = await self.repo.journey_usage(agent.agency_id, keys)
        values = await self.repo.value_counts(agent.agency_id, keys)

        eligible: list[CustomFieldDefinition] = []
        unchanged = 0
        for definition in found:
            if payload.action == "archive":
                if definition.archived_at is not None:
                    unchanged += 1
                    continue
                if definition.key in usage and not payload.force:
                    refusals.append(
                        CustomFieldBulkRefusal(
                            id=definition.id,
                            key=definition.key,
                            label=definition.label,
                            reason="used_in_journey",
                            templates=usage[definition.key],
                        )
                    )
                    continue
            elif (
                payload.action == "scope"
                and definition.scope == payload.scope
                or (
                    payload.action == "section"
                    and definition.profile_section == payload.profile_section
                )
            ):
                unchanged += 1
                continue
            eligible.append(definition)

        # Les conséquences se comptent sur les ÉLIGIBLES : annoncer des
        # valeurs concernées par un champ qu'on refuse serait un mensonge.
        report = CustomFieldBulkReport(
            dry_run=payload.dry_run,
            requested=len(ids),
            eligible=len(eligible),
            applied=0,
            unchanged=unchanged,
            refused=len(refusals),
            with_values=sum(1 for d in eligible if values.get(d.key)),
            values_count=sum(values.get(d.key, 0) for d in eligible),
            in_journey=sum(1 for d in eligible if d.key in usage),
            refusals=refusals,
        )
        if payload.dry_run or not eligible:
            return report

        if payload.action == "archive":
            changes: dict[str, object] = {"archived_at": datetime.now(UTC)}
        elif payload.action == "scope":
            changes = {"scope": payload.scope}
        else:
            changes = {"profile_section": payload.profile_section}
        # UN seul UPDATE pour toute la sélection (leçon du lot d'import :
        # une écriture par ligne coûte une requête par ligne).
        await self.db.execute(
            update(CustomFieldDefinition)
            .where(CustomFieldDefinition.id.in_([d.id for d in eligible]))
            .values(**changes)
            .execution_options(synchronize_session=False)
        )
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="agency.custom_fields_set",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={
                "bulk": payload.action,
                "count": len(eligible),
                "keys": sorted(d.key for d in eligible),
                **({"scope": payload.scope} if payload.action == "scope" else {}),
                **(
                    {"profile_section": payload.profile_section}
                    if payload.action == "section"
                    else {}
                ),
                **({"forced": True} if payload.force and payload.action == "archive" else {}),
            },
        )
        await self.db.commit()
        report.applied = len(eligible)
        return report

    async def unarchive(self, agent: Agent, field_id: uuid.UUID) -> CustomFieldDefinition:
        """Symmetric to archive: clears archived_at. The field reappears
        in forms and its previously-orphaned JSONB values become exposed
        and validable again — the (agency_id, key) UNIQUE covers archived
        rows too, so resurrection can never collide. Idempotent: a no-op
        if already active."""
        definition = await self.repo.get_in_agency(agent.agency_id, field_id)
        if definition is None:
            raise NotFoundError("Custom field not found.")
        if definition.archived_at is not None:
            definition.archived_at = None
            await self.db.commit()
            await self.db.refresh(definition)
        return definition
