import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from src.dashboard.dashboard_schema import DashboardResponse


class DashboardManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _count_by(
        self,
        agency_id: uuid.UUID,
        column: InstrumentedAttribute[str] | InstrumentedAttribute[str | None],
    ) -> dict[str, int]:
        stmt = (
            select(column, func.count())
            .where(ClientCase.agency_id == agency_id, ClientCase.deleted_at.is_(None))
            .group_by(column)
        )
        return {key: count for key, count in (await self.db.execute(stmt)).all() if key is not None}

    async def get_dashboard(self, agent: Agent) -> DashboardResponse:
        by_status = await self._count_by(agent.agency_id, ClientCase.status)
        by_dest_country = await self._count_by(agent.agency_id, ClientCase.dest_country)
        return DashboardResponse(
            total_cases=sum(by_status.values()),
            by_status=by_status,
            by_dest_country=by_dest_country,
        )
