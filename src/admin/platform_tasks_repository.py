"""Pure DB access for the superadmin task backlog. The Prism list order,
computed at read time (no stored kanban position): done last, priority
desc (urgent > high > medium > low), due_at asc NULLS LAST, newest last
tiebreak."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.platform_task import PlatformTask

_PRIORITY_ORDER = case(
    (PlatformTask.priority == "urgent", 4),
    (PlatformTask.priority == "high", 3),
    (PlatformTask.priority == "medium", 2),
    else_=1,
)
_DONE_LAST = case((PlatformTask.status == "done", 1), else_=0)


class PlatformTasksRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _filtered(
        self,
        *,
        assigned_to: uuid.UUID | None,
        agency_id: uuid.UUID | None,
        status: str | None,
        include_done: bool,
        priority: str | None,
        task_type: str | None,
        is_overdue: bool,
        due_before: datetime | None,
        due_after: datetime | None,
    ) -> Select[tuple[PlatformTask]]:
        query = select(PlatformTask)
        if assigned_to is not None:
            query = query.where(PlatformTask.assigned_to_agent_id == assigned_to)
        if agency_id is not None:
            query = query.where(PlatformTask.agency_id == agency_id)
        if status is not None:
            query = query.where(PlatformTask.status == status)
        elif not include_done:
            query = query.where(PlatformTask.status != "done")
        if priority is not None:
            query = query.where(PlatformTask.priority == priority)
        if task_type is not None:
            query = query.where(PlatformTask.task_type == task_type)
        if is_overdue:
            query = query.where(
                PlatformTask.status != "done", PlatformTask.due_at < datetime.now(UTC)
            )
        if due_before is not None:
            query = query.where(PlatformTask.due_at <= due_before)
        if due_after is not None:
            query = query.where(PlatformTask.due_at >= due_after)
        return query

    async def list_page(
        self, *, page: int, page_size: int, **filters: object
    ) -> tuple[list[PlatformTask], int]:
        filtered = self._filtered(**filters)  # type: ignore[arg-type]
        total = (
            await self.db.execute(select(func.count()).select_from(filtered.subquery()))
        ).scalar_one()
        rows = (
            (
                await self.db.execute(
                    filtered.order_by(
                        _DONE_LAST.asc(),
                        _PRIORITY_ORDER.desc(),
                        PlatformTask.due_at.asc().nulls_last(),
                        PlatformTask.created_at.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    async def get(self, task_id: uuid.UUID) -> PlatformTask | None:
        return await self.db.get(PlatformTask, task_id)

    async def display_names(
        self, tasks: list[PlatformTask]
    ) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
        """(agency names, agent full names) for the rows — two IN queries.
        Covers assignees AND completers (one widened IN, not a third query)."""
        agency_ids = {t.agency_id for t in tasks if t.agency_id is not None}
        agent_ids = {t.assigned_to_agent_id for t in tasks} | {
            t.completed_by_agent_id for t in tasks if t.completed_by_agent_id is not None
        }
        agencies: dict[uuid.UUID, str] = {}
        if agency_ids:
            rows = await self.db.execute(
                select(Agency.id, Agency.name).where(Agency.id.in_(agency_ids))
            )
            agencies = {row.id: row.name for row in rows}
        agents: dict[uuid.UUID, str] = {}
        if agent_ids:
            rows = await self.db.execute(
                select(Agent.id, Agent.first_name, Agent.last_name).where(Agent.id.in_(agent_ids))
            )
            agents = {row.id: f"{row.first_name} {row.last_name}".strip() for row in rows}
        return agencies, agents

    async def summary(self) -> dict[str, int]:
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=now.weekday())
        not_done = PlatformTask.status != "done"
        row = (
            await self.db.execute(
                select(
                    func.count().label("total"),
                    func.count().filter(not_done).label("pending"),
                    func.count().filter(not_done, PlatformTask.due_at < now).label("overdue"),
                    func.count()
                    .filter(
                        not_done,
                        PlatformTask.due_at >= today_start,
                        PlatformTask.due_at < today_start + timedelta(days=1),
                    )
                    .label("due_today"),
                    func.count()
                    .filter(
                        PlatformTask.status == "done",
                        PlatformTask.completed_at >= week_start,
                    )
                    .label("completed_this_week"),
                ).select_from(PlatformTask)
            )
        ).one()
        return dict(row._mapping)
