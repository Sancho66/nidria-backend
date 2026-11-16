"""Separate support projections: no client content in the default view."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SupportOperatorResponse(BaseModel):
    id: uuid.UUID
    name: str


class SupportGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator_id: uuid.UUID
    duration_minutes: Literal[15, 30, 60, 120, 240]


class SupportGrantResponse(BaseModel):
    id: uuid.UUID
    operator_id: uuid.UUID
    operator_name: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    active: bool


class SupportEventResponse(BaseModel):
    id: uuid.UUID
    actor_name: str
    action: str
    resource_id: uuid.UUID | None
    created_at: datetime


class SupportAccessResponse(BaseModel):
    grants: list[SupportGrantResponse]
    events: list[SupportEventResponse]
    events_total: int
    page: int
    page_size: int


class SupportStepResponse(BaseModel):
    id: uuid.UUID
    position: int
    status: str


class SupportCaseResponse(BaseModel):
    id: uuid.UUID
    status: str
    steps: list[SupportStepResponse]
    access_expires_at: datetime | None


class SupportCasesResponse(BaseModel):
    items: list[SupportCaseResponse]
    total: int
    page: int
    page_size: int


class SupportPersonResponse(BaseModel):
    name: str
    email: str | None
    phone: str | None


class SupportDocumentResponse(BaseModel):
    id: uuid.UUID
    filename: str
    validation_status: str | None


class SupportTextResponse(BaseModel):
    id: uuid.UUID
    body: str
    created_at: datetime
    step_id: uuid.UUID | None = None


class SupportCaseContentResponse(BaseModel):
    case_id: uuid.UUID
    access_expires_at: datetime
    people: list[SupportPersonResponse]
    documents: list[SupportDocumentResponse]
    notes: list[SupportTextResponse]
    messages: list[SupportTextResponse]
    # Explicit limits keep the support page bounded without pretending completeness.
    truncated: bool = False
