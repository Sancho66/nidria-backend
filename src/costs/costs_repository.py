import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_step_cost import CaseStepCost
from shared.models.case_step_progress import CaseStepProgress


class CostsRepository:
    """Cost lines are ALWAYS reached through case_step_progress → case, so a
    line of another case (or agency) is invisible here. Queried ONLY by the
    agency-facing costs manager — never by expat_schema nor external_schema."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_case(self, case_id: uuid.UUID) -> list[CaseStepCost]:
        stmt = (
            select(CaseStepCost)
            .join(CaseStepProgress, CaseStepProgress.id == CaseStepCost.case_step_progress_id)
            .where(CaseStepProgress.case_id == case_id)
            .order_by(CaseStepCost.incurred_on, CaseStepCost.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_line_in_case(self, case_id: uuid.UUID, cost_id: uuid.UUID) -> CaseStepCost | None:
        stmt = (
            select(CaseStepCost)
            .join(CaseStepProgress, CaseStepProgress.id == CaseStepCost.case_step_progress_id)
            .where(CaseStepCost.id == cost_id, CaseStepProgress.case_id == case_id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_progress_in_case(
        self, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> CaseStepProgress | None:
        stmt = select(CaseStepProgress).where(
            CaseStepProgress.id == progress_id, CaseStepProgress.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_line(self, **kwargs: Any) -> CaseStepCost:
        line = CaseStepCost(**kwargs)
        self.db.add(line)
        return line
