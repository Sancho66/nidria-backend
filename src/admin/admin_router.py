"""Superadmin platform admin surface (Groupe C UI). ADDITIVE: no
existing endpoint is touched. GET /agencies stays the LIGHT switcher
endpoint (the header "Changer d'agence"); GET /admin/agencies is the
RICH table endpoint (the "Gérer les agences" screen). Same superadmin
gate as the rest of the platform lifecycle (agency.create)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.admin.admin_manager import AdminManager
from src.admin.admin_schema import AdminAgenciesResponse
from src.admin.platform_tasks_manager import PlatformTasksManager
from src.admin.platform_tasks_schema import (
    CalendarLinkResponse,
    CompleteTaskRequest,
    PlatformOperatorRead,
    PlatformTaskAttachmentRead,
    PlatformTaskCreate,
    PlatformTaskListResponse,
    PlatformTaskRead,
    PlatformTaskSummary,
    PlatformTaskUpdate,
)
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.exceptions import ValidationError
from src.core.http import file_download_response
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/admin", tags=["admin"])

BINDINGS = [
    # Platform tool: only the superadmin holds agency.create; an agency
    # admin/agent/expat is 403. Reached with the superadmin's OWN token
    # (not an impersonation session — see the impersonation note in the
    # tests).
    RouteBinding("GET", "/admin/agencies", Audience.AGENT, Permission.AGENCY_CREATE),
    # The superadmin internal task backlog (Prism tasks port v1) — its OWN
    # platform permission: delegating tasks never implies agency creation.
    RouteBinding("GET", "/admin/tasks", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE),
    RouteBinding("POST", "/admin/tasks", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE),
    RouteBinding("GET", "/admin/tasks/summary", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE),
    RouteBinding("GET", "/admin/operators", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE),
    RouteBinding(
        "PATCH", "/admin/tasks/{task_id}", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE
    ),
    RouteBinding(
        "DELETE", "/admin/tasks/{task_id}", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE
    ),
    RouteBinding(
        "POST", "/admin/tasks/{task_id}/complete", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE
    ),
    RouteBinding(
        "POST", "/admin/tasks/{task_id}/reopen", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE
    ),
    RouteBinding(
        "GET",
        "/admin/tasks/{task_id}/calendar-link",
        Audience.AGENT,
        Permission.PLATFORM_TASK_MANAGE,
    ),
    RouteBinding(
        "POST",
        "/admin/tasks/{task_id}/attachments",
        Audience.AGENT,
        Permission.PLATFORM_TASK_MANAGE,
    ),
    RouteBinding(
        "GET",
        "/admin/tasks/{task_id}/attachments",
        Audience.AGENT,
        Permission.PLATFORM_TASK_MANAGE,
    ),
    RouteBinding(
        "GET",
        "/admin/tasks/{task_id}/attachments/{attachment_id}/download",
        Audience.AGENT,
        Permission.PLATFORM_TASK_MANAGE,
    ),
    RouteBinding(
        "DELETE",
        "/admin/tasks/{task_id}/attachments/{attachment_id}",
        Audience.AGENT,
        Permission.PLATFORM_TASK_MANAGE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/agencies", response_model=AdminAgenciesResponse)
async def list_agencies(
    agent: AgentDep,
    db: DbDep,
    search: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = Query(1, ge=1),
    # le=200: the task form loads the full agency selector in one page
    # (the front asks 200; a 422 here silently EMPTIED the selector).
    page_size: int = Query(20, ge=1, le=200),
    trial_expiring_within_days: int | None = Query(None, ge=0),
    onboarding_incomplete: bool = False,
    billing_status: str | None = Query(None, pattern="^(active|past_due|canceled)$"),
) -> AdminAgenciesResponse:
    """The superadmin agencies table: paginated, searchable (name/slug),
    sortable (created_at|name|cases_count), with derived status, seat/case
    counts, the 3 onboarding gestures, the S0/S1/S2 state and the login
    heartbeat. Filters (combinable, applied in SQL BEFORE pagination):
    `trial_expiring_within_days`, `onboarding_incomplete` — Eric's "who expires
    soon and hasn't started". A CONSTANT number of queries, no N+1."""
    return await AdminManager(db).list_agencies(
        search=search,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
        trial_expiring_within_days=trial_expiring_within_days,
        onboarding_incomplete=onboarding_incomplete,
        billing_status=billing_status,
    )


# --- platform tasks (superadmin backlog, Prism port v1) -----------------------


@router.get("/tasks", response_model=PlatformTaskListResponse)
async def list_platform_tasks(
    agent: AgentDep,
    db: DbDep,
    assigned_to: str | None = Query(None, description="an agent UUID, or 'me'"),
    agency_id: uuid.UUID | None = None,
    status: str | None = None,
    include_done: bool = True,
    priority: str | None = None,
    task_type: str | None = None,
    is_overdue: bool = False,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> PlatformTaskListResponse:
    """The Prism list order, computed: done last, priority desc, due_at
    asc NULLS LAST. Completion precedence: an explicit `status` beats
    `include_done=false` (Prism). No due_this_week param: the pair
    due_after/due_before covers any window — the front sends the week
    bounds it displays (timezone-correct on ITS side)."""
    assignee: uuid.UUID | None = None
    if assigned_to is not None:
        if assigned_to == "me":
            assignee = agent.id
        else:
            try:
                assignee = uuid.UUID(assigned_to)
            except ValueError as exc:
                raise ValidationError(
                    "assigned_to must be an agent UUID or 'me'.",
                    code="task.assigned_to_invalid",
                ) from exc
    return await PlatformTasksManager(db).list_tasks(
        page=page,
        page_size=page_size,
        assigned_to=assignee,
        agency_id=agency_id,
        status=status,
        include_done=include_done,
        priority=priority,
        task_type=task_type,
        is_overdue=is_overdue,
        due_before=due_before,
        due_after=due_after,
    )


@router.get("/tasks/summary", response_model=PlatformTaskSummary)
async def platform_tasks_summary(agent: AgentDep, db: DbDep) -> PlatformTaskSummary:
    return await PlatformTasksManager(db).summary()


@router.post("/tasks", response_model=PlatformTaskRead, status_code=201)
async def create_platform_task(
    payload: PlatformTaskCreate, agent: AgentDep, db: DbDep
) -> PlatformTaskRead:
    """Assignee defaults to the acting superadmin; an explicit assignee
    must be a platform superadmin too."""
    return await PlatformTasksManager(db).create(agent, payload)


@router.patch("/tasks/{task_id}", response_model=PlatformTaskRead)
async def update_platform_task(
    task_id: uuid.UUID, payload: PlatformTaskUpdate, agent: AgentDep, db: DbDep
) -> PlatformTaskRead:
    return await PlatformTasksManager(db).update(agent, task_id, payload)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_platform_task(task_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    await PlatformTasksManager(db).delete(task_id)


@router.post("/tasks/{task_id}/complete", response_model=PlatformTaskRead)
async def complete_platform_task(
    task_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    body: CompleteTaskRequest | None = None,
) -> PlatformTaskRead:
    """Optional body: completion_message, the client-facing note carried
    by the done email (verbatim, never translated) and stored for life."""
    return await PlatformTasksManager(db).complete(
        agent, task_id, completion_message=body.completion_message if body else None
    )


@router.post("/tasks/{task_id}/reopen", response_model=PlatformTaskRead)
async def reopen_platform_task(task_id: uuid.UUID, agent: AgentDep, db: DbDep) -> PlatformTaskRead:
    return await PlatformTasksManager(db).reopen(agent, task_id)


@router.get("/operators", response_model=list[PlatformOperatorRead])
async def list_platform_operators(agent: AgentDep, db: DbDep) -> list[PlatformOperatorRead]:
    """The assignable platform operators (superadmin role holders) for
    the task form selector — same gate as the tasks themselves."""
    return await PlatformTasksManager(db).list_operators()


@router.get("/tasks/{task_id}/calendar-link", response_model=CalendarLinkResponse)
async def platform_task_calendar_link(
    task_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> CalendarLinkResponse:
    """Google / Outlook / ICS for a scheduled task (the Prism port).
    400 when the task has no scheduled_at."""
    return await PlatformTasksManager(db).calendar_link(task_id)


@router.post(
    "/tasks/{task_id}/attachments", response_model=PlatformTaskAttachmentRead, status_code=201
)
async def upload_platform_task_attachment(
    task_id: uuid.UUID, file: UploadFile, agent: AgentDep, db: DbDep
) -> PlatformTaskAttachmentRead:
    """Multipart upload. Limits ALIGNED on case documents: same size cap
    (413 task.attachment_too_large) and same extension whitelist (415
    task.attachment_type_unsupported)."""
    return await PlatformTasksManager(db).upload_attachment(agent, task_id, file)


@router.get("/tasks/{task_id}/attachments", response_model=list[PlatformTaskAttachmentRead])
async def list_platform_task_attachments(
    task_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[PlatformTaskAttachmentRead]:
    return await PlatformTasksManager(db).list_attachments(task_id)


@router.get("/tasks/{task_id}/attachments/{attachment_id}/download")
async def download_platform_task_attachment(
    task_id: uuid.UUID, attachment_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> Response:
    """The documents mechanism: the private bucket is re-streamed by the
    backend (no signed URL anywhere in this product)."""
    file_name, content = await PlatformTasksManager(db).download_attachment(task_id, attachment_id)
    return file_download_response(file_name, content)


@router.delete("/tasks/{task_id}/attachments/{attachment_id}", status_code=204)
async def delete_platform_task_attachment(
    task_id: uuid.UUID, attachment_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> None:
    await PlatformTasksManager(db).delete_attachment(task_id, attachment_id)
