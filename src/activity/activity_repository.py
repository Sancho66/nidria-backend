import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.client_case import ClientCase
from src.activity.cost_redaction import cost_blind_clause


class ActivityRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_case_activity(
        self,
        case_id: uuid.UUID,
        action_types: list[str] | None,
        page: int,
        page_size: int,
        *,
        include_cost: bool,
    ) -> tuple[list[ActivityLog], int]:
        stmt = select(ActivityLog).where(ActivityLog.case_id == case_id)
        if action_types:
            stmt = stmt.where(ActivityLog.action_type.in_(action_types))
        if not include_cost:
            # In SQL, before the count: `total` and the page size never
            # count an entry the reader will not see (cost_redaction).
            stmt = stmt.where(cost_blind_clause())
        total = (
            await self.db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        stmt = (
            stmt.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list((await self.db.execute(stmt)).scalars()), total
