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
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import UploadFile
from sqlalchemy import delete, select
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
from shared.models.platform_task_attachment import PlatformTaskAttachment
from shared.models.platform_task_watcher import PlatformTaskWatcher
from shared.models.rbac import Role
from src.admin.platform_tasks_repository import PlatformTasksRepository
from src.admin.platform_tasks_schema import (
    CalendarLinkResponse,
    PlatformOperatorRead,
    PlatformTaskAttachmentRead,
    PlatformTaskCreate,
    PlatformTaskListResponse,
    PlatformTaskRead,
    PlatformTaskSummary,
    PlatformTaskUpdate,
)
from src.core import storage
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import (
    EmailContent,
    task_assigned_email,
    task_status_changed_email,
)
from src.core.exceptions import (
    BadRequestError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from src.core.i18n import resolve_notification_lang_agent
from src.core.rbac.baseline import PLATFORM_ROLE_NAMES

logger = logging.getLogger(__name__)


def _ics_escape(value: str) -> str:
    """RFC 5545 text escaping (the Prism helper)."""
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_calendar_link(task: PlatformTask, agency_name: str | None) -> CalendarLinkResponse:
    """The Prism generator, ported: Google (floating local + ctz=),
    Outlook (offset-baked ISO), minimal ICS (TZID, no VTIMEZONE block —
    calendar apps resolve IANA names against the system tzdb). Falls
    back to UTC on a legacy/typo zone."""
    assert task.scheduled_at is not None  # checked by caller
    duration = task.duration_minutes or 30
    tz_name = task.scheduled_timezone or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz_name = "UTC"
        tz = ZoneInfo("UTC")

    start_local = task.scheduled_at.astimezone(tz)
    end_local = start_local + timedelta(minutes=duration)
    start_compact = start_local.strftime("%Y%m%dT%H%M%S")
    end_compact = end_local.strftime("%Y%m%dT%H%M%S")

    title = task.title if agency_name is None else f"{task.title} - {agency_name}"
    description = task.description or ""
    location = task.location or ""

    google_url = (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={quote(title)}"
        f"&dates={start_compact}/{end_compact}"
        f"&ctz={quote(tz_name)}"
        f"&details={quote(description)}"
        f"&location={quote(location)}"
    )
    outlook_url = (
        "https://outlook.live.com/calendar/0/action/compose"
        f"?subject={quote(title)}"
        f"&startdt={quote(start_local.isoformat())}"
        f"&enddt={quote(end_local.isoformat())}"
        f"&body={quote(description)}"
        f"&location={quote(location)}"
    )
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Nidria//Platform Tasks//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:nidria-task-{task.id}@nidria\r\n"
        f"DTSTAMP:{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"DTSTART;TZID={tz_name}:{start_compact}\r\n"
        f"DTEND;TZID={tz_name}:{end_compact}\r\n"
        f"SUMMARY:{_ics_escape(title)}\r\n"
        f"DESCRIPTION:{_ics_escape(description)}\r\n"
        f"LOCATION:{_ics_escape(location)}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )
    return CalendarLinkResponse(
        google_url=google_url,
        outlook_url=outlook_url,
        ics_content=ics,
        title=title,
        start=start_local,
        end=end_local,
    )


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

    async def _validate_watchers(self, watcher_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Every watcher must be an ACTIVE platform operator (superadmin,
        deactivated_at NULL) — 422 named otherwise. Deduplicated; the
        assignee/creator MAY watch (the send dedup absorbs them)."""
        unique_ids = list(dict.fromkeys(watcher_ids))
        if not unique_ids:
            return []
        rows = (
            (
                await self.db.execute(
                    select(Agent.id)
                    .join(Role, Agent.role_id == Role.id)
                    .where(
                        Agent.id.in_(unique_ids),
                        Agent.deactivated_at.is_(None),
                        Role.is_system.is_(True),
                        Role.name.in_(PLATFORM_ROLE_NAMES),
                    )
                )
            )
            .scalars()
            .all()
        )
        invalid = set(unique_ids) - set(rows)
        if invalid:
            raise ValidationError(
                "Every watcher must be an active platform superadmin.",
                code="task.watcher_not_active_operator",
            )
        return unique_ids

    async def _replace_watchers(self, task_id: uuid.UUID, watcher_ids: list[uuid.UUID]) -> None:
        await self.db.execute(
            delete(PlatformTaskWatcher).where(PlatformTaskWatcher.task_id == task_id)
        )
        for agent_id in watcher_ids:
            self.db.add(PlatformTaskWatcher(task_id=task_id, agent_id=agent_id))

    async def _watcher_ids(self, task_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            (
                await self.db.execute(
                    select(PlatformTaskWatcher.agent_id).where(
                        PlatformTaskWatcher.task_id == task_id
                    )
                )
            ).scalars()
        )

    async def _validate_agency(self, agency_id: uuid.UUID) -> None:
        if await self.db.get(Agency, agency_id) is None:
            raise NotFoundError("Agency not found.", code="agency.not_found")

    # --- projection -----------------------------------------------------------

    async def _project(self, tasks: list[PlatformTask]) -> list[PlatformTaskRead]:
        agencies, agents = await self.repository.display_names(tasks)
        watcher_rows = (
            (
                await self.db.execute(
                    select(PlatformTaskWatcher.task_id, Agent.id, Agent.first_name, Agent.last_name)
                    .join(Agent, Agent.id == PlatformTaskWatcher.agent_id)
                    .where(PlatformTaskWatcher.task_id.in_([t.id for t in tasks]))
                    .order_by(Agent.first_name, Agent.last_name)
                )
            ).all()
            if tasks
            else []
        )
        watchers_by_task: dict[uuid.UUID, list[PlatformOperatorRead]] = {}
        for task_id, agent_id, first, last in watcher_rows:
            watchers_by_task.setdefault(task_id, []).append(
                PlatformOperatorRead(agent_id=agent_id, name=f"{first} {last}".strip())
            )
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
                scheduled_at=t.scheduled_at,
                scheduled_timezone=t.scheduled_timezone,
                duration_minutes=t.duration_minutes,
                location=t.location,
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
                completion_message=t.completion_message,
                watchers=watchers_by_task.get(t.id, []),
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
        message = (task.completion_message or "").strip()
        content = task_status_changed_email(
            title=task.title,
            new_status=task.status,
            actor_name=self._actor_name(actor),
            tasks_url=f"{get_settings().frontend_url}/admin/tasks",
            lang=lang,
            # The conditional block: done only, and only a real message.
            client_message=message if (task.status == "done" and message) else None,
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
            scheduled_at=payload.scheduled_at,
            scheduled_timezone=payload.scheduled_timezone,
            duration_minutes=payload.duration_minutes,
            location=payload.location,
            agency_id=payload.agency_id,
            assigned_to_agent_id=assignee,
            created_by_agent_id=actor.id,
        )
        # Created straight into a done lane: the audit stamp applies (Prism).
        self._apply_status(task, payload.status or "todo", actor)
        self.db.add(task)
        await self.db.flush()
        if payload.watcher_agent_ids is not None:
            watcher_ids = await self._validate_watchers(payload.watcher_agent_ids)
            await self._replace_watchers(task.id, watcher_ids)
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
        if "scheduled_at" in fields:
            task.scheduled_at = payload.scheduled_at
            if payload.scheduled_at is None:
                task.scheduled_timezone = None
        if "scheduled_timezone" in fields and payload.scheduled_timezone is not None:
            # Prism exact: a timezone-only edit KEEPS the wall clock the
            # operator picked and moves the stored UTC instant.
            if (
                "scheduled_at" not in fields
                and task.scheduled_at is not None
                and task.scheduled_timezone
                and payload.scheduled_timezone != task.scheduled_timezone
            ):
                local = task.scheduled_at.astimezone(ZoneInfo(task.scheduled_timezone))
                task.scheduled_at = local.replace(tzinfo=ZoneInfo(payload.scheduled_timezone))
            task.scheduled_timezone = payload.scheduled_timezone
        if "duration_minutes" in fields:
            task.duration_minutes = payload.duration_minutes
        if "location" in fields:
            task.location = payload.location
        if "agency_id" in fields:
            if payload.agency_id is not None:
                await self._validate_agency(payload.agency_id)
            task.agency_id = payload.agency_id
        if "completion_message" in fields:
            # PATCHable after the fact (correcting a posted note); reopen
            # never clears it — provided content lives forever.
            task.completion_message = payload.completion_message
        if "assigned_to_agent_id" in fields and payload.assigned_to_agent_id is not None:
            await self._validate_assignee(payload.assigned_to_agent_id)
            task.assigned_to_agent_id = payload.assigned_to_agent_id
        if "watcher_agent_ids" in fields and payload.watcher_agent_ids is not None:
            watcher_ids = await self._validate_watchers(payload.watcher_agent_ids)
            await self._replace_watchers(task.id, watcher_ids)
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
        if task.status != prev_status:
            # Watchers join on the SAME trigger (a real status change) —
            # the dedup dict absorbs creator/assignee overlaps, the actor
            # is never a recipient.
            for watcher_id in await self._watcher_ids(task.id):
                if watcher_id != actor.id and watcher_id not in mails:
                    mail = await self._status_mail(task, watcher_id, actor)
                    if mail is not None:
                        mails[watcher_id] = mail
        if mails:
            await self._notify(mails)
        return (await self._project([task]))[0]

    async def _flip_status(
        self,
        actor: Agent,
        task_id: uuid.UUID,
        new_status: str,
        completion_message: str | None = None,
    ) -> PlatformTaskRead:
        task = await self._get_or_404(task_id)
        if completion_message is not None:
            task.completion_message = completion_message
        changed = task.status != new_status
        self._apply_status(task, new_status, actor)
        await self.db.commit()
        await self.db.refresh(task)
        mails: dict[uuid.UUID, tuple[str, EmailContent]] = {}
        creator = task.created_by_agent_id
        if changed and creator is not None and creator != actor.id:
            mail = await self._status_mail(task, creator, actor)
            if mail is not None:
                mails[creator] = mail
        if changed:
            for watcher_id in await self._watcher_ids(task.id):
                if watcher_id != actor.id and watcher_id not in mails:
                    mail = await self._status_mail(task, watcher_id, actor)
                    if mail is not None:
                        mails[watcher_id] = mail
        if mails:
            await self._notify(mails)
        return (await self._project([task]))[0]

    async def complete(
        self, actor: Agent, task_id: uuid.UUID, completion_message: str | None = None
    ) -> PlatformTaskRead:
        return await self._flip_status(
            actor, task_id, "done", completion_message=completion_message
        )

    async def reopen(self, actor: Agent, task_id: uuid.UUID) -> PlatformTaskRead:
        return await self._flip_status(actor, task_id, "todo")

    async def delete(self, task_id: uuid.UUID) -> None:
        task = await self._get_or_404(task_id)
        attachments = (
            (
                await self.db.execute(
                    select(PlatformTaskAttachment).where(PlatformTaskAttachment.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
        for attachment in attachments:
            # Best-effort physical cleanup (the Prism contract): a storage
            # hiccup never blocks the delete, the DB CASCADE takes the rows.
            try:
                await asyncio.to_thread(storage.delete, attachment.storage_path)
            except Exception:
                logger.warning("attachment blob cleanup failed: %s", attachment.storage_path)
        await self.db.delete(task)
        await self.db.commit()

    async def calendar_link(self, task_id: uuid.UUID) -> CalendarLinkResponse:
        task = await self._get_or_404(task_id)
        if task.scheduled_at is None:
            raise BadRequestError("This task has no scheduled time.", code="task.not_scheduled")
        return build_calendar_link(task, await self._agency_name(task.agency_id))

    # --- attachments (Prism port; limits ALIGNED on case documents) -----------

    async def list_attachments(self, task_id: uuid.UUID) -> list[PlatformTaskAttachmentRead]:
        await self._get_or_404(task_id)
        rows = (
            (
                await self.db.execute(
                    select(PlatformTaskAttachment)
                    .where(PlatformTaskAttachment.task_id == task_id)
                    .order_by(PlatformTaskAttachment.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [PlatformTaskAttachmentRead.model_validate(r) for r in rows]

    async def upload_attachment(
        self, actor: Agent, task_id: uuid.UUID, file: UploadFile
    ) -> PlatformTaskAttachmentRead:
        await self._get_or_404(task_id)
        settings = get_settings()
        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("A file name is required.", code="task.attachment_no_name")
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in settings.allowed_document_extensions:
            raise UnsupportedMediaTypeError(
                f"File type .{extension or '?'} is not accepted. "
                f"Allowed: {', '.join(settings.allowed_document_extensions)}.",
                code="task.attachment_type_unsupported",
            )
        content = await file.read()
        if len(content) > settings.max_document_size_mb * 1024 * 1024:
            raise PayloadTooLargeError(
                f"File exceeds {settings.max_document_size_mb} MB.",
                code="task.attachment_too_large",
            )
        attachment_id = uuid.uuid4()
        # uuid-only key: the display name NEVER reaches the storage path.
        path = f"platform-tasks/{task_id}/{attachment_id}"
        content_type = file.content_type or "application/octet-stream"
        await asyncio.to_thread(storage.upload, path, content, content_type)
        row = PlatformTaskAttachment(
            id=attachment_id,
            task_id=task_id,
            file_name=filename[:255],
            content_type=content_type,
            size_bytes=len(content),
            storage_path=path,
            uploaded_by_agent_id=actor.id,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return PlatformTaskAttachmentRead.model_validate(row)

    async def _attachment_or_404(
        self, task_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> PlatformTaskAttachment:
        """Scoped by task_id: an attachment of ANOTHER task is a 404 —
        no id traversal through the URL."""
        row = (
            await self.db.execute(
                select(PlatformTaskAttachment).where(
                    PlatformTaskAttachment.id == attachment_id,
                    PlatformTaskAttachment.task_id == task_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("Attachment not found.", code="task.attachment_not_found")
        return row

    async def download_attachment(
        self, task_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> tuple[str, bytes]:
        row = await self._attachment_or_404(task_id, attachment_id)
        content = await asyncio.to_thread(storage.download, row.storage_path)
        return row.file_name, content

    async def delete_attachment(self, task_id: uuid.UUID, attachment_id: uuid.UUID) -> None:
        row = await self._attachment_or_404(task_id, attachment_id)
        # Storage FIRST (the documents order): a mid-failure leaves a
        # recoverable orphan row, never a dangling blob.
        await asyncio.to_thread(storage.delete, row.storage_path)
        await self.db.delete(row)
        await self.db.commit()

    async def summary(self) -> PlatformTaskSummary:
        return PlatformTaskSummary(**await self.repository.summary())

    async def list_operators(self) -> list[PlatformOperatorRead]:
        rows = (
            (
                await self.db.execute(
                    select(Agent)
                    .join(Role, Agent.role_id == Role.id)
                    .where(
                        Agent.deactivated_at.is_(None),  # actifs (spec operators)
                        Role.is_system.is_(True),
                        Role.name.in_(PLATFORM_ROLE_NAMES),
                    )
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
