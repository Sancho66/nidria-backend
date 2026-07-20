"""Superadmin platform tasks (Prism tasks port v1, GO 2026-07-20).

Fixed 3-lane statuses in code (no per-tenant catalog), priorities as the
Prism Literal. The nullable agency link is the SUBJECT of the work, not
a scope — served denormalized (agency_name) for the list."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from shared.models.platform_task import PLATFORM_TASK_PRIORITIES, PLATFORM_TASK_STATUSES

TaskStatus = str  # validated against PLATFORM_TASK_STATUSES in the manager
TaskPriority = str  # validated against PLATFORM_TASK_PRIORITIES in the manager

_STATUS_HELP = f"one of {PLATFORM_TASK_STATUSES}"
_PRIORITY_HELP = f"one of {PLATFORM_TASK_PRIORITIES}"


class PlatformTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: str | None = Field(default=None, description=_STATUS_HELP)
    priority: str = Field(default="medium", description=_PRIORITY_HELP)
    due_at: datetime | None = None
    agency_id: uuid.UUID | None = None
    # Defaults to the acting superadmin (the solo-operator fast path).
    assigned_to_agent_id: uuid.UUID | None = None


class PlatformTaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: str | None = Field(default=None, description=_STATUS_HELP)
    priority: str | None = Field(default=None, description=_PRIORITY_HELP)
    due_at: datetime | None = None
    agency_id: uuid.UUID | None = None
    assigned_to_agent_id: uuid.UUID | None = None


class PlatformTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    status: str
    priority: str
    due_at: datetime | None
    is_overdue: bool
    agency_id: uuid.UUID | None
    agency_name: str | None
    assigned_to_agent_id: uuid.UUID
    assigned_to_name: str
    created_by_agent_id: uuid.UUID | None
    completed_by_agent_id: uuid.UUID | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PlatformTaskListResponse(BaseModel):
    items: list[PlatformTaskRead]
    total: int
    page: int
    page_size: int


class PlatformTaskSummary(BaseModel):
    """The sidebar badge numbers (Prism's TaskSummary, platform-wide)."""

    total: int
    pending: int
    overdue: int
    due_today: int
    completed_this_week: int
