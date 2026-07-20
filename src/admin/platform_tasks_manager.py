"""Superadmin task backlog — the Prism transition model, whole: any lane
to any lane, the ONLY mechanic is the completion audit stamp on entering
a done lane (and the clear on leaving it). No ActivityLog (ours requires
a case), no contact, fixed 3 lanes.

Emails (Prism model, exact): assignee on creation, creator on status
change, new assignee + creator on reassignment — the actor is NEVER
their own recipient, recipients deduplicated, sends never blocking."""

import asyncio
import logging
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
    PLATFORM_TASK_TYPES,
    PlatformTask,
)
from shared.models.rbac import Role
from src.admin.platform_tasks_repository import PlatformTasksRepository
from src.admin.platform_tasks_schema import (
    PlatformOperatorRead,
    PlatformTaskCreate,
    PlatformTaskListResponse,
    PlatformTaskRead,
    PlatformTaskSummary,
    PlatformTaskUpdate,
)
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import (
    EmailContent,
    task_assigned_email,
    task_status_changed_email,
)
from src.core.exceptions import NotFoundError, ValidationError
from src.core.i18n import resolve_notification_lang_agent
from src.core.rbac.baseline import PLATFORM_ROLE_NAMES

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _validate_task_type(task_type: str) -> None:
        if task_type not in PLATFORM_TASK_TYPES:
            raise ValidationError(
                f"Unknown task type {task_type!r}. Allowed: {list(PLATFORM_TASK_TYPES)}.",
                code="task.type_unknown",
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
                task_type=t.task_type,
                due_at=t.due_at,
                is_overdue=(t.status != "done" and t.due_at is not None and t.due_at < now),
                agency_id=t.agency_id,
                agency_name=agencies.get(t.agency_id) if t.agency_id else None,
                assigned_to_agent_id=t.assigned_to_agent_id,
                assigned_to_name=agents.get(t.assigned_to_agent_id, ""),
                created_by_agent_id=t.created_by_agent_id,
                completed_by_agent_id=t.completed_by_agent_id,
                completed_by_name=(
                    agents.get(t.completed_by_agent_id) if t.completed_by_agent_id else None
                ),
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

    # --- emails (the Prism model, never blocking) -----------------------------

    async def _recipient(self, agent_id: uuid.UUID) -> tuple[str, str] | None:
        """(email, lang) of an agent, lang resolved by the existing agent
        rule (their agency's default language)."""
        row = (
            await self.db.execute(
                select(Agent.email, Agency.default_language)
                .join(Agency, Agency.id == Agent.agency_id)
                .where(Agent.id == agent_id)
            )
        ).first()
        if row is None:
            return None
        return row.email, resolve_notification_lang_agent(row.default_language)

    async def _agency_name(self, agency_id: uuid.UUID | None) -> str | None:
        if agency_id is None:
            return None
        agency = await self.db.get(Agency, agency_id)
        return agency.name if agency else None

    @staticmethod
    def _actor_name(actor: Agent) -> str:
        return f"{actor.first_name} {actor.last_name}".strip()

    async def _notify(self, mails: dict[uuid.UUID, tuple[str, EmailContent]]) -> None:
        """Send after commit, deduplicated by recipient, NEVER blocking
        the mutation (the Prism _safe_send pattern)."""
        for to, content in mails.values():
            try:
                await asyncio.to_thread(send_email, to, content.subject, content.text, content.html)
            except Exception:
                logger.warning("platform task email failed (never blocking)", exc_info=True)

    async def _assigned_mail(
        self, task: PlatformTask, recipient_id: uuid.UUID, actor: Agent
    ) -> tuple[str, EmailContent] | None:
        recipient = await self._recipient(recipient_id)
        if recipient is None:
            return None
        email_addr, lang = recipient
        content = task_assigned_email(
            title=task.title,
            priority=task.priority,
            due=task.due_at.strftime("%Y-%m-%d") if task.due_at else None,
            agency_name=await self._agency_name(task.agency_id),
            actor_name=self._actor_name(actor),
            tasks_url=f"{get_settings().frontend_url}/admin/tasks",
            lang=lang,
        )
        return email_addr, content

    async def _status_mail(
        self, task: PlatformTask, recipient_id: uuid.UUID, actor: Agent
    ) -> tuple[str, EmailContent] | None:
        recipient = await self._recipient(recipient_id)
        if recipient is None:
            return None
        email_addr, lang = recipient
        content = task_status_changed_email(
            title=task.title,
            new_status=task.status,
            actor_name=self._actor_name(actor),
            tasks_url=f"{get_settings().frontend_url}/admin/tasks",
            lang=lang,
        )
        return email_addr, content

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
        self._validate_task_type(payload.task_type)
        assignee = payload.assigned_to_agent_id or actor.id
        await self._validate_assignee(assignee)
        if payload.agency_id is not None:
            await self._validate_agency(payload.agency_id)
        task = PlatformTask(
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            task_type=payload.task_type,
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
        if task.assigned_to_agent_id != actor.id:  # never mail yourself
            mail = await self._assigned_mail(task, task.assigned_to_agent_id, actor)
            if mail is not None:
                await self._notify({task.assigned_to_agent_id: mail})
        return (await self._project([task]))[0]

    async def update(
        self, actor: Agent, task_id: uuid.UUID, payload: PlatformTaskUpdate
    ) -> PlatformTaskRead:
        task = await self._get_or_404(task_id)
        prev_status, prev_assignee = task.status, task.assigned_to_agent_id
        fields = payload.model_fields_set
        if "title" in fields and payload.title is not None:
            task.title = payload.title
        if "description" in fields:
            task.description = payload.description
        if "priority" in fields and payload.priority is not None:
            self._validate_priority(payload.priority)
            task.priority = payload.priority
        if "task_type" in fields and payload.task_type is not None:
            self._validate_task_type(payload.task_type)
            task.task_type = payload.task_type
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
        mails: dict[uuid.UUID, tuple[str, EmailContent]] = {}
        if task.assigned_to_agent_id != prev_assignee and task.assigned_to_agent_id != actor.id:
            mail = await self._assigned_mail(task, task.assigned_to_agent_id, actor)
            if mail is not None:
                mails[task.assigned_to_agent_id] = mail
        creator = task.created_by_agent_id
        if creator is not None and creator != actor.id and creator not in mails:
            if task.status != prev_status:
                mail = await self._status_mail(task, creator, actor)
            elif task.assigned_to_agent_id != prev_assignee:
                mail = await self._assigned_mail(task, creator, actor)
            else:
                mail = None
            if mail is not None:
                mails[creator] = mail
        if mails:
            await self._notify(mails)
        return (await self._project([task]))[0]

    async def _flip_status(
        self, actor: Agent, task_id: uuid.UUID, new_status: str
    ) -> PlatformTaskRead:
        task = await self._get_or_404(task_id)
        changed = task.status != new_status
        self._apply_status(task, new_status, actor)
        await self.db.commit()
        await self.db.refresh(task)
        creator = task.created_by_agent_id
        if changed and creator is not None and creator != actor.id:
            mail = await self._status_mail(task, creator, actor)
            if mail is not None:
                await self._notify({creator: mail})
        return (await self._project([task]))[0]

    async def complete(self, actor: Agent, task_id: uuid.UUID) -> PlatformTaskRead:
        return await self._flip_status(actor, task_id, "done")

    async def reopen(self, actor: Agent, task_id: uuid.UUID) -> PlatformTaskRead:
        return await self._flip_status(actor, task_id, "todo")

    async def delete(self, task_id: uuid.UUID) -> None:
        task = await self._get_or_404(task_id)
        await self.db.delete(task)
        await self.db.commit()

    async def summary(self) -> PlatformTaskSummary:
        return PlatformTaskSummary(**await self.repository.summary())

    async def list_operators(self) -> list[PlatformOperatorRead]:
        rows = (
            (
                await self.db.execute(
                    select(Agent)
                    .join(Role, Agent.role_id == Role.id)
                    .where(Role.is_system.is_(True), Role.name.in_(PLATFORM_ROLE_NAMES))
                    .order_by(Agent.first_name, Agent.last_name)
                )
            )
            .scalars()
            .all()
        )
        return [
            PlatformOperatorRead(agent_id=a.id, name=f"{a.first_name} {a.last_name}".strip())
            for a in rows
        ]
