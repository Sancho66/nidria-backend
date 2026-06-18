"""Expat portal field contract — designed BY EXCLUSION (what the client
must NOT see): no notes (even non-confidential — internal use), no raw
ActivityLog (the journal is the agency's tool; the projected timeline IS
the client view), no tags/source (internal qualification), no agent
list / internal staffing, NO internal UUID in the timeline. FR labels
("votre agence", "vous") are the frontend's job — the API ships stable
semantics."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

# Same inline custom-field schema the agency face embeds on CaseDetailResponse
# — reused (not re-declared) so both faces describe a custom field identically.
from src.cases.cases_schema import CustomFieldDefinitionInline

# Same resolved days-remaining counter the agency timeline ships — one
# computation in timeline_for_case, read by both faces (single source).
from src.progress.progress_schema import DeadlineCounter, StepContentAttachment


class ExpatAgencyResponse(BaseModel):
    name: str


class ExpatCaseSummaryResponse(BaseModel):
    id: uuid.UUID
    agency: ExpatAgencyResponse
    origin_country: str | None
    dest_country: str | None
    status: str
    steps_done: int
    steps_total: int
    created_at: datetime
    updated_at: datetime


class ExpatReferentResponse(BaseModel):
    """The named human contact at the agency (the case owner) —
    the brief: « nom du référent + son mail »."""

    first_name: str
    last_name: str
    email: str


class ExpatResponsibleResponse(BaseModel):
    """Displayable responsible: type 'agency' (no internal agent name —
    staffing doesn't face the client), 'you', or 'external' with the
    contact's name. type None = not assigned yet."""

    type: str | None
    name: str | None


class ExpatParticipantResponse(BaseModel):
    """A step participant as the client sees it (responsible refonte, N).
    Same anti-staffing as the responsible: 'agency' (no internal name),
    'you', or 'external' with the provider's name; plus the role."""

    role: str
    type: str | None  # agency | you | external
    name: str | None


class ExpatRequirementResponse(BaseModel):
    """A concrete requirement the client can see (and, for the writable
    kinds, fulfill). `person_label` is the RESOLVED name so the client
    knows whose passport/field is asked ("vous" is a frontend label —
    the API ships the real name). Archived custom-field requirements are
    filtered out upstream, never exposed."""

    id: uuid.UUID
    kind: str  # base_field | custom_field | document | case_field
    reference: str
    scope: str | None  # None for case-level requirements (vague C)
    status: str  # pending | provided (derived live for fields)
    person_label: str
    # NEW WAVE: read parity with the agency face.
    # Live value at the source so the client can see/re-edit what was
    # already provided (None for documents and when pending). For a
    # custom_field, the matching custom_field_definitions entry (by
    # `reference` = its key) tells the frontend how to render it.
    value: Any = None
    # The document the client deposited for a document requirement — join
    # to GET /expat/cases/{id}/documents for filename + download link.
    document_id: uuid.UUID | None = None
    # Backing plane (vague C): "person" (default) or "case". The front
    # routes the fulfillment endpoint by it (case write lands in C2).
    target: str = "person"


class ExpatTimelineStepResponse(BaseModel):
    # VAGUE 5: the case_step_progress id — needed to address the client
    # comment thread (/expat/cases/{case_id}/steps/{progress_id}/comments).
    # NOT an internal UUID leak: it's a step of the CLIENT'S OWN dossier,
    # which the expat already passes in its own comment URLs.
    progress_id: uuid.UUID
    name: str
    position: int
    status: str  # projected (blocked computed at read time)
    estimated_days: int | None
    completed_at: datetime | None
    blocked_by: list[str]  # step NAMES, never ids
    responsible: ExpatResponsibleResponse
    # "Action à réaliser par" — N participants with roles (anti-staffing).
    participants: list[ExpatParticipantResponse]
    # NEW WAVE 2: the concrete requirements the client can fill on this
    # step (writable while the step is active).
    requirements: list[ExpatRequirementResponse]
    # How the step closes — lets the client UX phrase the right message:
    # `auto` (closes by itself once all provided) vs `agency_validation`
    # (awaits the agency's validation).
    completion_mode: str
    # VAGUE 5: non-deleted comment count for a "X messages" badge.
    comment_count: int
    # Resolved days-remaining counter (firm deadline or estimated-derived).
    counter: DeadlineCounter
    # Feature 2 — descending agency content. The client ALWAYS sees the
    # step's note + attachments on their own dossier (downloaded via the
    # dedicated gated endpoint, bytes never inlined).
    content_note: str | None
    attachments: list[StepContentAttachment]
    # "Action validée par" — true when the client is the step's validator
    # and the step is active → the front shows the "validate" button. The
    # server still re-checks on the validate call (never trusts this flag).
    can_validate: bool


class RequirementValueRequest(BaseModel):
    """Client fulfillment of a base_field / custom_field requirement.
    `value` is type-validated downstream against the field kind; null
    clears it (requirement goes back to pending)."""

    value: Any = None


class ExpatCaseDetailResponse(ExpatCaseSummaryResponse):
    referent: ExpatReferentResponse | None
    timeline: list[ExpatTimelineStepResponse]
    # Active custom-field definitions of the agency (archived filtered) —
    # same shape the agency face embeds on CaseDetailResponse, so the
    # client renders a custom_field requirement correctly (select as a
    # select, human label, options) by matching its `reference` to a
    # definition `key`.
    custom_field_definitions: list[CustomFieldDefinitionInline]


class ExpatNotificationResponse(BaseModel):
    """A SENT in_app reminder IS the notification (Q8 — no extra table).
    Its own id stays: the V1.5 mark-as-read will need it."""

    id: uuid.UUID
    message_body: str
    sent_at: datetime
