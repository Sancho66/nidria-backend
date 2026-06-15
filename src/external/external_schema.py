"""External provider portal — field contract by EXCLUSION (RGPD). What
an assigned external must NOT see is enforced by the SHAPE here, not just
by filtering: no notes (confidential or not), no activity, no reminders,
no other external assignees / external_contacts, no internal staff, and
— critically — NO requirement `value` (the client's passport number etc.
is the client's data, not the provider's). The external sees the journey
timeline, the requirements (status + whose + what kind, never the value),
and the agency referent to contact."""

import uuid
from datetime import datetime

from pydantic import BaseModel

# Reused resolved counter (one computation in timeline_for_case).
from src.progress.progress_schema import DeadlineCounter


class ExternalAgencyResponse(BaseModel):
    name: str


class ExternalReferentResponse(BaseModel):
    """The agency contact the provider can reach (the case owner)."""

    first_name: str
    last_name: str
    email: str


class ExternalPrincipalResponse(BaseModel):
    """The case holder's identity — the minimum a provider needs to know
    WHO they are mandated for (a lawyer can't draft an act for "a client
    in Paraguay"). Name only: still NO sensitive value (passport, notes…)."""

    first_name: str
    last_name: str


class ExternalResponsibleResponse(BaseModel):
    type: str | None  # agency | you | external | None
    name: str | None


class ExternalRequirementResponse(BaseModel):
    """A requirement as the provider sees it — status, whose, what kind.
    DELIBERATELY no `value`: the actual personal data (passport number,
    date of birth, …) is never exposed to a third party by default."""

    id: uuid.UUID
    kind: str
    reference: str
    scope: str
    status: str
    person_label: str
    document_id: uuid.UUID | None


class ExternalTimelineStepResponse(BaseModel):
    progress_id: uuid.UUID
    name: str
    position: int
    status: str
    estimated_days: int | None
    completed_at: datetime | None
    blocked_by: list[str]
    responsible: ExternalResponsibleResponse
    completion_mode: str
    comment_count: int
    counter: DeadlineCounter
    requirements: list[ExternalRequirementResponse]


class ExternalCaseSummaryResponse(BaseModel):
    id: uuid.UUID
    agency: ExternalAgencyResponse
    principal: ExternalPrincipalResponse
    origin_country: str | None
    dest_country: str | None
    status: str
    steps_done: int
    steps_total: int
    created_at: datetime
    updated_at: datetime


class ExternalCaseDetailResponse(ExternalCaseSummaryResponse):
    referent: ExternalReferentResponse | None
    timeline: list[ExternalTimelineStepResponse]


class ExternalDocumentResponse(BaseModel):
    """Provider-facing document — no internal UUID (no uploaded_by_id):
    `uploaded_by_type` + `is_mine` (the provider's own uploads)."""

    id: uuid.UUID
    case_id: uuid.UUID
    filename: str
    uploaded_by_type: str
    is_mine: bool
    validation_status: str | None
    expires_at: datetime | None
    created_at: datetime
    step_name: str | None
    requirement_reference: str | None
    is_requirement: bool


class ExternalAssignmentResponse(BaseModel):
    """Agency-side view of who is assigned (the external agents)."""

    agent_id: uuid.UUID
    first_name: str
    last_name: str
    email: str
    role: str


class ExternalAssignmentCreateRequest(BaseModel):
    agent_id: uuid.UUID
