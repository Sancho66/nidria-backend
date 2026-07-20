"""Superadmin platform tasks (Prism tasks port v1, GO 2026-07-20).

Fixed 3-lane statuses in code (no per-tenant catalog), priorities as the
Prism Literal. The nullable agency link is the SUBJECT of the work, not
a scope — served denormalized (agency_name) for the list."""

import uuid
from datetime import datetime
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.models.platform_task import (
    PLATFORM_TASK_PRIORITIES,
    PLATFORM_TASK_STATUSES,
    PLATFORM_TASK_TYPES,
)

TaskStatus = str  # validated against PLATFORM_TASK_STATUSES in the manager
TaskPriority = str  # validated against PLATFORM_TASK_PRIORITIES in the manager

_STATUS_HELP = f"one of {PLATFORM_TASK_STATUSES}"
_PRIORITY_HELP = f"one of {PLATFORM_TASK_PRIORITIES}"
_TYPE_HELP = f"one of {PLATFORM_TASK_TYPES}"


def _validate_timezone(value: str | None) -> str | None:
    """None passes; otherwise the string must resolve to a real IANA
    zone (the exact Prism validation) — surfaced as a 422."""
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {value!r}") from exc
    return value


class PlatformTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: str | None = Field(default=None, description=_STATUS_HELP)
    priority: str = Field(default="medium", description=_PRIORITY_HELP)
    task_type: str = Field(default="task", description=_TYPE_HELP)
    due_at: datetime | None = None
    scheduled_at: datetime | None = None
    scheduled_timezone: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    location: str | None = Field(default=None, max_length=500)
    agency_id: uuid.UUID | None = None
    # Defaults to the acting superadmin (the solo-operator fast path).
    assigned_to_agent_id: uuid.UUID | None = None
    watcher_agent_ids: list[uuid.UUID] | None = None

    @field_validator("scheduled_timezone")
    @classmethod
    def _tz_is_iana(cls, v: str | None) -> str | None:
        return _validate_timezone(v)

    @model_validator(mode="after")
    def _scheduled_at_requires_tz(self) -> Self:
        if self.scheduled_at is not None and self.scheduled_timezone is None:
            raise ValueError("scheduled_timezone is required when scheduled_at is provided")
        return self


class PlatformTaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: str | None = Field(default=None, description=_STATUS_HELP)
    priority: str | None = Field(default=None, description=_PRIORITY_HELP)
    task_type: str | None = Field(default=None, description=_TYPE_HELP)
    due_at: datetime | None = None
    scheduled_at: datetime | None = None
    scheduled_timezone: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    location: str | None = Field(default=None, max_length=500)
    agency_id: uuid.UUID | None = None
    assigned_to_agent_id: uuid.UUID | None = None
    completion_message: str | None = None
    # FULL replacement of the watcher list (the simplest pattern).
    watcher_agent_ids: list[uuid.UUID] | None = None

    @field_validator("scheduled_timezone")
    @classmethod
    def _tz_is_iana(cls, v: str | None) -> str | None:
        return _validate_timezone(v)

    @model_validator(mode="after")
    def _scheduled_at_requires_tz(self) -> Self:
        # Prism exact: enforced only when scheduled_at is EXPLICITLY set
        # in this PATCH (a tz-only edit stays legal).
        if (
            "scheduled_at" in self.model_fields_set
            and self.scheduled_at is not None
            and self.scheduled_timezone is None
        ):
            raise ValueError("scheduled_timezone is required when scheduled_at is provided")
        return self


class CompleteTaskRequest(BaseModel):
    """Optional body of POST /complete: the client-facing note carried
    by the done email (copy-paste), stored on the task."""

    model_config = ConfigDict(extra="forbid")

    completion_message: str | None = None


class PlatformTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    status: str
    priority: str
    task_type: str
    due_at: datetime | None
    scheduled_at: datetime | None
    scheduled_timezone: str | None
    duration_minutes: int | None
    location: str | None
    is_overdue: bool
    agency_id: uuid.UUID | None
    agency_name: str | None
    assigned_to_agent_id: uuid.UUID
    assigned_to_name: str
    assigned_by: "PlatformOperatorRead | None"
    assigned_at: datetime | None
    created_by_agent_id: uuid.UUID | None
    completed_by_agent_id: uuid.UUID | None
    completed_by_name: str | None
    completed_at: datetime | None
    completion_message: str | None
    watchers: list["PlatformOperatorRead"] = []
    created_at: datetime
    updated_at: datetime


class PlatformTaskListResponse(BaseModel):
    items: list[PlatformTaskRead]
    total: int
    page: int
    page_size: int


class CalendarLinkResponse(BaseModel):
    """The Prism calendar-link payload: Google / Outlook URLs + minimal
    ICS, start/end rendered in the task's own zone."""

    google_url: str
    outlook_url: str
    ics_content: str
    title: str
    start: datetime
    end: datetime


class PlatformTaskAttachmentRead(BaseModel):
    """One attached file — storage_path is INTERNAL, never served."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    file_name: str
    content_type: str
    size_bytes: int
    uploaded_by_agent_id: uuid.UUID | None
    created_at: datetime


class PlatformOperatorRead(BaseModel):
    """One assignable platform operator (superadmin) — the task form
    selector. No pagination: they are two."""

    agent_id: uuid.UUID
    name: str


class PlatformTaskSummary(BaseModel):
    """The sidebar badge numbers (Prism's TaskSummary, platform-wide)."""

    total: int
    pending: int
    overdue: int
    due_today: int
    completed_this_week: int
