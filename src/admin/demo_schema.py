"""The superadmin's demo tool; no production dossier identifiers are accepted."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.i18n import Language
from src.journeys.journeys_schema import JourneyImportRequest


class DemoJourneyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language = "fr"
    template_id: str | None = Field(default=None, max_length=100)
    journey_import: JourneyImportRequest | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "DemoJourneyInput":
        if (self.template_id is None) == (self.journey_import is None):
            raise ValueError("Choose one journey source")
        if self.journey_import and self.journey_import.provider_assignments:
            raise ValueError("A demo cannot reference real providers")
        return self


class DemoCreateRequest(DemoJourneyInput):
    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    completed_steps: int = Field(default=2, ge=0, le=80)
    provided_documents: list[str] = Field(default_factory=list, max_length=200)
    include_reminder: bool = True
    include_signature: bool = True
    logo_data_url: str | None = Field(default=None, max_length=350_000)


class DemoDocumentChoice(BaseModel):
    key: str
    step_ref: str
    step_label: str
    label: str


class DemoJourneyPreview(BaseModel):
    name: str
    language: Language
    steps: list[str]
    documents: list[DemoDocumentChoice]


class DemoTemplateRead(BaseModel):
    id: str
    name: str
    language: Language
    steps: int


class DemoRead(BaseModel):
    code: str
    name: str
    language: Language
    journey_name: str
    completed_steps: int
    total_steps: int
    public_url: str
    enabled: bool
    created_at: datetime
    has_logo: bool


class DemoListResponse(BaseModel):
    items: list[DemoRead]
    total: int
    page: int
    page_size: int


class DemoControlEnvelope(BaseModel):
    """Internal transport only, never an endpoint in the production contract."""

    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)
