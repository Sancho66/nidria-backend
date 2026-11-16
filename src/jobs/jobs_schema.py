import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.jobs.run_privacy import safe_statistics


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
    """Operational metadata only: private logs and error details are redacted."""

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

    @model_validator(mode="after")
    def remove_private_details(self) -> Self:
        self.stats = safe_statistics(self.stats)
        self.error = None
        return self


class JobRunDetailResponse(JobRunResponse):
    log_output: str

    @field_validator("log_output")
    @classmethod
    def hide_private_log(cls, value: str) -> str:
        return ""
