"""Data access for saved CRM import mappings (BLOC 3).

EVERY query is scoped by agency_id — a mapping is never read or written
cross-agency (same RGPD rigour as the rest of the codebase)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.crm_import_mapping import CrmImportMapping


class MappingRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, agency_id: uuid.UUID, mapping_id: uuid.UUID) -> CrmImportMapping | None:
        stmt = select(CrmImportMapping).where(
            CrmImportMapping.id == mapping_id,
            CrmImportMapping.agency_id == agency_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_first_for_crm(
        self, agency_id: uuid.UUID, journey_template_id: uuid.UUID, crm_slug: str
    ) -> CrmImportMapping | None:
        """The oldest config for (agency, parcours, CRM). Several may now exist
        (named variants); the legacy resolve / import-by-crm_slug paths take the
        first deterministically rather than failing on multiple rows."""
        stmt = (
            select(CrmImportMapping)
            .where(
                CrmImportMapping.agency_id == agency_id,
                CrmImportMapping.journey_template_id == journey_template_id,
                CrmImportMapping.crm_slug == crm_slug,
            )
            .order_by(CrmImportMapping.created_at)
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def get_by_name(
        self,
        agency_id: uuid.UUID,
        journey_template_id: uuid.UUID,
        crm_slug: str,
        name: str,
    ) -> CrmImportMapping | None:
        """The config with this exact (agency, parcours, CRM, name) — the
        natural key. Used to report a same-name conflict precisely (instead of
        relying on the DB IntegrityError, which could also fire on an older,
        stricter constraint)."""
        stmt = select(CrmImportMapping).where(
            CrmImportMapping.agency_id == agency_id,
            CrmImportMapping.journey_template_id == journey_template_id,
            CrmImportMapping.crm_slug == crm_slug,
            CrmImportMapping.name == name,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        agency_id: uuid.UUID,
        *,
        journey_template_id: uuid.UUID | None = None,
        crm_slug: str | None = None,
    ) -> list[CrmImportMapping]:
        stmt = select(CrmImportMapping).where(CrmImportMapping.agency_id == agency_id)
        if journey_template_id is not None:
            stmt = stmt.where(CrmImportMapping.journey_template_id == journey_template_id)
        if crm_slug is not None:
            stmt = stmt.where(CrmImportMapping.crm_slug == crm_slug)
        stmt = stmt.order_by(CrmImportMapping.created_at)
        return list((await self.db.execute(stmt)).scalars())

    def add(self, **kwargs: Any) -> CrmImportMapping:
        row = CrmImportMapping(**kwargs)
        self.db.add(row)
        return row

    async def delete(self, row: CrmImportMapping) -> None:
        await self.db.delete(row)
