import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: str
    name: str
    cron_expression: str
    timezone: str
    is_enabled: bool
    paused_until: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None


class JobConfigUpdateRequest(BaseModel):
    cron_expression: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, min_length=1, max_length=50)
    is_enabled: bool | None = None


class JobPauseRequest(BaseModel):
    until: datetime


class JobTriggerRequest(BaseModel):
    dry_run: bool = False


class JobRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: int | None
    stats: dict[str, Any]
    error: str | None
    triggered_by: str
    triggered_by_agent_id: uuid.UUID | None


class JobRunDetailResponse(JobRunResponse):
    log_output: str
