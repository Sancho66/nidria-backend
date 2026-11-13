"""Read-only adoption of production agencies whose first source was a demo."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DemoAdoptionSummary(BaseModel):
    accounts: int = 0
    journeys: int = 0
    clients: int = 0
    cases: int = 0


class DemoAdoptionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    search: str = Field(default="", max_length=200)
    batch: str = Field(default="", max_length=100)
    demo: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    activity: Literal["all", "none", "journeys", "clients", "cases"] = "all"
    sort: Literal["recent", "activity", "journeys", "clients", "cases"] = "recent"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)


class DemoAdoptionRead(DemoAdoptionSummary):
    agency_id: uuid.UUID
    name: str
    batch: str
    registered_at: datetime
    last_login_at: datetime | None
    last_creation_at: datetime | None
    has_demo_reference: bool


class DemoAdoptionTotals(BaseModel):
    accounts: int
    with_journeys: int
    with_clients: int
    with_cases: int


class DemoAdoptionResponse(BaseModel):
    items: list[DemoAdoptionRead]
    total: int
    page: int
    page_size: int
    totals: DemoAdoptionTotals
    generated_at: datetime
