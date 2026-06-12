"""Saved views — ported from Prism (src/views/views_repository)."""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.saved_view import SavedView


class ViewsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_agent(
        self,
        agency_id: uuid.UUID,
        agent_id: uuid.UUID,
        entity: str | None = None,
    ) -> list[SavedView]:
        """The agent's own views + every shared view of the agency.

        Customizable "All" rows (is_default_all=True) are excluded —
        they are a personal display preference, not a named view; the
        frontend fetches them through GET /views/default-all instead.

        Defaults sort first, then alphabetical by name."""
        stmt = (
            select(SavedView)
            .where(
                SavedView.agency_id == agency_id,
                SavedView.is_default_all.is_(False),
                or_(
                    SavedView.agent_id == agent_id,
                    SavedView.is_shared.is_(True),
                ),
            )
            .options(selectinload(SavedView.agent))
        )
        if entity is not None:
            stmt = stmt.where(SavedView.entity == entity)
        stmt = stmt.order_by(SavedView.is_default.desc(), SavedView.name.asc())
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_default_all(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID, entity: str
    ) -> SavedView | None:
        """The caller's customizable "All" row, or None. Backs the
        GET / PUT (upsert) / DELETE default-all routes."""
        stmt = (
            select(SavedView)
            .where(
                SavedView.agency_id == agency_id,
                SavedView.agent_id == agent_id,
                SavedView.entity == entity,
                SavedView.is_default_all.is_(True),
            )
            .options(selectinload(SavedView.agent))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, agency_id: uuid.UUID, view_id: uuid.UUID) -> SavedView | None:
        stmt = (
            select(SavedView)
            .where(SavedView.agency_id == agency_id, SavedView.id == view_id)
            .options(selectinload(SavedView.agent))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()
