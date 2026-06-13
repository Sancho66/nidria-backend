import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.custom_field import CustomFieldDefinition


class CustomFieldsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_agency(
        self, agency_id: uuid.UUID, *, include_archived: bool = False
    ) -> list[CustomFieldDefinition]:
        stmt = select(CustomFieldDefinition).where(CustomFieldDefinition.agency_id == agency_id)
        if not include_archived:
            stmt = stmt.where(CustomFieldDefinition.archived_at.is_(None))
        stmt = stmt.order_by(CustomFieldDefinition.position, CustomFieldDefinition.created_at)
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

    def add(self, **kwargs: object) -> CustomFieldDefinition:
        definition = CustomFieldDefinition(**kwargs)
        self.db.add(definition)
        return definition
