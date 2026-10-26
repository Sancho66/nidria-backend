"""Bounded uploads and durable import progress for platform operators."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_IMPORT_ROWS = 500
MAX_IMPORT_BYTES = 8_000_000


class DemoImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    csv_text: str = Field(min_length=1, max_length=MAX_IMPORT_BYTES)
    batch: str = Field(min_length=1, max_length=100)
    lifetime_days: Literal[30, 60, 90] = 60
    expected_count: int = Field(ge=1, le=MAX_IMPORT_ROWS)

    @field_validator("batch")
    @classmethod
    def named_batch(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A batch name is required")
        return value

    @field_validator("csv_text")
    @classmethod
    def bounded_utf8(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("CSV exceeds 8 MB")
        return value


class DemoImportIssue(BaseModel):
    row: int
    column: str
    reason: str


class DemoImportRead(BaseModel):
    id: uuid.UUID
    batch: str
    lifetime_days: int
    status: Literal["queued", "running", "completed"]
    total: int
    processed: int
    created: int
    reused: int
    failed: int
    created_at: datetime
    updated_at: datetime
    errors: list[DemoImportIssue] = Field(default_factory=list)


class DemoImportList(BaseModel):
    items: list[DemoImportRead]


class DemoImportLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: uuid.UUID
    kind: Literal["links", "errors"] = "links"


class DemoImportExport(BaseModel):
    csv: str
    count: int
