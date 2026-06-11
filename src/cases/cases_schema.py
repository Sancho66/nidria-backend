import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.core.enums import CaseStatus, ExternalContactType
from src.progress.progress_schema import StepProgressResponse

_COUNTRY_PATTERN = r"^[A-Z]{2}$"


class CaseCreateRequest(BaseModel):
    """Principal-only create: family members and external contacts go
    through their own endpoints."""

    # Principal expat (linked-or-created by email; an EXISTING user's
    # identity is never overwritten by this payload).
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    preferred_lang: str = Field(default="fr", min_length=2, max_length=5)
    # Case
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    dest_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    status: CaseStatus = CaseStatus.PROSPECT
    source: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list)
    owner_agent_id: uuid.UUID | None = None  # default: the creator


class CaseUpdateRequest(BaseModel):
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    dest_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    status: CaseStatus | None = None
    source: str | None = Field(default=None, max_length=100)
    tags: list[str] | None = None
    owner_agent_id: uuid.UUID | None = None


class CaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agency_id: uuid.UUID
    principal_expat_user_id: uuid.UUID
    owner_agent_id: uuid.UUID | None
    journey_template_id: uuid.UUID | None
    origin_country: str | None
    dest_country: str | None
    status: str
    source: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class PrincipalSummaryResponse(BaseModel):
    """Identity subset for list items — the full PrincipalResponse
    (activated) stays a detail-endpoint concern."""

    model_config = ConfigDict(from_attributes=True)

    first_name: str
    last_name: str
    email: str
    preferred_lang: str


class CaseListItemResponse(CaseResponse):
    principal: PrincipalSummaryResponse


class CaseListResponse(BaseModel):
    items: list[CaseListItemResponse]
    total: int
    page: int
    page_size: int


class PrincipalResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    preferred_lang: str
    activated: bool


class FamilyMemberRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=50)


class FamilyMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    relationship: str


class ExternalContactCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    type: ExternalContactType = ExternalContactType.OTHER


class ExternalContactUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    type: ExternalContactType | None = None


class ExternalContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    type: str


class CaseNoteCreateRequest(BaseModel):
    body: str = Field(min_length=1)
    is_confidential: bool = False


class CaseNoteUpdateRequest(BaseModel):
    """`is_confidential` is immutable after creation — flipping it
    would silently move the note across the visibility boundary."""

    body: str = Field(min_length=1)


class CaseNoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_agent_id: uuid.UUID | None
    body: str
    is_confidential: bool
    created_at: datetime
    updated_at: datetime


class CaseDetailResponse(CaseResponse):
    principal: PrincipalResponse
    family_members: list[FamilyMemberResponse]
    external_contacts: list[ExternalContactResponse]
    notes: list[CaseNoteResponse]
    # Projected timeline (BLOCKED computed at read time).
    progress: list[StepProgressResponse]


class CaseFilters(BaseModel):
    """Query-side contract of GET /cases."""

    status: list[CaseStatus] | None = None
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    dest_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    owner_agent_id: uuid.UUID | None = None
    preferred_lang: str | None = None
    tag: list[str] | None = None  # contains-ALL semantics
    q: str | None = None  # ilike on principal first/last name + email

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
