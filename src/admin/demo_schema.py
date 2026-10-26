"""The superadmin's demo tool; no production dossier identifiers are accepted."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

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
    batch: str = Field(default="", max_length=100)
    lifetime_days: Literal[30, 60, 90] = 60


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
    scope: Literal["client", "agency"] = "client"
    lead_email: str | None = None
    expires_at: datetime | None = None
    first_opened_at: datetime | None = None
    last_opened_at: datetime | None = None
    visits: int = 0
    batch: str = ""
    status: Literal["active", "disabled", "expired"] = "active"
    tour_status: Literal["not_started", "started", "completed", "skipped"] = "not_started"
    tour_last_step: int = 0
    tour_started_at: datetime | None = None
    tour_completed_at: datetime | None = None


class AgencyDemoInput(BaseModel):
    """One approved prospect row; never a real client or production identifier."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    company_name: str = Field(min_length=1, max_length=200)
    language: Literal["en", "fr"] = "en"
    profession: Literal["accountant", "lawyer", "formation", "relocation"]
    country: Literal["GB", "US", "IE", "CA", "AU", "NZ", "FR", "BE"]
    city: str = Field(default="", max_length=150)
    steps: list[str] = Field(min_length=6, max_length=6)
    step_titles: list[str] = Field(default_factory=list, max_length=6)
    journey_name: str | None = Field(default=None, max_length=200)
    documents: list[str] = Field(default_factory=list, max_length=60)
    persona: Literal["client", "director", "employer", "couple", "family", "employee"]
    logo_data_url: str | None = Field(default=None, max_length=350_000)

    @field_validator("steps")
    @classmethod
    def six_nonempty_steps(cls, steps: list[str]) -> list[str]:
        normalized = [step.strip() for step in steps]
        if any(not step or len(step) > 1000 for step in normalized):
            raise ValueError("Six nonempty step descriptions are required")
        return normalized

    @field_validator("step_titles")
    @classmethod
    def short_titles(cls, titles: list[str]) -> list[str]:
        normalized = [" ".join(title.split()) for title in titles]
        if normalized and len(normalized) != 6:
            raise ValueError("Supply six titles; blanks use the profession defaults")
        if any(
            title and (not 2 <= len(title.split()) <= 5 or len(title) > 80) for title in normalized
        ):
            raise ValueError("Step titles must contain two to five words, at most 80 characters")
        return normalized if any(normalized) else []

    @field_validator("documents")
    @classmethod
    def document_labels(cls, documents: list[str]) -> list[str]:
        normalized = [document.strip() for document in documents]
        if any(not document or len(document) > 200 for document in normalized):
            raise ValueError("Document names must contain 1 to 200 characters")
        if len({document.casefold() for document in normalized}) != len(normalized):
            raise ValueError("Document names must be unique")
        return normalized


class AgencyDemoBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    items: list[AgencyDemoInput] = Field(min_length=1, max_length=5)
    batch: str = Field(default="", max_length=100)
    lifetime_days: Literal[30, 60, 90] = 60

    @model_validator(mode="after")
    def unique_emails(self) -> "AgencyDemoBatchRequest":
        if len({str(item.email).casefold() for item in self.items}) != len(self.items):
            raise ValueError("Duplicate lead email in the batch")
        return self


class AgencyDemoBatchResponse(BaseModel):
    items: list[DemoRead]


class DemoListResponse(BaseModel):
    items: list[DemoRead]
    total: int
    page: int
    page_size: int


class DemoFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    search: str = Field(default="", max_length=200)
    batch: str = Field(default="", max_length=100)
    status: Literal["all", "active", "disabled", "expired"] = "all"
    opened: Literal["all", "opened", "unopened"] = "all"


class DemoSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    codes: list[str] | None = Field(default=None, min_length=1, max_length=5000)
    filters: DemoFilters | None = None
    excluded_codes: list[str] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def exactly_one_selector(self) -> "DemoSelection":
        import re

        if (self.codes is None) == (self.filters is None):
            raise ValueError("Choose codes or filters")
        if self.codes and (
            len(set(self.codes)) != len(self.codes)
            or any(not re.fullmatch(r"[a-f0-9]{32}", code) for code in self.codes)
        ):
            raise ValueError("Invalid demo codes")
        if self.excluded_codes and (
            self.filters is None
            or any(not re.fullmatch(r"[a-f0-9]{32}", code) for code in self.excluded_codes)
        ):
            raise ValueError("Only a filtered selection can exclude valid codes")
        return self


class DemoExportResponse(BaseModel):
    csv: str
    count: int
    selection_token: str


class DemoBulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: DemoSelection
    action: Literal["disable", "delete"]
    expected_count: int = Field(ge=1, le=5000)
    selection_token: str = Field(min_length=1, max_length=300)


class DemoBulkResponse(BaseModel):
    affected: int


class DemoArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch: str = Field(default="", max_length=100)


class DemoControlEnvelope(BaseModel):
    """Internal transport only, never an endpoint in the production contract."""

    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)
