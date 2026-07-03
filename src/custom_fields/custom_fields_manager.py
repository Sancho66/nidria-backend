import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, NotFoundError
from src.core.i18n import DEFAULT_LANG, apply_i18n_write
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.custom_fields.custom_fields_schema import (
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionUpdate,
)
from src.usage.usage_manager import UsageManager


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

    async def create(
        self, agent: Agent, payload: CustomFieldDefinitionCreate
    ) -> CustomFieldDefinition:
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
        )
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="agency.custom_fields_set",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"key": payload.key},
        )
        definition.label_i18n = label_blob
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

    async def archive(self, agent: Agent, field_id: uuid.UUID) -> CustomFieldDefinition:
        """Soft archive — the only form of removal. Saved values are
        kept (the JSONB is independent); the field leaves the form."""
        definition = await self.repo.get_in_agency(agent.agency_id, field_id)
        if definition is None:
            raise NotFoundError("Custom field not found.")
        if definition.archived_at is None:
            definition.archived_at = datetime.now(UTC)
            await self.db.commit()
            await self.db.refresh(definition)
        return definition

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
