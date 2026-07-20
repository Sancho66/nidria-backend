"""Superadmin task backlog — the Prism transition model, whole: any lane
to any lane, the ONLY mechanic is the completion audit stamp on entering
a done lane (and the clear on leaving it). v1 scope (GO 2026-07-20):
no ActivityLog (ours requires a case), no emails (2 superadmins), no
contact, fixed 3 lanes."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.platform_task import (
    PLATFORM_TASK_PRIORITIES,
    PLATFORM_TASK_STATUSES,
    PlatformTask,
)
from src.admin.platform_tasks_repository import PlatformTasksRepository
from src.admin.platform_tasks_schema import (
    PlatformTaskCreate,
    PlatformTaskListResponse,
    PlatformTaskRead,
    PlatformTaskSummary,
    PlatformTaskUpdate,
)
from src.core.exceptions import NotFoundError, ValidationError
from src.core.rbac.baseline import PLATFORM_ROLE_NAMES


class PlatformTasksManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repository = PlatformTasksRepository(db)

    # --- validation -----------------------------------------------------------

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in PLATFORM_TASK_STATUSES:
            raise ValidationError(
                f"Unknown task status {status!r}. Allowed: {list(PLATFORM_TASK_STATUSES)}.",
                code="task.status_unknown",
            )

    @staticmethod
    def _validate_priority(priority: str) -> None:
        if priority not in PLATFORM_TASK_PRIORITIES:
            raise ValidationError(
                f"Unknown task priority {priority!r}. Allowed: {list(PLATFORM_TASK_PRIORITIES)}.",
                code="task.priority_unknown",
            )

    async def _validate_assignee(self, agent_id: uuid.UUID) -> None:
        """The assignee must be a platform operator (superadmin role) —
        the Prism 'member of the project' check, platform edition."""
        agent = (
            await self.db.execute(
                select(Agent).options(joinedload(Agent.role)).where(Agent.id == agent_id)
            )
        ).scalar_one_or_none()
        if agent is None or not (
            agent.role is not None
            and agent.role.is_system
            and agent.role.name in PLATFORM_ROLE_NAMES
        ):
            raise ValidationError(
                "The assignee must be a platform superadmin.",
                code="task.assignee_not_superadmin",
            )

    async def _validate_agency(self, agency_id: uuid.UUID) -> None:
        if await self.db.get(Agency, agency_id) is None:
            raise NotFoundError("Agency not found.", code="agency.not_found")

    # --- projection -----------------------------------------------------------

    async def _project(self, tasks: list[PlatformTask]) -> list[PlatformTaskRead]:
        agencies, agents = await self.repository.display_names(tasks)
        now = datetime.now(UTC)
        return [
            PlatformTaskRead(
                id=t.id,
                title=t.title,
                description=t.description,
                status=t.status,
                priority=t.priority,
                due_at=t.due_at,
                is_overdue=(t.status != "done" and t.due_at is not None and t.due_at < now),
                agency_id=t.agency_id,
                agency_name=agencies.get(t.agency_id) if t.agency_id else None,
                assigned_to_agent_id=t.assigned_to_agent_id,
                assigned_to_name=agents.get(t.assigned_to_agent_id, ""),
                created_by_agent_id=t.created_by_agent_id,
                completed_by_agent_id=t.completed_by_agent_id,
                completed_at=t.completed_at,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
            for t in tasks
        ]

    async def _get_or_404(self, task_id: uuid.UUID) -> PlatformTask:
        task = await self.repository.get(task_id)
        if task is None:
            raise NotFoundError("Task not found.", code="task.not_found")
        return task

    # --- status mechanics (the whole Prism transition model) ------------------

    def _apply_status(self, task: PlatformTask, new_status: str, actor: Agent) -> None:
        self._validate_status(new_status)
        if new_status == task.status:
            return
        was_done, is_done = task.status == "done", new_status == "done"
        task.status = new_status
        if is_done and not was_done:
            task.completed_at = datetime.now(UTC)
            task.completed_by_agent_id = actor.id
        elif was_done and not is_done:
            task.completed_at = None
            task.completed_by_agent_id = None

    # --- use cases ------------------------------------------------------------

    async def list_tasks(
        self, *, page: int, page_size: int, **filters: object
    ) -> PlatformTaskListResponse:
        tasks, total = await self.repository.list_page(page=page, page_size=page_size, **filters)
        return PlatformTaskListResponse(
            items=await self._project(tasks), total=total, page=page, page_size=page_size
        )

    async def create(self, actor: Agent, payload: PlatformTaskCreate) -> PlatformTaskRead:
        self._validate_priority(payload.priority)
        assignee = payload.assigned_to_agent_id or actor.id
        await self._validate_assignee(assignee)
        if payload.agency_id is not None:
            await self._validate_agency(payload.agency_id)
        task = PlatformTask(
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            due_at=payload.due_at,
            agency_id=payload.agency_id,
            assigned_to_agent_id=assignee,
            created_by_agent_id=actor.id,
        )
        # Created straight into a done lane: the audit stamp applies (Prism).
        self._apply_status(task, payload.status or "todo", actor)
        self.db.add(task)
        await self.db.commit()
        await self.db.refresh(task)
        return (await self._project([task]))[0]

    async def update(
        self, actor: Agent, task_id: uuid.UUID, payload: PlatformTaskUpdate
    ) -> PlatformTaskRead:
        task = await self._get_or_404(task_id)
        fields = payload.model_fields_set
        if "title" in fields and payload.title is not None:
            task.title = payload.title
        if "description" in fields:
            task.description = payload.description
        if "priority" in fields and payload.priority is not None:
            self._validate_priority(payload.priority)
            task.priority = payload.priority
        if "due_at" in fields:
            task.due_at = payload.due_at
        if "agency_id" in fields:
            if payload.agency_id is not None:
                await self._validate_agency(payload.agency_id)
            task.agency_id = payload.agency_id
        if "assigned_to_agent_id" in fields and payload.assigned_to_agent_id is not None:
            await self._validate_assignee(payload.assigned_to_agent_id)
            task.assigned_to_agent_id = payload.assigned_to_agent_id
        if "status" in fields and payload.status is not None:
            self._apply_status(task, payload.status, actor)
        await self.db.commit()
        await self.db.refresh(task)
        return (await self._project([task]))[0]

    async def complete(self, actor: Agent, task_id: uuid.UUID) -> PlatformTaskRead:
        task = await self._get_or_404(task_id)
        self._apply_status(task, "done", actor)
        await self.db.commit()
        await self.db.refresh(task)
        return (await self._project([task]))[0]

    async def reopen(self, actor: Agent, task_id: uuid.UUID) -> PlatformTaskRead:
        task = await self._get_or_404(task_id)
        self._apply_status(task, "todo", actor)
        await self.db.commit()
        await self.db.refresh(task)
        return (await self._project([task]))[0]

    async def delete(self, task_id: uuid.UUID) -> None:
        task = await self._get_or_404(task_id)
        await self.db.delete(task)
        await self.db.commit()

    async def summary(self) -> PlatformTaskSummary:
        return PlatformTaskSummary(**await self.repository.summary())
